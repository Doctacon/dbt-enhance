from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from dbt_enhance._wire import MAX_REQUEST, PROTOCOL, Decoder, encode
from dbt_enhance._worker_helper import _Client, _Supervisor, watch_files

_ROOT = Path(__file__).parents[1]
_HELPER = _ROOT / "src/dbt_enhance/_worker_helper.py"
_WIRE = _ROOT / "src/dbt_enhance/_wire.py"
_TOKEN = "a" * 64
_PRIVATE_DIRS: list[Path] = []

_FAKE_SESSION = r"""
import os
import re
import time

_URL = re.compile(r"https?://[^\s]+")

class QueryError(RuntimeError):
    pass

class SessionError(RuntimeError):
    def __init__(self, message, *, reason="request_cleanup_failed"):
        super().__init__(message)
        self.reason = reason

def compatibility():
    if os.environ.get("FAKE_INCOMPATIBLE") == "1":
        raise RuntimeError("unsupported fake runtime")
    delay = float(os.environ.get("FAKE_COMPAT_DELAY", "0"))
    if delay:
        time.sleep(delay)
    if os.environ.get("FAKE_START_LOG") == "1":
        print("x" * (17 * 1024 * 1024), flush=True)
    return {"dbt_core": "fake", "python": "fake"}

def _scrub_error(error):
    return _URL.sub("<redacted URL>", str(error))

class DbtSession:
    def __init__(self, context, on_config=None):
        self.context = context
        self.on_config = on_config
        self.initialized = False
        self.count = 0
        self.closed = False

    def execute(self, sql, limit):
        if not self.initialized:
            self.initialized = True
            if self.on_config:
                self.on_config([os.path.join(self.context["project_dir"], "credential.pem")])
        self.count += 1
        if sql.startswith("sleep:"):
            time.sleep(float(sql.split(":", 1)[1]))
        if sql == "query-error":
            raise QueryError("deliberate query error")
        if sql == "cleanup-error":
            raise SessionError("deliberate cleanup error")
        if sql == "unsafe-reason":
            raise SessionError("safe display", reason="raw SQL and credentials")
        if sql == "sso-error":
            print(
                "Initiating login request with your identity provider "
                "https://login.example.test/a-secret",
                flush=True,
            )
            raise RuntimeError("authentication failed at https://login.example.test/a-secret")
        if sql == "huge-log":
            print("x" * (17 * 1024 * 1024), flush=True)
        return {
            "columns": ["value"],
            "rows": [[self.count]],
            "compiled_sql": sql,
            "warnings": [],
            "session_pid": os.getpid(),
            "limit": limit,
        }

    def close(self):
        self.closed = True
"""


@pytest.fixture(scope="session", autouse=True)
def _remove_short_socket_directories() -> Any:
    yield
    for directory in _PRIVATE_DIRS:
        shutil.rmtree(directory, ignore_errors=True)


def _copy_worker(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    # Darwin's sockaddr_un path limit is only 104 bytes; pytest's descriptive
    # temp paths routinely exceed it.
    private = Path("/tmp") / ("dbe-" + uuid.uuid4().hex[:12])
    private.mkdir(mode=0o700)
    _PRIVATE_DIRS.append(private)
    helper = project / "_worker_helper.py"
    shutil.copy2(_HELPER, helper)
    shutil.copy2(_WIRE, project / "_wire.py")
    (project / "_session.py").write_text(_FAKE_SESSION)
    return project, private, helper


def _bootstrap(project: Path, private: Path, instance: str, idle: float = 10.0) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "directory": str(private),
        "instance": instance,
        "token": _TOKEN,
        "context": {"project_dir": str(project), "profiles_dir": None, "target": None},
        "idle_seconds": idle,
    }


def _start(
    tmp_path: Path,
    *,
    idle: float = 10.0,
    environment: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[bytes], Path, Path, str]:
    project, private, helper = _copy_worker(tmp_path)
    instance = uuid.uuid4().hex
    env = os.environ.copy()
    env.update(environment or {})
    process = subprocess.Popen(
        [sys.executable, str(helper)],
        cwd=project,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps(_bootstrap(project, private, instance, idle)).encode())
    process.stdin.close()
    path = private / ("s-" + instance)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not path.exists() and process.poll() is None:
        time.sleep(0.01)
    return process, path, project, instance


