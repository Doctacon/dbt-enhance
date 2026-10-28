from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from dbt_enhance import dbt, worker
from dbt_enhance._wire import Decoder, WireError, encode
from dbt_enhance.models import EnhanceError, QueryRequest

_FAKE_SESSION = """
import os
import sys
import time
from pathlib import Path

class UnsupportedSession(Exception):
    pass

class QueryError(RuntimeError):
    pass

class SessionError(RuntimeError):
    def __init__(self, message, *, reason="request_cleanup_failed"):
        super().__init__(message)
        self.reason = reason

def compatibility():
    return {"dbt_core": "test", "python": "test"}

class DbtSession:
    def __init__(self, context, on_config=None):
        self.project = Path(context["project_dir"])
        self.count = 0
        if on_config:
            on_config([str(self.project / "credential.key")])

    def execute(self, sql, limit):
        self.count += 1
        if sql == "sleep":
            time.sleep(60)
        if sql == "sso":
            print("Initiating login request with your identity provider.", flush=True)
            print("Enter the URL the SSO URL redirected you to:", flush=True)
            print("https://example.invalid/auth?token=DO_NOT_FORWARD", flush=True)
            time.sleep(60)
        if sql == "fail":
            raise QueryError("deliberate SQL error")
        if sql == "fatal":
            raise RuntimeError("deliberate fatal error")
        if sql == "cleanup-fail":
            raise SessionError("deliberate cleanup failure")
        if sql == "uncertain":
            with (self.project / "execution-count").open("a") as f:
                f.write("executed\\n")
            os._exit(9)
        rows = [[os.getpid(), self.count, os.environ.get("WORKER_TEST_VALUE", "unset")]]
        columns = ["pid", "count", "value"]
        if sql == "argv":
            rows, columns = [[" ".join(sys.argv)]], ["argv"]
        return {"columns": columns, "rows": rows,
                "compiled_sql": "select 1 limit " + str(limit), "warnings": []}

    def close(self):
        (self.project / ("closed-" + str(os.getpid()))).touch()
"""


@pytest.fixture
def setup_worker(tmp_path, monkeypatch):
    # Unix socket paths must remain short on macOS as well as Linux.
    with tempfile.TemporaryDirectory(prefix="de-test-", dir="/tmp") as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
        monkeypatch.delenv("DBT_PROFILES_DIR", raising=False)
        monkeypatch.delenv("DBT_ENGINE_PROFILES_DIR", raising=False)
        helper_dir = tmp_path / "helper"
        helper_dir.mkdir()
        for filename in ("_worker_helper.py", "_wire.py"):
            shutil.copy(Path(worker.__file__).with_name(filename), helper_dir / filename)
        (helper_dir / "_session.py").write_text(_FAKE_SESSION)
        monkeypatch.setattr(worker, "_HELPER", helper_dir / "_worker_helper.py")
        project = tmp_path / "project with spaces"
        project.mkdir()
        (project / "dbt_project.yml").write_text("name: fake_worker\n")
        (project / "credential.key").write_text("synthetic-test-key")
        profiles = tmp_path / "profiles"
        profiles.mkdir()
        (profiles / "profiles.yml").write_text("test-only: true\n")
        executable = project / "dbt"
        executable.write_text(f"#!{sys.executable}\n" + worker._ENTRY_POINTS[0])
        executable.chmod(0o700)
        source = project / "query.sql"
        source.write_text("-- not used as the selection\n")
        request = QueryRequest(
            sql="pid",
            file=source,
            profiles_dir=profiles,
            dbt=executable,
            timeout=10,
            backend="worker",
        )
        try:
            yield project, request
        finally:
            try:
                worker.stop(project.resolve())
            except (EnhanceError, OSError):
                pass


def test_frames_survive_partial_and_coalesced_reads():
    first = encode({"type": "hello", "sql": "snowman ☃"})
    second = encode({"type": "result", "rows": [[1, None, True]]})
    decoder = Decoder()
    for byte in first[:-1]:
        decoder.feed(bytes([byte]))
        assert decoder.pop() is None
    decoder.feed(first[-1:] + second)
    assert decoder.pop() == {"type": "hello", "sql": "snowman ☃"}
    assert decoder.pop() == {"type": "result", "rows": [[1, None, True]]}
    assert decoder.pop() is None


