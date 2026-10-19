import json
import subprocess

import pytest
from typer.testing import CliRunner

from dbt_enhance import install
from dbt_enhance.cli import app
from dbt_enhance.models import EnhanceError

runner = CliRunner()


def test_help_and_version():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("query", "popup", "setup", "doctor", "demo"):
        assert name in result.stdout
    assert "_view" not in result.stdout
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.2" in result.stdout


@pytest.mark.parametrize(
    "arguments,sql,fragment",
    [
        (["--file", "[scratch]"], "select 1", "Save this buffer"),
        (["--file", "model.sql"], "  \n", "selection is empty"),
        (["--file", "model.sql", "--limit", "0"], "select 1", "1"),
        (["--file", "model.sql", "--timeout", "nan"], "select 1", "finite"),
        (["--file", "model.sql"], "x" * 1_048_577, "exceeds 1 MiB"),
    ],
)
def test_invalid_requests_fail_before_running(arguments, sql, fragment):
    result = runner.invoke(app, ["query", "--json", *arguments], input=sql)
    assert result.exit_code != 0
    assert fragment in result.output


def test_popup_receives_exact_selection_and_absolute_buffer(tmp_path, monkeypatch):
    from dbt_enhance import popup

    captured = []
    monkeypatch.setattr(popup, "launch_popup", captured.append)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["popup", "--file", "models/my query.sql"], input="select {{ 1 + 2 }}\n"
    )
    assert result.exit_code == 0, result.output
    assert captured[0].sql == "select {{ 1 + 2 }}\n"
    assert captured[0].file == tmp_path / "models/my query.sql"


@pytest.mark.parametrize(
    "direct,expected",
    [
        (None, "dbt-enhance==0.1.0"),
        ({"url": "file:///tmp/local%20project", "dir_info": {}}, "file:///tmp/local%20project"),
        (
            {
                "url": "https://example.com/repo.git",
                "vcs_info": {"vcs": "git", "commit_id": "abc123"},
            },
            "git+https://example.com/repo.git@abc123",
        ),
    ],
)
def test_install_source_follows_uvx_provenance(monkeypatch, direct, expected):
    class Package:
        version = "0.1.0"

        def read_text(self, _):
            return json.dumps(direct) if direct else None

    monkeypatch.setattr(install, "distribution", lambda _: Package())
    assert install.installation_source() == expected


def test_setup_refuses_conflicts_before_installing(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    original = '[keys.select.space]\nx = "quit"\n'
    config.write_text(original)
    commands = []
    monkeypatch.setattr(install.shutil, "which", lambda _: "uv")

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, str(tmp_path / "bin"), "")

    monkeypatch.setattr(install.subprocess, "run", run)
    with pytest.raises(EnhanceError, match="already has a binding"):
        install.setup(config, source=".")
    assert len(commands) == 1
    assert commands[0] == ["uv", "tool", "dir", "--bin"]
    assert config.read_text() == original


def test_setup_installs_stable_tool_before_config(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    binary = tmp_path / "bin" / "dbt-enhance"
    binary.parent.mkdir()
    commands = []
    monkeypatch.setattr(install.shutil, "which", lambda _: "uv")

    def run(command, **kwargs):
        commands.append(command)
        if "install" in command:
            binary.touch()
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, str(binary.parent), "")

    monkeypatch.setattr(install.subprocess, "run", run)
    installed, backup = install.setup(config, source="file:///tmp/package")
    assert installed == binary
    assert backup is None
    assert commands[-1] == ["uv", "tool", "install", "--force", "file:///tmp/package"]
    assert str(binary) in config.read_text()


def test_worker_backend_is_default_with_explicit_process_opt_out(tmp_path, monkeypatch):
    from dbt_enhance import popup

    captured = []
    monkeypatch.setattr(popup, "launch_popup", captured.append)
    args = ["popup", "--file", str(tmp_path / "model.sql")]
    assert runner.invoke(app, args, input="select 1").exit_code == 0
    assert captured[-1].backend == "worker"
    assert runner.invoke(app, [*args, "--backend", "process"], input="select 1").exit_code == 0
    assert captured[-1].backend == "process"
    monkeypatch.setenv("DBT_ENHANCE_BACKEND", "process")
    assert runner.invoke(app, args, input="select 1").exit_code == 0
    assert captured[-1].backend == "process"
    assert runner.invoke(app, [*args, "--backend", "worker"], input="select 1").exit_code == 0
    assert captured[-1].backend == "worker"
    assert runner.invoke(app, [*args, "--backend", "invalid"], input="select 1").exit_code != 0


def test_query_command_uses_worker_default(tmp_path, monkeypatch):
    from dbt_enhance import ui

    captured = []

    class FakeApp:
        def __init__(self, *, request):
            captured.append(request)

        def run(self):
            pass

    monkeypatch.setattr(ui, "QueryApp", FakeApp)
    args = ["query", "--file", str(tmp_path / "model.sql"), "--sql", "select 1"]
    assert runner.invoke(app, args).exit_code == 0
    assert captured[-1].backend == "worker"
    assert runner.invoke(app, [*args, "--backend", "process"]).exit_code == 0
    assert captured[-1].backend == "process"


def test_stop_resolves_project_without_starting_a_query(tmp_path, monkeypatch):
    from dbt_enhance import worker

    (tmp_path / "dbt_project.yml").touch()
    seen = []
    monkeypatch.setattr(worker, "stop", lambda project: seen.append(project) or True)
    result = runner.invoke(app, ["stop", "--file", str(tmp_path / "model.sql")])
    assert result.exit_code == 0, result.output
    assert "stopped" in result.stdout
    assert seen == [tmp_path.resolve()]
