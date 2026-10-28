import json
import os
import shlex
import stat
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from dbt_enhance import popup
from dbt_enhance.models import EnhanceError, QueryRequest


def test_popup_private_transport_and_cleanup(tmp_path, monkeypatch):
    request = QueryRequest("select '$HOME'", tmp_path / "some file.sql")
    monkeypatch.setenv("DBT_TEST_SECRET", "private-token")
    monkeypatch.setenv("TMUX_PANE", "%4")
    monkeypatch.setattr(popup, "tmux_binary", lambda: "/bin/tmux")
    paths = []

    def run(command, **kwargs):
        assert command[:2] == ["/bin/tmux", "display-popup"]
        assert command[command.index("-t") + 1] == "%4"
        assert "private-token" not in " ".join(command)
        child = shlex.split(command[-1])
        path = Path(child[4])
        paths.append(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        payload = json.loads(path.read_text())
        assert payload["request"]["sql"] == request.sql
        assert payload["request"]["backend"] == "worker"
        assert payload["environment"]["DBT_TEST_SECRET"] == "private-token"
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(popup.subprocess, "run", run)
    popup.launch_popup(request)
    assert len(paths) == 1
    assert not paths[0].parent.exists()


def test_failed_popup_also_cleans_request(tmp_path, monkeypatch):
    monkeypatch.setattr(popup, "tmux_binary", lambda: "tmux")
    paths = []

    def run(command, **kwargs):
        paths.append(Path(shlex.split(command[-1])[4]))
        return subprocess.CompletedProcess(command, 1, "", "no current client")

    monkeypatch.setattr(popup.subprocess, "run", run)
    with pytest.raises(EnhanceError, match="no current client"):
        popup.launch_popup(QueryRequest("select 1", tmp_path / "x.sql"))
    assert not paths[0].parent.exists()


def test_handoff_preserves_credentials_but_uses_popup_terminal(tmp_path, monkeypatch):
    path = tmp_path / "request.json"
    data = asdict(
        QueryRequest(
            "select 1",
            tmp_path / "orders.sql",
            allowed_targets=("dev",),
            worker_idle_seconds=37,
            diagnostics=False,
            theme="light",
        )
    )
    data["file"] = str(data["file"])
    path.write_text(
        json.dumps(
            {
                "request": data,
                "environment": {
                    "TERM": "editor-term",
                    "PATH": "/editor/bin",
                    "MY_PASSWORD": "secret",
                },
            }
        )
    )
    monkeypatch.setattr(os, "environ", {"TERM": "popup-term", "STALE_PASSWORD": "bad"})
    result = popup.read_request(path, consume=True)
    assert result.file == tmp_path / "orders.sql"
    assert result.backend == "worker"
    assert result.allowed_targets == ("dev",)
    assert result.worker_idle_seconds == 37
    assert result.diagnostics is False
    assert result.theme == "light"
    assert not path.exists()
    assert os.environ == {"TERM": "popup-term", "PATH": "/editor/bin", "MY_PASSWORD": "secret"}


@pytest.mark.parametrize("invalid", [{"allowed_targets": "dev"}, {"theme": []}])
def test_invalid_handoff_does_not_replace_environment(tmp_path, monkeypatch, invalid):
    path = tmp_path / "request.json"
    data = asdict(QueryRequest("select 1", tmp_path / "orders.sql"))
    data["file"] = str(data["file"])
    data.update(invalid)
    path.write_text(json.dumps({"request": data, "environment": {"REPLACED": "yes"}}))
    monkeypatch.setattr(os, "environ", {"ORIGINAL": "yes"})
    with pytest.raises(EnhanceError):
        popup.read_request(path, consume=True)
    assert os.environ == {"ORIGINAL": "yes"}


def test_no_tmux_has_actionable_error(monkeypatch):
    monkeypatch.setattr(popup.shutil, "which", lambda _: None)
    with pytest.raises(EnhanceError, match="Install tmux"):
        popup.tmux_binary()


@pytest.mark.parametrize(
    "version,valid", [("tmux 3.2a", False), ("tmux 3.3a", True), ("tmux 3.5a", True)]
)
def test_tmux_minimum_version(monkeypatch, version, valid):
    monkeypatch.setattr(popup.shutil, "which", lambda _: "tmux")
    monkeypatch.setattr(
        popup.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, version, "")
    )
    if valid:
        assert popup.tmux_binary(require_session=False) == "tmux"
    else:
        with pytest.raises(EnhanceError, match="Upgrade tmux"):
            popup.tmux_binary(require_session=False)


def test_no_session_has_actionable_error(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(popup.shutil, "which", lambda _: "tmux")
    monkeypatch.setattr(
        popup.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "tmux 3.5a", "")
    )
    with pytest.raises(EnhanceError, match="tmux new-session"):
        popup.tmux_binary()