@pytest.mark.parametrize("data", [struct.pack("!I", 1000), struct.pack("!I", 0)])
def test_frames_reject_bad_lengths_early(data):
    with pytest.raises(WireError):
        Decoder(maximum=100).feed(data)


@pytest.mark.parametrize("body", [b"not JSON", b"[]", b"\xff"])
def test_frames_reject_malformed_payloads(body):
    decoder = Decoder()
    decoder.feed(struct.pack("!I", len(body)) + body)
    with pytest.raises(WireError):
        decoder.pop()


def test_console_script_detection_preserves_venv_python_symlink(tmp_path):
    python = tmp_path / "python"
    python.symlink_to(sys.executable)
    for index, source in enumerate(worker._ENTRY_POINTS):
        executable = tmp_path / f"dbt-{index}"
        executable.write_text(f"#!{python}\n" + source)
        assert worker.project_python(executable) == python
        assert worker.project_python(executable) != python.resolve()


@pytest.mark.parametrize(
    "script",
    [
        '#!/bin/sh\nexec dbt "$@"\n',
        "#!/usr/bin/env python3\nfrom dbt.cli.main import cli\ncli()\n",
        f"#!{sys.executable}\nimport os\nos.environ['DBT_TARGET']='different'\n"
        + worker._ENTRY_POINTS[0],
    ],
)
def test_custom_wrapper_is_not_silently_bypassed(tmp_path, script):
    executable = tmp_path / "dbt"
    executable.write_text(script)
    with pytest.raises(EnhanceError, match="--backend process"):
        worker.project_python(executable)


def test_same_context_reuses_session_and_stop_closes_it(setup_worker):
    project, request = setup_worker
    first = dbt.run_query(request)
    second = dbt.run_query(request)
    assert first.rows[0][0] == second.rows[0][0]
    assert [first.rows[0][1], second.rows[0][1]] == [1, 2]
    assert first.compiled_sql == second.compiled_sql == "select 1 limit 500"
    information = worker.status(project.resolve())
    assert information["session_pid"] == first.rows[0][0]
    assert set(information) == {"pid", "session_pid", "versions"}
    alias = project.parent / "project-alias"
    alias.symlink_to(project, target_is_directory=True)
    assert worker.status(alias) == information
    assert worker.stop(alias)
    assert (project / f"closed-{first.rows[0][0]}").exists()


def test_worker_inspection_inventory_idle_identity_and_disconnect(setup_worker):
    project, request = setup_worker
    first = worker.run_query(request)
    information = worker.inspect_worker(project)
    assert information is not None
    assert information["project"] == project.resolve()
    assert information["state"] == "ready"
    assert information["idle_seconds"] == 600.0
    assert information["idle_remaining"] <= 600.0
    assert information["session_pid"] == first.rows[0][0]
    inventory = worker.inventory()
    assert len(inventory) == 1
    assert inventory[0]["project"] == information["project"]
    assert inventory[0]["session_pid"] == information["session_pid"]

    changed = worker.run_query(replace(request, worker_idle_seconds=30.0))
    assert changed.rows[0][0] != first.rows[0][0]
    assert worker.inspect_worker(project)["idle_seconds"] == 30.0
    assert worker.disconnect(project) is True
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and worker.inspect_worker(project) is not None:
        time.sleep(0.01)
    assert worker.inspect_worker(project) is None
    assert worker.disconnect(project) is False