def _receive(sock: socket.socket, decoder: Decoder, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = decoder.pop()
        if message is not None:
            return message
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        data = sock.recv(65536)
        if not data:
            raise EOFError("worker closed the connection")
        decoder.feed(data)
    raise TimeoutError("worker response timed out")


def _connect(path: Path, token: str = _TOKEN) -> tuple[socket.socket, Decoder, dict[str, Any]]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + 5
    while True:
        try:
            sock.connect(str(path))
            break
        except (FileNotFoundError, ConnectionRefusedError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    decoder = Decoder()
    sock.sendall(
        encode(
            {
                "type": "hello",
                "protocol": PROTOCOL,
                "instance": path.name[2:],
                "token": token,
            },
            MAX_REQUEST,
        )
    )
    message = _receive(sock, decoder)
    while message["type"] == "starting":
        message = _receive(sock, decoder)
    return sock, decoder, message


def _execute(
    sock: socket.socket,
    decoder: Decoder,
    sql: str,
    *,
    timeout: float = 2.0,
    limit: int = 10,
) -> list[dict[str, Any]]:
    request_id = uuid.uuid4().hex
    sock.sendall(
        encode(
            {
                "type": "execute",
                "request_id": request_id,
                "sql": sql,
                "limit": limit,
                "timeout": timeout,
            },
            MAX_REQUEST,
        )
    )
    messages: list[dict[str, Any]] = []
    while True:
        message = _receive(sock, decoder, timeout + 3)
        messages.append(message)
        if message["type"] in {"result", "error"}:
            return messages


def _stop(process: subprocess.Popen[bytes], path: Path) -> None:
    if process.poll() is not None:
        return
    try:
        sock, decoder, _ = _connect(path)
        sock.sendall(encode({"type": "stop"}, MAX_REQUEST))
        assert _receive(sock, decoder)["type"] == "stopped"
        sock.close()
    except (OSError, EOFError):
        process.terminate()
    process.wait(timeout=5)


def test_authentication_retained_session_busy_and_watches(tmp_path: Path) -> None:
    process, path, project, _ = _start(tmp_path)
    credential = project / "credential.pem"
    credential.write_text("first-secret")
    try:
        for invalid_token in ("b" * 64, "☃"):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as offender:
                offender.connect(str(path))
                offender.sendall(
                    encode(
                        {
                            "type": "hello",
                            "protocol": PROTOCOL,
                            "instance": path.name[2:],
                            "token": invalid_token,
                        },
                        MAX_REQUEST,
                    )
                )
                offender.settimeout(1)
                assert offender.recv(1) == b""
            assert process.poll() is None

        first, first_decoder, ready = _connect(path)
        assert ready["type"] == "ready"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert ready["session_pid"] != ready["pid"]
        request_id = uuid.uuid4().hex
        first.sendall(
            encode(
                {
                    "type": "execute",
                    "request_id": request_id,
                    "sql": "sleep:0.35",
                    "limit": 7,
                    "timeout": 2,
                },
                MAX_REQUEST,
            )
        )
        assert _receive(first, first_decoder) == {"type": "accepted", "request_id": request_id}
        assert _receive(first, first_decoder)["status"] == "running"

        busy, busy_decoder, _ = _connect(path)
        busy_messages = _execute(busy, busy_decoder, "select busy")
        assert busy_messages[-1]["message"] == "Worker is busy."
        busy.close()

        first_result = _receive(first, first_decoder)
        assert first_result["type"] == "result"
        assert first_result["result"]["rows"] == [[1]]
        first_digest = first_result["watched"][0]["digest"]
        assert "first-secret" not in json.dumps(first_result)
        session_pid = first_result["result"]["session_pid"]
        first.close()

        credential.write_text("second-secret")
        second, second_decoder, _ = _connect(path)
        second_result = _execute(second, second_decoder, "select 2")[-1]
        assert second_result["type"] == "result"
        assert second_result["result"]["rows"] == [[2]]
        assert second_result["result"]["session_pid"] == session_pid
        # Configuration is resolved only on lazy initialization, so the watch
        # snapshot remains the baseline associated with this retained session.
        assert second_result["watched"][0]["digest"] == first_digest
        second.close()
    finally:
        _stop(process, path)


def test_recoverable_query_error_keeps_session_and_credential_snapshot(tmp_path: Path) -> None:
    process, path, project, _ = _start(tmp_path)
    credential = project / "credential.pem"
    credential.write_text("first-secret")
    try:
        first, first_decoder, ready = _connect(path)
        session_pid = ready["session_pid"]
        error = _execute(first, first_decoder, "query-error")[-1]
        assert error["type"] == "error"
        assert error["message"] == "deliberate query error"
        assert error["reusable"] is True
        assert error["sso"] is False
        assert len(error["watched"]) == 1
        assert "first-secret" not in json.dumps(error)
        first.close()

        second, second_decoder, second_ready = _connect(path)
        result = _execute(second, second_decoder, "select after error")[-1]
        assert result["type"] == "result"
        assert result["result"]["session_pid"] == session_pid
        assert second_ready["session_pid"] == session_pid
        assert result["result"]["rows"] == [[2]]
        assert result["watched"] == error["watched"]
        second.close()
        assert process.poll() is None
    finally:
        _stop(process, path)


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("cleanup-error", "request_cleanup_failed"),
        ("unsafe-reason", "session_error"),
    ],
)
def test_session_error_reason_is_validated_and_forwarded(
    tmp_path: Path, sql: str, reason: str
) -> None:
    process, path, _, _ = _start(tmp_path)
    sock, decoder, _ = _connect(path)
    error = _execute(sock, decoder, sql)[-1]
    assert error["type"] == "error"
    assert error.get("reusable") is not True
    assert error["retirement_reason"] == reason
    assert "raw SQL and credentials" not in json.dumps(error)
    process.wait(timeout=5)


