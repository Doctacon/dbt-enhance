"""Opt-in, project-scoped warm worker client; native dbt stays in its own Python.

The project lock covers a complete request. Context changes retire the old worker
rather than creating competing workers writing the same dbt target. A request is
never replayed after sending SQL, and this module never kills a PID from a file.
"""

from __future__ import annotations

import ast
import contextlib
import fcntl
import hmac
import json
import math
import os
import re
import secrets
import select
import socket
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from ._wire import (
    MAX_REQUEST,
    PROTOCOL,
    Decoder,
    WireError,
    encode,
    validate_retirement_reason,
)
from .dbt import (
    _child_environment,
    _notify,
    _preview_sql,
    _terminate_process_group,
    _validate_request,
    discover_project,
    find_dbt,
)
from .models import (
    SSO_GUIDANCE,
    SSO_STATUS,
    EnhanceError,
    QueryRequest,
    QueryResult,
    QueryStatus,
)

_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_STATE_LIMIT = 64 * 1024
_POLL = 0.05
_ADMISSION_FILE = "admission"
_ADMISSION_BYTES = 65
_ADMISSION_ERROR = "Package removal is in progress; retry the query after removal finishes."
_HELPER = Path(__file__).with_name("_worker_helper.py")
# These are ordinary pip/uv console-script bodies, not arbitrary Python wrappers.
_ENTRY_POINTS = (
    "import sys\nfrom dbt.cli.main import cli\nif __name__ == '__main__':\n    sys.exit(cli())\n",
    "import re\nimport sys\nfrom dbt.cli.main import cli\n"
    "if __name__ == '__main__':\n"
    "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
    "    sys.exit(cli())\n",
    "import sys\nfrom dbt.cli.main import cli\n"
    "if __name__ == '__main__':\n"
    "    if sys.argv[0].endswith('-script.pyw'):\n"
    "        sys.argv[0] = sys.argv[0][:-11]\n"
    "    elif sys.argv[0].endswith('.exe'):\n"
    "        sys.argv[0] = sys.argv[0][:-4]\n"
    "    sys.exit(cli())\n",
)
_ENTRY_TREES = {ast.dump(ast.parse(source)) for source in _ENTRY_POINTS}


class _Deadline:
    def __init__(self, seconds: float, cancel: threading.Event | None = None) -> None:
        self.seconds = seconds
        self.end = time.monotonic() + seconds
        self.cancel = cancel or threading.Event()
        self.sso = False
        self.execution_started = False
        self.cancellation: str | None = None
        self.failure_reason: str | None = None

    def remaining(self) -> float:
        if self.cancel.is_set():
            error = EnhanceError(
                "Query was cancelled.",
                code="cancelled",
                stage="execution" if self.execution_started else "preflight",
            )
            if self.execution_started:
                self.cancellation = "requested_unverified"
                self.failure_reason = "client_disconnect"
                error.cancellation = self.cancellation
            raise error
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            message = f"dbt query timed out after {self.seconds:g} seconds."
            if self.sso:
                message += "\n" + SSO_GUIDANCE
            error = EnhanceError(
                message,
                code="timeout",
                stage="execution" if self.execution_started else "preflight",
            )
            if self.execution_started:
                self.cancellation = "requested_unverified"
                self.failure_reason = "timeout"
                error.cancellation = self.cancellation
            raise error
        return remaining

    def pause(self) -> None:
        self.cancel.wait(min(_POLL, self.remaining()))


def _private_directory(path: Path, *, create: bool) -> bool:
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise EnhanceError(
            f"Worker directory must be an owned, non-symlink directory with mode 0700: {path}"
        )
    return True


def _runtime_root(*, create: bool) -> Path | None:
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and len(os.fsencode(xdg)) < 35:
        base = Path(xdg)
        if not base.is_absolute() or not _private_directory(base, create=False):
            raise EnhanceError("XDG_RUNTIME_DIR must be an existing private absolute directory.")
        root = base / "dbt-enhance"
    else:
        # A short path also fits Darwin's 104-byte Unix-domain socket limit.
        root = Path("/tmp") / f"dbt-enhance-{os.getuid()}"
    return root if _private_directory(root, create=create) else None