def test_idle_only_lifecycle_commands_are_checked_by_supervisor(setup_worker):
    project, request = setup_worker
    worker.run_query(request)
    outcome = []

    def execute():
        try:
            worker.run_query(replace(request, sql="sleep"))
        except EnhanceError as error:
            outcome.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        information = worker.inspect_worker(project)
        if information is not None and information["state"] == "active":
            break
        time.sleep(0.01)
    else:
        pytest.fail("worker did not become active")
    assert worker.disconnect(project) is False
    assert worker.reset(project) is False
    assert worker.reset(project, allow_active=True) is True
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert len(outcome) == 1
    assert getattr(outcome[0], "cancellation", None) == "requested_unverified"
    retired = worker.retirement_status(project)
    assert retired["cancellation"] == "requested_unverified"
    assert retired["session_pid"] > 0


def test_worker_environment_preserves_arbitrary_profile_inputs(tmp_path, monkeypatch):
    source = {
        "_": "/bin/hx",
        "OLDPWD": "/previous",
        "SHLVL": "3",
        "PWD": "/caller",
        "TERM": "xterm",
        "TERM_PROGRAM": "test",
        "TMUX": "test",
        "TMUX_PANE": "%1",
        "LINES": "50",
        "COLUMNS": "100",
        "YAZI_ID": "1",
        "YAZI_LEVEL": "2",
        "DBT_TARGET": "dev",
        "DBT_ENHANCE_TARGET": "dev",
        "SNOWFLAKE_ACCOUNT": "test",
        "PATH": "/project/bin",
        "HOME": "/test/home",
        "AWS_SESSION_TOKEN": "synthetic",
        "CUSTOM_PROFILE_PASSWORD": "synthetic",
        "YAZI_CONFIG_HOME": "/test/yazi",
    }
    monkeypatch.setattr(os, "environ", source)
    effective = worker._worker_environment(tmp_path)
    assert effective["PWD"] == str(tmp_path)
    for key in (
        "_",
        "OLDPWD",
        "SHLVL",
        "TERM",
        "TERM_PROGRAM",
        "TMUX",
        "TMUX_PANE",
        "LINES",
        "COLUMNS",
        "YAZI_ID",
        "YAZI_LEVEL",
    ):
        assert key not in effective
    for key in (
        "DBT_TARGET",
        "DBT_ENHANCE_TARGET",
        "SNOWFLAKE_ACCOUNT",
        "PATH",
        "HOME",
        "AWS_SESSION_TOKEN",
        "CUSTOM_PROFILE_PASSWORD",
        "YAZI_CONFIG_HOME",
    ):
        assert effective[key] == source[key]
    assert source["PWD"] == "/caller"  # The caller's UI environment is unchanged.


@pytest.mark.parametrize(
    "name",
    [
        "_",
        "OLDPWD",
        "SHLVL",
        "PWD",
        "TERM",
        "TERM_PROGRAM",
        "TMUX",
        "TMUX_PANE",
        "LINES",
        "COLUMNS",
        "YAZI_ID",
        "YAZI_LEVEL",
    ],
)
def test_launcher_bookkeeping_does_not_restart_worker(setup_worker, monkeypatch, name):
    project, request = setup_worker
    monkeypatch.setenv(name, "first-launch")
    before = worker.run_query(request)
    monkeypatch.setenv(name, "second-launch")
    after = worker.run_query(request)
    assert after.rows[0][0] == before.rows[0][0]
    assert after.rows[0][1] == 2
    monkeypatch.delenv(name)
    without_marker = worker.run_query(request)
    assert without_marker.rows[0][0] == before.rows[0][0]
    assert without_marker.rows[0][1] == 3
    assert worker.retirement_status(project.resolve()) is None


def test_terminal_and_popup_handoff_share_worker(setup_worker, monkeypatch, tmp_path):
    from dbt_enhance.popup import read_request

    # read_request replaces os.environ; isolate that operation from the test host.
    monkeypatch.setattr(os, "environ", os.environ.copy())
    _, request = setup_worker
    monkeypatch.setenv("TERM", "terminal-before-popup")
    monkeypatch.setenv("_", "/bin/dbt-enhance")
    before = worker.run_query(request)
    editor_environment = dict(os.environ, SHLVL="3", OLDPWD="/previous", _="/bin/hx")
    handoff = tmp_path / "request.json"
    handoff.write_text(
        json.dumps(
            {
                "request": {
                    "sql": request.sql,
                    "file": str(request.file),
                    "dbt": str(request.dbt),
                    "profiles_dir": str(request.profiles_dir),
                    "backend": "worker",
                },
                "environment": editor_environment,
            }
        )
    )
    monkeypatch.setenv("TERM", "popup-terminal")
    monkeypatch.setenv("TMUX_PANE", "%99")
    popup_request = read_request(handoff)
    after = worker.run_query(popup_request)
    assert after.rows[0][0] == before.rows[0][0]
    assert after.rows[0][1] == 2