@pytest.mark.parametrize(
    "message",
    [
        {
            "type": "error",
            "request_id": "b" * 32,
            "message": "wrong request",
            "reusable": True,
            "watched": [],
        },
        {
            "type": "error",
            "request_id": "a" * 32,
            "message": "bad snapshot",
            "reusable": True,
            "watched": [{"path": "/tmp/key", "digest": "bad"}],
        },
        {
            "type": "error",
            "request_id": "a" * 32,
            "message": "not explicitly reusable",
            "reusable": 1,
            "watched": [],
        },
    ],
)
def test_malformed_recoverable_error_retires_supervisor(
    tmp_path: Path, message: dict[str, Any]
) -> None:
    supervisor = _Supervisor(tmp_path, "c" * 32, _TOKEN, {}, 10)
    server, peer = socket.socketpair()
    client = _Client(server, authenticated=True)
    supervisor.state = "active"
    supervisor.active = client
    supervisor.request_id = "a" * 32
    try:
        supervisor._child_message(message, time.monotonic())
        assert supervisor.state == "retiring"
        assert client.result_pending is False
    finally:
        server.close()
        peer.close()
        supervisor.selector.close()


def test_watch_files_is_keyed_bounded_and_tracks_existence(tmp_path: Path) -> None:
    path = tmp_path / "key.pem"
    missing = watch_files([str(path)], _TOKEN)
    path.write_text("private material")
    present = watch_files([str(path)], _TOKEN)
    changed_token = watch_files([str(path)], "b" * 64)
    assert missing != present != changed_token
    assert "private material" not in json.dumps(present)
    assert len(present[0]["digest"]) == 64