def _private_open(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        os.close(fd)
        raise EnhanceError(f"Worker state must be an owned regular file with mode 0600: {path}")
    return fd


@contextlib.contextmanager
def _lock(path: Path, deadline: _Deadline) -> Iterator[None]:
    fd = _private_open(path, os.O_RDWR | os.O_CREAT)
    try:
        while True:
            deadline.remaining()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                deadline.pause()
        yield
    finally:
        os.close(fd)


def _admission_epoch(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    data = os.read(fd, _ADMISSION_BYTES + 1)
    if (
        len(data) != _ADMISSION_BYTES
        or data[-1:] != b"\n"
        or _HEX64.fullmatch(data[:-1].decode("ascii", "ignore")) is None
    ):
        raise EnhanceError(
            "Invalid worker admission registry; inspect the private runtime directory."
        )
    return data[:-1].decode("ascii")


def _write_admission_epoch(fd: int) -> None:
    data = memoryview((secrets.token_hex(32) + "\n").encode("ascii"))
    os.lseek(fd, 0, os.SEEK_SET)
    while data:
        written = os.write(fd, data)
        if written <= 0:
            raise OSError("Could not update worker admission registry.")
        data = data[written:]
    os.ftruncate(fd, _ADMISSION_BYTES)
    os.fsync(fd)


def _ensure_admission_locked(root: Path) -> int:
    """Open the persistent lock inode while the registry lock is held."""
    fd = _private_open(root / _ADMISSION_FILE, os.O_RDWR | os.O_CREAT)
    try:
        if os.fstat(fd).st_size == 0:
            _write_admission_epoch(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_admission(deadline: _Deadline) -> int:
    root = _runtime_root(create=True)
    assert root is not None
    with _lock(root / "lock", deadline):
        return _ensure_admission_locked(root)


def _admission_snapshot(deadline: _Deadline) -> str:
    fd = _open_admission(deadline)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise EnhanceError(_ADMISSION_ERROR, stage="preflight") from None
        return _admission_epoch(fd)
    finally:
        os.close(fd)


def _admit(expected: str, deadline: _Deadline) -> int:
    """Acquire shared admission without waiting behind package removal."""
    deadline.remaining()
    fd = _open_admission(deadline)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise EnhanceError(_ADMISSION_ERROR, stage="preflight") from None
        if _admission_epoch(fd) != expected:
            raise EnhanceError(
                "This queued query predates package removal and was not executed; retry it.",
                stage="preflight",
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


class _RemovalGuard:
    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._invalidated = False

    def invalidate(self) -> None:
        """Reject requests queued before this removal reached irreversible effects."""
        if self._invalidated:
            return
        _write_admission_epoch(self._fd)
        self._invalidated = True


@contextlib.contextmanager
def removal_guard(*, seconds: float = 2.0) -> Iterator[_RemovalGuard]:
    """Exclude new query admission for a bounded package-removal critical section.

    Accepted queries hold no registry lock while executing, so callers can inventory
    and either preserve them or explicitly reset them while this guard is held.
    """
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not math.isfinite(float(seconds))
        or seconds <= 0
    ):
        raise ValueError("removal guard timeout must be a positive finite number")
    deadline = _Deadline(float(seconds))
    fd = _open_admission(deadline)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline.end:
                    raise EnhanceError(
                        "Package removal could not start because a query is still being admitted; "
                        "retry removal after worker startup finishes."
                    ) from None
                time.sleep(min(_POLL, max(0.0, deadline.end - time.monotonic())))
        _admission_epoch(fd)
        yield _RemovalGuard(fd)
    finally:
        os.close(fd)


def _read(path: Path) -> bytes:
    with os.fdopen(_private_open(path, os.O_RDONLY), "rb") as handle:
        data = handle.read(_STATE_LIMIT + 1)
    if len(data) > _STATE_LIMIT:
        raise EnhanceError("Worker state exceeded its safety limit.")
    return data


def _write_state(path: Path, value: dict[str, Any]) -> None:
    data = json.dumps(value).encode("utf-8")
    if len(data) > _STATE_LIMIT:
        raise EnhanceError("Worker state exceeded its safety limit.")
    temporary = path.with_name(f".state-{secrets.token_hex(8)}")
    try:
        with os.fdopen(
            _private_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb"
        ) as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _slot(project: Path, deadline: _Deadline, *, create: bool) -> tuple[Path, bytes] | None:
    root = _runtime_root(create=create)
    if root is None:
        return None
    with _lock(root / "lock", deadline):
        admission_fd = _ensure_admission_locked(root)
        os.close(admission_fd)
        key_path = root / "key"
        try:
            key = _read(key_path)
        except FileNotFoundError:
            if not create:
                return None
            key = secrets.token_bytes(32)
            with os.fdopen(
                _private_open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb"
            ) as handle:
                handle.write(key)
        if len(key) != 32:
            raise EnhanceError("Invalid private worker key; inspect the worker runtime directory.")
        # Status/stop may receive a symlink spelling (e.g. /tmp on macOS),
        # while discovery returns the canonical project root. They share a slot.
        name = hmac.digest(key, os.fsencode(project.resolve()), "sha256").hex()[:16]
        directory = root / name
        if not _private_directory(directory, create=create):
            return None
    return directory, key


def project_python(dbt: Path) -> Path:
    """Recognize a console script without executing/ignoring a custom wrapper."""
    try:
        with dbt.open("rb") as handle:
            raw = handle.read(16385)
        if len(raw) > 16384:
            raise ValueError
        source = raw.decode("utf-8")
        first = source.splitlines()[0]
        interpreter = Path(first[2:])
        if (
            not first.startswith("#!/")
            or not interpreter.is_file()
            or not os.access(interpreter, os.X_OK)
        ):
            raise ValueError
        if ast.dump(ast.parse(source)) not in _ENTRY_TREES:
            raise ValueError
    except (OSError, ValueError, SyntaxError, IndexError):
        raise EnhanceError(
            "Worker mode requires an ordinary dbt Core Python console script with an absolute "
            "interpreter path. Use --backend process for shell/custom wrappers.",
            code="unsupported_environment",
            stage="preflight",
        ) from None
    # Resolving this symlink would discard its virtualenv and import the wrong dbt.
    return interpreter.absolute()


def _valid_last_retirement(value: object) -> dict[str, Any] | None:
    if (
        not isinstance(value, dict)
        or not set(value).issubset({"reason", "session_pid", "cancellation"})
        or not {"reason", "session_pid"}.issubset(value)
    ):
        return None
    reason = validate_retirement_reason(value.get("reason"))
    session_pid = value.get("session_pid")
    if (
        reason is None
        or not isinstance(session_pid, int)
        or isinstance(session_pid, bool)
        or session_pid <= 0
    ):
        return None
    result: dict[str, Any] = {"reason": reason, "session_pid": session_pid}
    cancellation = value.get("cancellation")
    if cancellation is not None:
        if cancellation != "requested_unverified":
            return None
        result["cancellation"] = cancellation
    return result


def _record(directory: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(_read(directory / "control.json"))
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError) as exc:
        raise EnhanceError("Invalid worker state; inspect the private runtime directory.") from exc
    if (
        not isinstance(data, dict)
        or data.get("protocol") != PROTOCOL
        or not isinstance(data.get("instance"), str)
        or not _HEX32.fullmatch(data["instance"])
        or not isinstance(data.get("token"), str)
        or not _HEX64.fullmatch(data["token"])
        or not isinstance(data.get("generation"), str)
        or not _HEX64.fullmatch(data["generation"])
    ):
        raise EnhanceError(
            "Incompatible or malformed worker state; use the prior version to stop it."
        )
    last_retirement = _valid_last_retirement(data.get("last_retirement"))
    if last_retirement is None:
        data.pop("last_retirement", None)
    else:
        data["last_retirement"] = last_retirement
    return data


def _persist_retirement(
    directory: Path,
    record: dict[str, Any],
    retirement_reason: object,
    cancellation: object = None,
) -> None:
    """Persist only fixed retirement and cancellation certainty codes."""
    session_pid = record.get("session_pid")
    if not isinstance(session_pid, int) or isinstance(session_pid, bool) or session_pid <= 0:
        return
    reason = validate_retirement_reason(retirement_reason) or "worker_failure"
    retired: dict[str, Any] = {"reason": reason, "session_pid": session_pid}
    if cancellation == "requested_unverified":
        retired["cancellation"] = cancellation
    record["last_retirement"] = retired
    _write_state(directory / "control.json", record)


def _stat_stamp(path: Path) -> tuple[int, int, int] | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_ino, info.st_size, info.st_mtime_ns


def _generation(
    project: Path,
    dbt: Path,
    interpreter: Path,
    request: QueryRequest,
    environment: dict[str, str],
    key: bytes,
) -> str:
    files = {
        project / name
        for name in (
            "dbt_project.yml",
            "packages.yml",
            "dependencies.yml",
            "package-lock.yml",
            "selectors.yml",
            "catalogs.yml",
            "vars.yml",
        )
    }
    if environment.get("PYTHON_DOTENV_DISABLED", "").lower() not in {"1", "true", "yes"}:
        for parent in (project, *project.parents):
            candidate = parent / ".env"
            files.add(candidate)
            if candidate.is_file():
                break
    if request.profiles_dir is not None:
        profile_dirs = [request.profiles_dir]
    else:
        profile_dirs = [
            Path(environment[name])
            for name in ("DBT_PROFILES_DIR", "DBT_ENGINE_PROFILES_DIR")
            if environment.get(name)
        ]
        if not profile_dirs:
            profile_dirs = [project, Path.home() / ".dbt"]
    for directory in profile_dirs:
        directory = directory.expanduser()
        if not directory.is_absolute():
            directory = project / directory
        files.add(directory / "profiles.yml")
    for name in ("_worker_helper.py", "_session.py", "_wire.py", "worker.py"):
        files.add(Path(__file__).with_name(name))
    digest = hmac.new(key, digestmod="sha256")
    digest.update(
        json.dumps(
            {
                "protocol": PROTOCOL,
                "project": str(project),
                "dbt": str(dbt),
                "python": str(interpreter),
                "target": request.target,
                "profiles_dir": str(request.profiles_dir) if request.profiles_dir else None,
                "allowed_targets": request.allowed_targets,
                "worker_idle_seconds": request.worker_idle_seconds,
                "environment": environment,
            },
            sort_keys=True,
        ).encode()
    )
    for path in sorted(files):
        digest.update(os.fsencode(path) + b"\0")
        present = True
        stamp = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise EnhanceError(f"Worker configuration must be a regular file: {path}")
                stamp = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
                data = handle.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise EnhanceError("Worker configuration file exceeds the 4 MiB safety limit.")
        except FileNotFoundError:
            present, data = False, b""
        # Detect edits restored to their old contents during startup too; the
        # long-lived config may have read the intervening version.
        digest.update(json.dumps(stamp).encode() + b"\0")
        digest.update(bytes([present]) + len(data).to_bytes(8, "big") + data)
    venv = interpreter.parent.parent
    packages = sorted(venv.glob("lib/python*/site-packages/*.dist-info/METADATA"))
    for path in [dbt, interpreter, venv / "pyvenv.cfg", *packages]:
        digest.update(json.dumps((str(path), _stat_stamp(path))).encode())
    return digest.hexdigest()


def _send(sock: socket.socket, message: dict[str, Any], deadline: _Deadline) -> None:
    data = memoryview(encode(message, MAX_REQUEST))
    while data:
        timeout = min(_POLL, deadline.remaining())
        if not select.select([], [sock], [], timeout)[1]:
            continue
        try:
            count = sock.send(data)
        except BlockingIOError:
            continue
        if count == 0:
            raise ConnectionError("Worker connection closed.")
        data = data[count:]


def _receive(sock: socket.socket, decoder: Decoder, deadline: _Deadline) -> dict[str, Any]:
    while True:
        deadline.remaining()
        message = decoder.pop()
        if message is not None:
            return message
        if not select.select([sock], [], [], min(_POLL, deadline.remaining()))[0]:
            continue
        try:
            data = sock.recv(65536)
        except BlockingIOError:
            continue
        if not data:
            raise ConnectionError("Worker connection closed; the query was not retried.")
        decoder.feed(data)


def _connect(directory: Path, record: dict[str, Any]) -> socket.socket:
    path = directory / f"s-{record['instance']}"
    info = path.lstat()
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise EnhanceError("Worker socket is not a private, owned socket.")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.2)
        sock.connect(str(path))
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


def _hello(sock: socket.socket, record: dict[str, Any], deadline: _Deadline) -> None:
    _send(
        sock,
        {
            "type": "hello",
            "protocol": PROTOCOL,
            "instance": record["instance"],
            "token": record["token"],
        },
        deadline,
    )


def _ready(
    sock: socket.socket, record: dict[str, Any], deadline: _Deadline
) -> tuple[dict[str, Any], Decoder]:
    decoder = Decoder()
    _hello(sock, record, deadline)
    while True:
        message = _receive(sock, decoder, deadline)
        if message.get("type") == "starting":
            continue
        if message.get("type") == "error":
            raise EnhanceError(str(message.get("message", "Worker could not start.")))
        if (
            message.get("type") != "ready"
            or message.get("protocol") != PROTOCOL
            or message.get("instance") != record["instance"]
        ):
            raise EnhanceError("Worker handshake did not match this installation.")
        return message, decoder


def _stop_record(directory: Path, record: dict[str, Any], seconds: float = 2.0) -> bool:
    deadline = _Deadline(seconds)
    try:
        sock = _connect(directory, record)
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    with sock:
        _hello(sock, record, deadline)
        # Stop is accepted after authentication even while the session imports dbt.
        _send(sock, {"type": "stop"}, deadline)
        decoder = Decoder()
        while True:
            response = _receive(sock, decoder, deadline)
            if response.get("type") == "stopped":
                retired = _valid_last_retirement(record.get("last_retirement"))
                if response.get("cancellation") == "requested_unverified" and (
                    retired is None
                    or retired.get("session_pid") != record.get("session_pid")
                    or retired.get("cancellation") is None
                ):
                    _persist_retirement(
                        directory,
                        record,
                        "worker_failure",
                        "requested_unverified",
                    )
                return True
            if response.get("type") == "error":
                raise EnhanceError(str(response.get("message", "Worker could not stop.")))


def _spawn(
    directory: Path,
    generation: str,
    context: dict[str, Any],
    interpreter: Path,
    environment: dict[str, str],
    deadline: _Deadline,
    idle_seconds: float,
    last_retirement: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], subprocess.Popen]:
    record = {
        "protocol": PROTOCOL,
        "instance": secrets.token_hex(16),
        "token": secrets.token_hex(32),
        "generation": generation,
        "project": context["project_dir"],
        "watched": [],
    }
    validated_retirement = _valid_last_retirement(last_retirement)
    if validated_retirement is not None:
        record["last_retirement"] = validated_retirement
    bootstrap = json.dumps(
        {
            "protocol": PROTOCOL,
            "instance": record["instance"],
            "token": record["token"],
            "directory": str(directory),
            "context": context,
            "idle_seconds": idle_seconds,
        }
    ).encode()
    if len(bootstrap) > _STATE_LIMIT:
        raise EnhanceError("Worker startup context exceeds its safety limit.")
    process = subprocess.Popen(
        [str(interpreter), str(_HELPER)],
        cwd=context["project_dir"],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        bufsize=0,
    )
    try:
        assert process.stdin is not None
        os.set_blocking(process.stdin.fileno(), False)
        pending = memoryview(bootstrap)
        while pending:
            deadline.remaining()
            if select.select([], [process.stdin], [], min(_POLL, deadline.remaining()))[1]:
                try:
                    count = os.write(process.stdin.fileno(), pending)
                except BlockingIOError:
                    continue
                pending = pending[count:]
        process.stdin.close()
        record["pid"] = process.pid
        _write_state(directory / "control.json", record)
    except BaseException:
        if process.stdin is not None:
            process.stdin.close()
        with contextlib.suppress(Exception):
            _terminate_process_group(process)
        raise
    return record, process


def _open_worker(
    directory: Path, record: dict[str, Any], process: subprocess.Popen | None, deadline: _Deadline
) -> tuple[socket.socket, dict[str, Any], Decoder]:
    while True:
        deadline.remaining()
        try:
            sock = _connect(directory, record)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            if process is None:
                raise
            try:
                error = json.loads(_read(directory / f"e-{record['instance']}"))
            except FileNotFoundError:
                error = None
            if error is not None:
                raise EnhanceError(str(error.get("error", "Worker failed to start."))) from None
            if process.poll() is not None:
                raise EnhanceError(
                    "Worker failed to start. Use --backend process for this environment."
                ) from None
            deadline.pause()
    try:
        ready, decoder = _ready(sock, record, deadline)
        return sock, ready, decoder
    except BaseException:
        sock.close()
        raise


def _valid_watched(watched: Any) -> bool:
    return (
        isinstance(watched, list)
        and len(watched) <= 128
        and all(
            isinstance(item, dict)
            and set(item) == {"path", "digest"}
            and isinstance(item["path"], str)
            and isinstance(item["digest"], str)
            and _HEX64.fullmatch(item["digest"]) is not None
            for item in watched
        )
    )


def _watched_unchanged(record: dict[str, Any]) -> bool:
    from ._worker_helper import watch_files

    watched = record.get("watched", [])
    if not _valid_watched(watched):
        raise EnhanceError("Invalid worker credential-file state.")
    return watch_files([item["path"] for item in watched], record["token"]) == watched


def _worker_environment(project: Path) -> dict[str, str]:
    """Use one effective environment for both worker identity and execution.

    Drop only launcher bookkeeping, not a prefix allowlist: profiles and Jinja
    can depend on arbitrary environment variables, including credentials.
    Terminal state belongs to the UI, not the detached dbt child.
    """
    environment = _child_environment()
    for name in (
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
        environment.pop(name, None)
    # Popen starts the worker in this directory, regardless of the caller's cwd.
    environment["PWD"] = str(project)
    return environment


def run_query(
    request: QueryRequest,
    cancel: threading.Event | None = None,
    on_status: Callable[[str], None] | None = None,
) -> QueryResult:
    _validate_request(request)
    if request.allowed_targets is not None and (
        not isinstance(request.allowed_targets, (tuple, list))
        or not all(isinstance(item, str) for item in request.allowed_targets)
    ):
        raise EnhanceError("Allowed targets must be a sequence of target names or None.")
    if (
        isinstance(request.worker_idle_seconds, bool)
        or not isinstance(request.worker_idle_seconds, (int, float))
        or not math.isfinite(float(request.worker_idle_seconds))
        or request.worker_idle_seconds <= 0
    ):
        raise EnhanceError("Worker idle timeout must be a positive, finite number of seconds.")
    started = time.monotonic()
    deadline = _Deadline(float(request.timeout), cancel)
    deadline.remaining()
    # Linearize this request against removal before project discovery or any
    # potentially slow preflight. The epoch is checked again after queueing.
    admission_epoch = _admission_snapshot(deadline)
    project = discover_project(request.file)
    dbt = find_dbt(project, request.dbt)
    interpreter = project_python(dbt)
    environment = _worker_environment(project)
    context = {
        "project_dir": str(project),
        "target": request.target,
        "profiles_dir": str((project / request.profiles_dir.expanduser()).absolute())
        if request.profiles_dir is not None
        else None,
        "allowed_targets": (
            list(request.allowed_targets) if request.allowed_targets is not None else None
        ),
    }
    sock = None
    record = None
    directory = None
    execution_sent = False
    session_reusable = False
    process = None
    last_retirement = None

    def finish() -> None:
        if sock is not None:
            sock.close()
        if (
            not session_reusable
            and record is not None
            and directory is not None
            and (execution_sent or process is not None)
        ):
            # Finish retirement before releasing the project lock. Otherwise the
            # next query could race a dying worker or receive a spurious busy error.
            if deadline.cancellation is not None:
                with contextlib.suppress(Exception):
                    _persist_retirement(
                        directory,
                        record,
                        deadline.failure_reason,
                        deadline.cancellation,
                    )
            with contextlib.suppress(Exception):
                _stop_record(directory, record)
            if process is not None and process.poll() is None:
                with contextlib.suppress(Exception):
                    _terminate_process_group(process)

    try:
        located = _slot(project, deadline, create=True)
        assert located is not None
        directory, key = located
        lock_path = directory / "lock"
        lock_fd = _private_open(lock_path, os.O_RDWR | os.O_CREAT)
        queued = False
        try:
            while True:
                deadline.remaining()
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not queued:
                        queued = True
                        _notify(
                            on_status,
                            QueryStatus(
                                "Queued for project worker",
                                stage="queued",
                                backend="worker",
                            ),
                        )
                    deadline.pause()
        except BaseException:
            os.close(lock_fd)
            raise
        with contextlib.ExitStack() as cleanup:
            cleanup.callback(os.close, lock_fd)
            admission_fd: int | None = _admit(admission_epoch, deadline)

            def release_admission() -> None:
                nonlocal admission_fd
                if admission_fd is not None:
                    os.close(admission_fd)
                    admission_fd = None

            cleanup.callback(release_admission)
            cleanup.callback(finish)
            generation = _generation(project, dbt, interpreter, request, environment, key)
            record = _record(directory)
            if record is not None:
                last_retirement = _valid_last_retirement(record.get("last_retirement"))
            if record and (record["generation"] != generation or not _watched_unchanged(record)):
                _notify(on_status, "Restarting worker for changed configuration")
                _persist_retirement(directory, record, "configuration_changed")
                last_retirement = _valid_last_retirement(record.get("last_retirement"))
                _stop_record(directory, record, min(2.0, deadline.remaining()))
                record = None
            ready = decoder = None
            if record is not None:
                try:
                    sock, ready, decoder = _open_worker(directory, record, None, deadline)
                except (FileNotFoundError, ConnectionRefusedError, ConnectionError):
                    record = None  # Idle expiry/crash, before any query was sent.
            if record is None:
                _notify(on_status, "Starting project dbt worker")
                record, process = _spawn(
                    directory,
                    generation,
                    context,
                    interpreter,
                    environment,
                    deadline,
                    float(request.worker_idle_seconds),
                    last_retirement,
                )
                sock, ready, decoder = _open_worker(directory, record, process, deadline)
            assert sock is not None and decoder is not None and ready is not None
            record["session_pid"] = ready.get("session_pid")
            record["versions"] = ready.get("versions", {})
            _write_state(directory / "control.json", record)
            request_id = secrets.token_hex(16)
            _notify(
                on_status,
                QueryStatus("Running query on project worker", stage="execution", backend="worker"),
            )
            execution_sent = True  # Even a partial send makes retry unsafe.
            deadline.execution_started = True
            _send(
                sock,
                {
                    "type": "execute",
                    "request_id": request_id,
                    "sql": _preview_sql(request.sql),
                    "limit": request.limit,
                    "timeout": deadline.remaining(),
                },
                deadline,
            )
            resolved_context: dict[str, Any] | None = None
            while True:
                message = _receive(sock, decoder, deadline)
                if message.get("request_id") != request_id:
                    raise EnhanceError("Worker returned a result for a different query.")
                kind = message.get("type")
                if kind == "accepted":
                    # The supervisor marks the request active before acknowledging.
                    # Removal can now inventory it without query execution retaining
                    # a global lock.
                    release_admission()
                    continue
                if kind == "progress":
                    if message.get("status") == "sso":
                        deadline.sso = True
                        _notify(on_status, SSO_STATUS)
                    elif message.get("status") == "running":
                        _notify(
                            on_status,
                            QueryStatus(
                                "Native dbt request running",
                                stage="compilation",
                                backend="worker",
                            ),
                        )
                    continue
                if kind == "context":
                    candidate = message.get("context")
                    if not _valid_native_context(candidate):
                        raise EnhanceError("Worker returned malformed native context metadata.")
                    resolved_context = candidate
                    _notify(
                        on_status,
                        QueryStatus(
                            "Native target "
                            f"{candidate['target']} verified ({candidate['adapter']})",
                            stage="preflight",
                            target=candidate["target"],
                            target_verified=True,
                            backend="worker",
                            support_tier="provisional",
                        ),
                    )
                    continue
                if kind == "error":
                    if message.get("reusable") is True:
                        watched = message.get("watched")
                        if not _valid_watched(watched):
                            raise EnhanceError("Worker returned malformed credential-file state.")
                        if not isinstance(message.get("message"), str):
                            raise EnhanceError("Worker returned a malformed query error.")
                        record["watched"] = watched
                        _write_state(directory / "control.json", record)
                        deadline.remaining()
                        session_reusable = True
                    else:
                        _persist_retirement(
                            directory,
                            record,
                            message.get("retirement_reason"),
                            message.get("cancellation"),
                        )
                    detail = str(message.get("message", "Worker query failed."))[:8000]
                    if message.get("sso") or deadline.sso:
                        detail = SSO_GUIDANCE
                    denied = message.get("code") == "target_not_allowed"
                    error = EnhanceError(
                        detail,
                        code="target_not_allowed" if denied else "query_failed",
                        stage="preflight" if denied else "execution",
                    )
                    if message.get("cancellation") == "requested_unverified":
                        error.cancellation = "requested_unverified"
                    raise error
                if kind != "result":
                    raise EnhanceError("Worker returned an unexpected response.")
                result = message.get("result")
                if not _valid_result(result, request.limit):
                    raise EnhanceError("Worker returned malformed query results.")
                record["watched"] = message.get("watched", [])
                _write_state(directory / "control.json", record)
                deadline.remaining()
                elapsed = time.monotonic() - started
                session_reusable = True
                native = _native_result_context(result)
                if native is not None:
                    if resolved_context is not None and native != resolved_context:
                        raise EnhanceError("Worker native context changed during the request.")
                return QueryResult(
                    columns=result["columns"],
                    rows=result["rows"],
                    compiled_sql=result["compiled_sql"],
                    warnings=result["warnings"],
                    elapsed=elapsed,
                    project_dir=project,
                    limit=request.limit,
                    target=native["target"] if native is not None else request.target,
                    column_types=result.get("column_types", []),
                    target_verified=native is not None,
                    backend="worker",
                    adapter=native["adapter"] if native is not None else None,
                    support_tier="provisional",
                    session_state="ready",
                    versions=(
                        native["versions"] if native is not None else ready.get("versions", {})
                    ),
                    sql_provenance=result.get("sql_provenance"),
                )
    except EnhanceError:
        raise
    except (OSError, ValueError, WireError) as exc:
        error = EnhanceError(f"Project worker failed: {exc}. The query was not retried.")
        raise error from None


_VALUE_KINDS = frozenset({"unknown", "text", "boolean", "number", "date", "datetime", "time"})
_NATIVE_RESULT_KEYS = frozenset({"target", "target_verified", "adapter", "versions"})


def _valid_native_context(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == _NATIVE_RESULT_KEYS
        and isinstance(value.get("target"), str)
        and bool(value["target"])
        and value.get("target_verified") is True
        and isinstance(value.get("adapter"), str)
        and bool(value["adapter"])
        and isinstance(value.get("versions"), dict)
        and all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in value["versions"].items()
        )
    )


def _native_result_context(result: dict[str, Any]) -> dict[str, Any] | None:
    present = _NATIVE_RESULT_KEYS.intersection(result)
    if not present:
        return None
    if present != _NATIVE_RESULT_KEYS:
        return None
    value = {key: result[key] for key in _NATIVE_RESULT_KEYS}
    return value if _valid_native_context(value) else None


def _valid_result(result: Any, limit: int) -> bool:
    if not isinstance(result, dict):
        return False
    columns, rows, sql, warnings = (
        result.get(key) for key in ("columns", "rows", "compiled_sql", "warnings")
    )
    legacy_valid = (
        isinstance(columns, list)
        and all(isinstance(column, str) for column in columns)
        and isinstance(rows, list)
        and len(rows) <= limit
        and all(isinstance(row, list) and len(row) == len(columns) for row in rows)
        and isinstance(sql, str)
        and bool(sql.strip())
        and isinstance(warnings, list)
        and all(isinstance(warning, str) for warning in warnings)
    )
    if not legacy_valid:
        return False
    native_present = _NATIVE_RESULT_KEYS.intersection(result)
    if native_present and _native_result_context(result) is None:
        return False
    column_types = result.get("column_types", [])
    if "column_types" in result and (
        not isinstance(column_types, list)
        or len(column_types) != len(columns)
        or not all(isinstance(kind, str) and kind in _VALUE_KINDS for kind in column_types)
    ):
        return False
    provenance = result.get("sql_provenance")
    if "sql_provenance" in result and provenance != "worker_native_result":
        return False
    return True


def inspect_worker(project: Path) -> dict[str, Any] | None:
    """Authenticate to a worker and return live supervisor-owned metadata."""

    canonical = project.resolve()
    deadline = _Deadline(1.0)
    located = _slot(canonical, deadline, create=False)
    if located is None:
        return None
    directory, _ = located
    record = _record(directory)
    if record is None:
        return None
    try:
        with _connect(directory, record) as sock:
            decoder = Decoder()
            _hello(sock, record, deadline)
            ready = _receive(sock, decoder, deadline)
            if ready.get("type") not in {"starting", "ready"}:
                if ready.get("type") == "error":
                    raise EnhanceError(str(ready.get("message", "Worker could not start.")))
                raise EnhanceError("Worker handshake did not match this installation.")
            if ready.get("protocol") != PROTOCOL or ready.get("instance") != record["instance"]:
                raise EnhanceError("Worker handshake did not match this installation.")
    except (FileNotFoundError, ConnectionRefusedError, ConnectionError):
        return None
    state = ready.get("state")
    idle_seconds = ready.get("idle_seconds")
    idle_remaining = ready.get("idle_remaining")
    versions = ready.get("versions")
    pid = ready.get("pid")
    session_pid = ready.get("session_pid")
    if (
        state not in {"starting", "ready", "active"}
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(session_pid, int)
        or isinstance(session_pid, bool)
        or session_pid <= 0
        or not isinstance(idle_seconds, (int, float))
        or isinstance(idle_seconds, bool)
        or not math.isfinite(float(idle_seconds))
        or not isinstance(versions, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in versions.items()
        )
        or (idle_remaining is not None and not isinstance(idle_remaining, (int, float)))
    ):
        raise EnhanceError("Worker returned malformed lifecycle metadata.")
    active = state != "ready"
    return {
        "project": canonical,
        "state": state,
        "status": "active" if active else "idle",
        "active": active,
        "idle_seconds": float(idle_seconds),
        "idle_remaining": float(idle_remaining) if idle_remaining is not None else None,
        "versions": dict(versions),
        "pid": pid,
        "session_pid": session_pid,
    }


def status(project: Path) -> dict[str, Any] | None:
    """Legacy worker status shape, preserved for compatibility."""

    information = inspect_worker(project)
    if information is None:
        return None
    return {key: information[key] for key in ("pid", "session_pid", "versions")}


def retirement_status(project: Path) -> dict[str, Any] | None:
    """Read the last validated retirement diagnostic without requiring a live worker."""
    located = _slot(project, _Deadline(1.0), create=False)
    if located is None:
        return None
    directory, _ = located
    record = _record(directory)
    if record is None:
        return None
    return _valid_last_retirement(record.get("last_retirement"))


def _lifecycle_command(
    project: Path, command: dict[str, Any], success: str, seconds: float = 2.0
) -> bool:
    located = _slot(project.resolve(), _Deadline(seconds), create=False)
    if located is None:
        return False
    directory, _ = located
    record = _record(directory)
    if record is None:
        return False
    deadline = _Deadline(seconds)
    try:
        sock = _connect(directory, record)
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    with sock:
        _hello(sock, record, deadline)
        _send(sock, command, deadline)
        decoder = Decoder()
        while True:
            response = _receive(sock, decoder, deadline)
            if response.get("type") == success:
                retired = _valid_last_retirement(record.get("last_retirement"))
                if response.get("cancellation") == "requested_unverified" and (
                    retired is None
                    or retired.get("session_pid") != record.get("session_pid")
                    or retired.get("cancellation") is None
                ):
                    _persist_retirement(
                        directory,
                        record,
                        "worker_failure",
                        "requested_unverified",
                    )
                return True
            if response.get("type") == "error":
                if response.get("code") == "worker_active":
                    return False
                raise EnhanceError(str(response.get("message", "Worker lifecycle command failed.")))


def _inert_slot(directory: Path) -> bool:
    """Recognize only slots that cannot describe or accept worker traffic."""
    entries = list(directory.iterdir())
    if not entries:
        return True
    if len(entries) == 1 and entries[0].name == "lock":
        fd = _private_open(entries[0], os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True
        finally:
            os.close(fd)
    return False


def inventory() -> tuple[dict[str, Any], ...]:
    """Return authenticated workers, failing closed on ambiguous owned slots."""

    root = _runtime_root(create=False)
    if root is None:
        return ()
    workers: list[dict[str, Any]] = []
    for directory in root.iterdir():
        if not directory.name or directory.name in {"key", "lock", _ADMISSION_FILE}:
            continue
        try:
            if not _private_directory(directory, create=False):
                raise EnhanceError(f"Unexpected worker registry entry: {directory}")
            try:
                record = _record(directory)
            except EnhanceError as exc:
                raise EnhanceError(
                    f"Cannot safely inspect owned worker slot {directory.name}: {exc} "
                    "Package removal is blocked; inspect the private runtime directory."
                ) from exc
            if record is None:
                if _inert_slot(directory):
                    continue
                raise EnhanceError(
                    f"Owned worker slot {directory.name} has no authenticated state but is not "
                    "empty. Package removal is blocked; inspect the private runtime directory."
                )
            project_value = record.get("project")
            if not isinstance(project_value, str) or not os.path.isabs(project_value):
                raise EnhanceError(
                    f"Owned worker slot {directory.name} has no valid project identity; "
                    "package removal is blocked."
                )
            project = Path(project_value)
            located = _slot(project, _Deadline(1.0), create=False)
            if located is None or located[0] != directory:
                raise EnhanceError(
                    f"Owned worker slot {directory.name} does not match its project identity; "
                    "package removal is blocked."
                )
            # A valid authenticated record whose private socket is gone/refused is
            # stale. Every other handshake, authentication, or timeout failure is
            # actionable and must block removal rather than hide a possible worker.
            try:
                information = inspect_worker(project)
            except EnhanceError as exc:
                raise EnhanceError(
                    f"Could not authenticate or inspect owned worker slot {directory.name}: "
                    f"{exc} Package removal is blocked; retry or inspect the private runtime "
                    "directory."
                ) from exc
            if information is not None:
                workers.append(information)
        except EnhanceError:
            raise
        except (OSError, ValueError) as exc:
            raise EnhanceError(
                f"Cannot safely inspect owned worker slot {directory.name}: {exc}. "
                "Package removal is blocked; inspect the private runtime directory."
            ) from exc
    workers.sort(key=lambda item: str(item["project"]))
    return tuple(workers)


def reset(project: Path, *, allow_active: bool = False) -> bool:
    """Retire a worker; active work is preserved unless explicitly allowed."""

    if not isinstance(allow_active, bool):
        raise TypeError("allow_active must be a bool")
    return _lifecycle_command(
        project,
        {"type": "reset", "allow_active": allow_active},
        "reset",
    )


def disconnect(project: Path) -> bool:
    """Disconnect only an idle worker, checked atomically by its supervisor."""

    return _lifecycle_command(project, {"type": "disconnect"}, "disconnected")


def stop(project: Path) -> bool:
    """Stop this project's authenticated worker, including an active query."""
    located = _slot(project, _Deadline(2.0), create=False)
    if located is None:
        return False
    directory, _ = located
    record = _record(directory)
    if record is None:
        return False
    # Do not take the query lock: stop must interrupt an active request.
    return _stop_record(directory, record)