@pytest.mark.parametrize(
    "change",
    [
        "environment",
        "project",
        "project-touch",
        "profile",
        "target",
        "credential",
        "key-link",
        "dotenv",
    ],
)
def test_context_or_credentials_change_recycles_worker(setup_worker, monkeypatch, change):
    project, request = setup_worker
    if change == "dotenv":
        monkeypatch.delenv("PYTHON_DOTENV_DISABLED")
        (project / ".env").write_text("SYNTHETIC=before\n")
    if change == "key-link":
        (project / "old-key").write_text("synthetic-first-key")
        (project / "new-key").write_text("synthetic-second-key")
        (project / "credential.key").unlink()
        (project / "credential.key").symlink_to(project / "old-key")
    before = worker.run_query(request)
    if change == "environment":
        monkeypatch.setenv("WORKER_TEST_VALUE", "changed")
    elif change == "project":
        (project / "dbt_project.yml").write_text("name: changed\n")
    elif change == "project-touch":
        os.utime(project / "dbt_project.yml")
    elif change == "profile":
        (request.profiles_dir / "profiles.yml").write_text("test-only: changed\n")
    elif change == "target":
        request = replace(request, target="other")
    elif change == "credential":
        (project / "credential.key").write_text("synthetic-rotated-key")
    elif change == "key-link":
        (project / "credential.key").unlink()
        (project / "credential.key").symlink_to(project / "new-key")
    else:
        (project / ".env").write_text("SYNTHETIC=after\n")
    after = worker.run_query(request)
    assert before.rows[0][0] != after.rows[0][0]
    assert after.rows[0][1] == 1
    assert (project / f"closed-{before.rows[0][0]}").exists()
    assert worker.retirement_status(project.resolve()) == {
        "reason": "configuration_changed",
        "session_pid": before.rows[0][0],
    }
    if change == "environment":
        assert after.rows[0][2] == "changed"


def test_query_error_reuses_session_without_replaying_sql(setup_worker):
    project, request = setup_worker
    before = worker.run_query(request)
    with pytest.raises(EnhanceError, match="deliberate SQL error"):
        worker.run_query(replace(request, sql="fail"))
    assert worker.retirement_status(project.resolve()) is None
    after = worker.run_query(request)
    assert after.rows[0][0] == before.rows[0][0]
    assert after.rows[0][1] == 3
    assert worker.retirement_status(project.resolve()) is None


@pytest.mark.parametrize("failure", ["watched", "message", "cancelled"])
def test_reusable_error_still_retires_on_malformed_reply_or_cancellation(
    setup_worker, monkeypatch, failure
):
    _, request = setup_worker
    before = worker.run_query(request)
    cancel = threading.Event()
    receive = worker._receive

    def receive_error(*args, **kwargs):
        message = receive(*args, **kwargs)
        if message.get("reusable") is True:
            if failure == "cancelled":
                cancel.set()
            else:
                message[failure] = None
        return message

    with monkeypatch.context() as patch:
        patch.setattr(worker, "_receive", receive_error)
        with pytest.raises(EnhanceError, match="malformed|cancelled"):
            worker.run_query(replace(request, sql="fail"), cancel)
    after = worker.run_query(request)
    assert after.rows[0][0] != before.rows[0][0]