def test_credential_symlink_rotation_and_fifo_are_bounded(tmp_path: Path) -> None:
    first, second, alias = (tmp_path / name for name in ("first", "second", "alias"))
    first.write_text("synthetic-first-key")
    second.write_text("synthetic-second-key")
    alias.symlink_to(first)
    before = watch_files([str(alias)], _TOKEN)
    alias.unlink()
    alias.symlink_to(second)
    assert watch_files([str(alias)], _TOKEN) != before
    alias.unlink()
    os.mkfifo(alias)
    # A regression to blocking open must fail this test, not hang the test runner.
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from dbt_enhance._worker_helper import watch_files; "
            f"print(watch_files([{str(alias)!r}], {_TOKEN!r}))",
        ],
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("action", ["stop", "timeout"])
def test_supervisor_remains_responsive_while_child_cannot_read(tmp_path: Path, action: str):
    process, path, _, _ = _start(tmp_path)
    active, decoder, ready = _connect(path)
    child_pid = ready["session_pid"]
    os.kill(child_pid, signal.SIGSTOP)
    try:
        request_id = uuid.uuid4().hex
        active.sendall(
            encode(
                {
                    "type": "execute",
                    "request_id": request_id,
                    "sql": "x" * (1024 * 1024),
                    "limit": 1,
                    "timeout": 20 if action == "stop" else 0.15,
                },
                MAX_REQUEST,
            )
        )
        started = time.monotonic()
        assert _receive(active, decoder)["type"] == "accepted"
        if action == "stop":
            stopper, stop_decoder, _ = _connect(path)
            with stopper:
                stopper.sendall(encode({"type": "stop"}, MAX_REQUEST))
                assert _receive(stopper, stop_decoder)["type"] == "stopped"
        else:
            while True:
                message = _receive(active, decoder)
                if message["type"] == "error":
                    assert "timed out" in message["message"]
                    break
        process.wait(timeout=5)
        assert time.monotonic() - started < 3
    finally:
        active.close()
        if process.poll() is None:
            # Unfreeze on assertion failure so even the old blocking-send bug
            # cannot strand a stopped fixture process.
            try:
                os.kill(child_pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            _stop(process, path)


def test_sso_progress_and_errors_never_forward_raw_url(tmp_path: Path) -> None:
    process, path, _, _ = _start(tmp_path)
    try:
        sock, decoder, _ = _connect(path)
        messages = _execute(sock, decoder, "sso-error")
        assert messages[0]["type"] == "accepted"
        assert any(item.get("status") == "sso" for item in messages)
        error = messages[-1]
        assert error["type"] == "error"
        assert error["sso"] is True
        assert "https://" not in json.dumps(messages)
        process.wait(timeout=5)
    finally:
        _stop(process, path)


def test_child_output_cap_retires_worker(tmp_path: Path) -> None:
    process, path, _, _ = _start(tmp_path)
    sock, decoder, _ = _connect(path)
    messages = _execute(sock, decoder, "huge-log", timeout=5)
    assert messages[-1]["type"] == "error"
    assert "16 MiB" in messages[-1]["message"]
    process.wait(timeout=5)


def test_timeout_and_stop_kill_the_owned_session_group(tmp_path: Path) -> None:
    process, path, _, _ = _start(tmp_path)
    sock, decoder, ready = _connect(path)
    session_pid = ready["session_pid"]
    messages = _execute(sock, decoder, "sleep:10", timeout=0.15)
    assert messages[-1]["type"] == "error"
    assert "timed out" in messages[-1]["message"]
    process.wait(timeout=5)
    with pytest.raises(ProcessLookupError):
        os.kill(session_pid, 0)

    process2, path2, _, _ = _start(tmp_path / "second")
    active, active_decoder, _ = _connect(path2)
    request_id = uuid.uuid4().hex
    active.sendall(
        encode(
            {
                "type": "execute",
                "request_id": request_id,
                "sql": "sleep:10",
                "limit": 1,
                "timeout": 20,
            },
            MAX_REQUEST,
        )
    )
    assert _receive(active, active_decoder)["type"] == "accepted"
    stopper, stopper_decoder, _ = _connect(path2)
    stopper.sendall(encode({"type": "stop"}, MAX_REQUEST))
    assert _receive(stopper, stopper_decoder)["type"] == "stopped"
    assert _receive(active, active_decoder)["type"] in {"progress", "error"}
    if process2.poll() is None:
        process2.wait(timeout=5)


def test_active_client_eof_retires_worker_but_idle_health_eof_does_not(tmp_path: Path) -> None:
    process, path, _, _ = _start(tmp_path)
    health, _, _ = _connect(path)
    health.close()
    time.sleep(0.1)
    assert process.poll() is None

    active, decoder, ready = _connect(path)
    child_pid = ready["session_pid"]
    active.sendall(
        encode(
            {
                "type": "execute",
                "request_id": uuid.uuid4().hex,
                "sql": "sleep:10",
                "limit": 1,
                "timeout": 20,
            },
            MAX_REQUEST,
        )
    )
    assert _receive(active, decoder)["type"] == "accepted"
    active.close()
    process.wait(timeout=5)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_idle_exit_and_startup_incompatibility_file(tmp_path: Path) -> None:
    process, path, _, _ = _start(tmp_path, idle=0.15)
    sock, _, _ = _connect(path)
    sock.close()
    process.wait(timeout=5)
    assert not path.exists()

    process2, _, _, instance = _start(
        tmp_path / "incompatible", environment={"FAKE_INCOMPATIBLE": "1"}
    )
    process2.wait(timeout=5)
    candidates = [directory / ("e-" + instance) for directory in _PRIVATE_DIRS]
    error_path = next(candidate for candidate in candidates if candidate.exists())
    payload = json.loads(error_path.read_text())
    assert "unsupported fake runtime" in payload["error"]
    assert stat.S_IMODE(error_path.stat().st_mode) == 0o600


def test_stop_works_during_native_imports(tmp_path: Path):
    process, path, _, _ = _start(tmp_path, environment={"FAKE_COMPAT_DELAY": "10"})
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(path))
            sock.sendall(
                encode(
                    {
                        "type": "hello",
                        "protocol": PROTOCOL,
                        "instance": path.name[2:],
                        "token": _TOKEN,
                    }
                )
            )
            decoder = Decoder()
            assert _receive(sock, decoder)["type"] == "starting"
            sock.sendall(encode({"type": "stop"}))
            assert _receive(sock, decoder)["type"] == "stopped"
        process.wait(timeout=3)
    finally:
        _stop(process, path)