@pytest.mark.parametrize(
    ("sql", "message", "reason"),
    [
        ("fatal", "deliberate fatal error", "non_dbt_error"),
        ("cleanup-fail", "deliberate cleanup failure", "request_cleanup_failed"),
    ],
)
def test_fatal_errors_persist_reason_across_replacement(setup_worker, sql, message, reason):
    project, request = setup_worker
    before = worker.run_query(request)
    with pytest.raises(EnhanceError, match=message):
        worker.run_query(replace(request, sql=sql))
    expected = {"reason": reason, "session_pid": before.rows[0][0]}
    assert worker.retirement_status(project.resolve()) == expected
    after = worker.run_query(request)
    assert after.rows[0][0] != before.rows[0][0]
    assert after.rows[0][1] == 1
    assert worker.retirement_status(project.resolve()) == expected


def test_arbitrary_retirement_reason_is_replaced_not_persisted(setup_worker, monkeypatch):
    project, request = setup_worker
    before = worker.run_query(request)
    receive = worker._receive
    unsafe = "sqlstate from select synthetic-secret"

    def receive_unsafe_reason(*args, **kwargs):
        message = receive(*args, **kwargs)
        if message.get("type") == "error" and message.get("reusable") is not True:
            message["retirement_reason"] = unsafe
        return message

    with monkeypatch.context() as patch:
        patch.setattr(worker, "_receive", receive_unsafe_reason)
        with pytest.raises(EnhanceError, match="deliberate fatal error"):
            worker.run_query(replace(request, sql="fatal"))

    expected = {"reason": "worker_failure", "session_pid": before.rows[0][0]}
    assert worker.retirement_status(project.resolve()) == expected
    directory, _ = worker._slot(project.resolve(), worker._Deadline(2), create=False)
    state = (directory / "control.json").read_text()
    assert unsafe not in state
    assert "deliberate fatal error" not in state


def test_first_query_error_persists_credential_snapshot(setup_worker):
    project, request = setup_worker
    with pytest.raises(EnhanceError, match="deliberate SQL error"):
        worker.run_query(replace(request, sql="fail"))
    directory, _ = worker._slot(project.resolve(), worker._Deadline(2), create=False)
    before = json.loads((directory / "control.json").read_text())
    assert len(before["watched"]) == 1

    (project / "credential.key").write_text("synthetic-rotated-key")
    result = worker.run_query(request)
    assert result.rows[0][0] != before["session_pid"]
    assert result.rows[0][1] == 1


def test_crash_after_execution_is_never_replayed_or_sent_to_cli(setup_worker, monkeypatch):
    project, request = setup_worker
    worker.run_query(request)
    monkeypatch.setattr(dbt, "_run_process", lambda *a, **k: pytest.fail("Unsafe fallback"))
    with pytest.raises(EnhanceError):
        dbt.run_query(replace(request, sql="uncertain"))
    assert (project / "execution-count").read_text().splitlines() == ["executed"]


def test_timeout_retires_worker_and_next_request_recovers(setup_worker):
    _, request = setup_worker
    before = worker.run_query(request)
    started = time.monotonic()
    with pytest.raises(EnhanceError, match="timed out") as raised:
        worker.run_query(replace(request, sql="sleep", timeout=0.3))
    assert raised.value.code == "timeout"
    assert raised.value.stage == "execution"
    assert raised.value.cancellation == "requested_unverified"
    assert time.monotonic() - started < 3
    after = worker.run_query(request)
    assert after.rows[0][0] != before.rows[0][0]


def test_pre_execution_cancellation_has_safe_diagnostic_metadata(setup_worker):
    _, request = setup_worker
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(EnhanceError, match="cancelled") as raised:
        worker.run_query(request, cancel)
    assert raised.value.code == "cancelled"
    assert raised.value.stage == "preflight"
    assert not hasattr(raised.value, "cancellation")


def test_explicit_cancellation_returns_and_retires_session(setup_worker):
    _, request = setup_worker
    before = worker.run_query(request)
    cancel = threading.Event()
    timer = threading.Timer(0.3, cancel.set)
    timer.start()
    try:
        with pytest.raises(EnhanceError, match="cancelled") as raised:
            worker.run_query(replace(request, sql="sleep"), cancel)
    finally:
        timer.cancel()
    assert raised.value.code == "cancelled"
    assert raised.value.stage == "execution"
    assert raised.value.cancellation == "requested_unverified"
    assert worker.run_query(request).rows[0][0] != before.rows[0][0]


def test_lock_wait_shares_deadline_without_killing_other_query(setup_worker):
    project, request = setup_worker
    uncontended = []
    before = worker.run_query(request, on_status=uncontended.append)
    assert not any(getattr(item, "stage", None) == "queued" for item in uncontended)
    directory, _ = worker._slot(project.resolve(), worker._Deadline(2), create=False)
    queued = []
    with worker._lock(directory / "lock", worker._Deadline(2)):
        with pytest.raises(EnhanceError, match="timed out") as raised:
            worker.run_query(replace(request, timeout=0.2), on_status=queued.append)
    assert raised.value.code == "timeout"
    assert raised.value.stage == "preflight"
    assert not hasattr(raised.value, "cancellation")
    assert any(getattr(item, "stage", None) == "queued" for item in queued)
    assert worker.run_query(request).rows[0][0] == before.rows[0][0]


def test_exclusive_removal_guard_rejects_new_query_before_worker_start(setup_worker):
    project, request = setup_worker
    with worker.removal_guard():
        with pytest.raises(EnhanceError, match="removal is in progress"):
            worker.run_query(request)
        assert worker.inspect_worker(project) is None


def test_removal_guard_wait_for_starting_admission_is_bounded():
    deadline = worker._Deadline(2)
    epoch = worker._admission_snapshot(deadline)
    admission = worker._admit(epoch, deadline)
    started = time.monotonic()
    try:
        with pytest.raises(EnhanceError, match="query is still being admitted"):
            with worker.removal_guard(seconds=0.1):
                pytest.fail("exclusive guard entered while shared admission was held")
    finally:
        os.close(admission)
    assert time.monotonic() - started < 1


def test_queued_query_is_rejected_after_removal_epoch_changes(setup_worker):
    project, request = setup_worker
    before = worker.run_query(request)
    directory, _ = worker._slot(project.resolve(), worker._Deadline(2), create=False)
    queued = threading.Event()
    outcome = []

    def status(value):
        if getattr(value, "stage", None) == "queued":
            queued.set()

    def execute():
        try:
            worker.run_query(request, on_status=status)
        except EnhanceError as exc:
            outcome.append(exc)

    with worker._lock(directory / "lock", worker._Deadline(2)):
        thread = threading.Thread(target=execute)
        thread.start()
        assert queued.wait(2), "request never joined the project queue"
        with worker.removal_guard() as guard:
            guard.invalidate()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert len(outcome) == 1
    assert "predates package removal" in str(outcome[0])
    after = worker.run_query(request)
    assert after.rows[0][0] == before.rows[0][0]
    assert after.rows[0][1] == 2, "the stale queued request must not send SQL"


def test_removal_preserves_unconfirmed_active_query_and_confirmed_reset_blocks_admission(
    setup_worker, tmp_path
):
    from dbt_enhance.lifecycle import apply_remove, preview_remove

    project, request = setup_worker
    worker.run_query(request)
    active_outcome = []

    def execute_active():
        try:
            worker.run_query(replace(request, sql="sleep"))
        except EnhanceError as exc:
            active_outcome.append(exc)

    thread = threading.Thread(target=execute_active)
    thread.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        information = worker.inspect_worker(project)
        if information is not None and information["active"]:
            break
        time.sleep(0.01)
    else:
        pytest.fail("worker did not become active")

    plan = preview_remove(state_root=tmp_path / "lifecycle")
    commands = []
    admission_errors = []

    def runner(command, **_kwargs):
        commands.append(command)
        try:
            worker.run_query(request)
        except EnhanceError as exc:
            admission_errors.append(exc)
        else:
            pytest.fail("query entered while removal held exclusive admission")
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(EnhanceError, match="Active workers block"):
        apply_remove(plan, runner=runner)
    assert commands == []
    assert thread.is_alive(), "unconfirmed removal must not interrupt active work"
    assert worker.inspect_worker(project)["active"] is True

    result = apply_remove(plan, allow_active=True, runner=runner)
    assert result.ok
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert len(active_outcome) == 1
    assert active_outcome[0].cancellation == "requested_unverified"
    assert commands == [["uv", "tool", "uninstall", "dbt-enhance"]]
    assert len(admission_errors) == 1
    assert "removal is in progress" in str(admission_errors[0])
    assert worker.inspect_worker(project) is None