def test_startup_output_limit_reports_an_authenticated_error(tmp_path: Path):
    process, path, _, _ = _start(
        tmp_path,
        environment={
            "FAKE_COMPAT_DELAY": "0.15",
            "FAKE_START_LOG": "1",
        },
    )
    try:
        sock, _, response = _connect(path)
        sock.close()
        assert response["type"] == "error"
        assert "16 MiB" in response["message"]
        process.wait(timeout=3)
    finally:
        _stop(process, path)


def test_malformed_oversized_and_partial_authenticated_frames_retire(tmp_path: Path) -> None:
    process, path, _, _ = _start(tmp_path)
    sock, decoder, _ = _connect(path)
    sock.sendall((MAX_REQUEST + 1).to_bytes(4, "big"))
    assert _receive(sock, decoder)["type"] == "error"
    process.wait(timeout=5)

    process2, path2, _, _ = _start(tmp_path / "partial")
    sock2, decoder2, _ = _connect(path2)
    malformed = encode({"type": "execute"}, MAX_REQUEST)
    # Exercise incremental framing rather than relying on packet boundaries.
    for byte in malformed:
        sock2.send(bytes([byte]))
    assert _receive(sock2, decoder2)["type"] == "error"
    process2.wait(timeout=5)

    process3, path3, _, _ = _start(tmp_path / "surrogate")
    sock3, decoder3, _ = _connect(path3)
    payload = json.dumps(
        {
            "type": "execute",
            "request_id": uuid.uuid4().hex,
            "sql": "\ud800",
            "limit": 1,
            "timeout": 1,
        }
    ).encode()
    sock3.sendall(len(payload).to_bytes(4, "big") + payload)
    assert _receive(sock3, decoder3)["type"] == "error"
    process3.wait(timeout=5)


def test_private_directory_permissions_are_required(tmp_path: Path) -> None:
    project, private, helper = _copy_worker(tmp_path)
    private.chmod(0o755)
    instance = uuid.uuid4().hex
    completed = subprocess.run(
        [sys.executable, str(helper)],
        cwd=project,
        input=json.dumps(_bootstrap(project, private, instance)).encode(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 1
    assert not (private / ("s-" + instance)).exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux parent-death signal is deterministic")
def test_session_dies_when_supervisor_is_killed(tmp_path: Path) -> None:
    process, path, _, _ = _start(tmp_path)
    sock, _, ready = _connect(path)
    child_pid = ready["session_pid"]
    os.kill(process.pid, signal.SIGKILL)
    process.wait(timeout=5)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
            # Container PID 1 may defer reaping an already-dead child. A zombie
            # owns no connection/file descriptors and is not a live session.
            state = Path(f"/proc/{child_pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state == "Z":
                break
        except (ProcessLookupError, FileNotFoundError):
            break
        time.sleep(0.05)
    else:
        pytest.fail("native session survived its supervisor")
    sock.close()