def test_worker_state_and_arguments_never_store_editor_secrets_or_sql(setup_worker, monkeypatch):
    project, request = setup_worker
    secret = "synthetic-secret-never-store"
    monkeypatch.setenv("DBT_ENV_SECRET_WORKER_TEST", secret)
    result = worker.run_query(replace(request, sql="argv"))
    directory, _ = worker._slot(project.resolve(), worker._Deadline(2), create=False)
    record_text = (directory / "control.json").read_text()
    record = json.loads(record_text)
    assert secret not in record_text
    assert secret not in result.rows[0][0]
    assert record["token"] not in result.rows[0][0]
    assert "synthetic-test-key" not in record_text
    assert (directory.stat().st_mode & 0o777) == 0o700
    assert ((directory / "control.json").stat().st_mode & 0o777) == 0o600
    assert ((directory / f"s-{record['instance']}").stat().st_mode & 0o777) == 0o600


def test_sso_progress_is_safe_and_timeout_actionable(setup_worker):
    _, request = setup_worker
    worker.run_query(request)
    progress = []
    with pytest.raises(EnhanceError) as error:
        worker.run_query(replace(request, sql="sso", timeout=0.6), on_status=progress.append)
    assert any("SSO" in value for value in progress)
    assert "dbt debug" in str(error.value)
    assert "DO_NOT_FORWARD" not in " ".join(progress) + str(error.value)


def test_inventory_blocks_malformed_owned_slot_with_live_socket(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    directory, _ = worker._slot(project, worker._Deadline(2), create=True)
    (directory / "control.json").write_text("{malformed")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(directory / "s-uninspectable"))
        listener.listen()
        with pytest.raises(EnhanceError, match="Package removal is blocked"):
            worker.inventory()
    finally:
        listener.close()


def test_private_state_rejects_symlinks_and_insecure_permissions(tmp_path):
    directory = tmp_path / "private"
    assert worker._private_directory(directory, create=True)
    link = tmp_path / "link"
    link.symlink_to(directory)
    with pytest.raises(EnhanceError, match="0700"):
        worker._private_directory(link, create=False)
    directory.chmod(0o755)
    with pytest.raises(EnhanceError, match="0700"):
        worker._private_directory(directory, create=False)
    real_file = tmp_path / "real"
    real_file.write_text("do not overwrite")
    linked_file = tmp_path / "linked"
    linked_file.symlink_to(real_file)
    with pytest.raises(OSError):
        worker._private_open(linked_file, os.O_RDONLY)
    assert real_file.read_text() == "do not overwrite"


def test_configuration_fifo_is_rejected_without_blocking(setup_worker):
    project, request = setup_worker
    (project / "packages.yml").unlink(missing_ok=True)
    os.mkfifo(project / "packages.yml")
    started = time.monotonic()
    with pytest.raises(EnhanceError, match="regular file"):
        worker.run_query(request)
    assert time.monotonic() - started < 1


async def test_worker_mode_keeps_ui_deadline_independent(setup_worker):
    from textual.widgets import Static

    from dbt_enhance.ui import QueryApp

    project, request = setup_worker
    worker.run_query(request)
    app = QueryApp(request=replace(request, sql="sleep", timeout=0.2))
    async with app.run_test() as pilot:
        await pilot.pause(0.5)
        assert "timed out" in str(app.query_one("#message", Static).render()).lower()
        assert not app.query_one("#spinner").display
        await pilot.press("q")
    assert worker.run_query(request).rows[0][1] == 1
