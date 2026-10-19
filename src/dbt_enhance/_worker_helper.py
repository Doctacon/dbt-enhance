#!/usr/bin/env python3
"""Standalone supervisor for one native dbt session.

This file runs standalone under the project's Python interpreter, without an
installed dbt-enhance package there. It uses stdlib and sibling helper modules.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import math
import os
import re
import selectors
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from ._wire import (
        IDLE_SECONDS,
        MAX_FRAME,
        MAX_REQUEST,
        MAX_SQL,
        PROTOCOL,
        Decoder,
        WireError,
        encode,
        validate_retirement_reason,
    )
except ImportError:  # Standalone copy.
    from _wire import (
        IDLE_SECONDS,
        MAX_FRAME,
        MAX_REQUEST,
        MAX_SQL,
        PROTOCOL,
        Decoder,
        WireError,
        encode,
        validate_retirement_reason,
    )

_BOOTSTRAP_LIMIT = 64 * 1024
_ERROR_LIMIT = 8 * 1024
_FILE_LIMIT = 4 * 1024 * 1024
_MAX_CONNECTIONS = 16
_HELLO_SECONDS = 5.0
_COMMAND_SECONDS = 30.0
_START_SECONDS = 120.0
_FLUSH_SECONDS = 1.0
_IO_CHUNK = 64 * 1024
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_HEX32 = re.compile(r"[0-9a-fA-F]{32}\Z")
_HEX64 = re.compile(r"[0-9a-fA-F]{64}\Z")
# Deliberately narrow markers: arbitrary dbt output must never become protocol.
_SSO_MARKERS = (
    b"\ninitiating login request with your identity provider",
    b"\nauthenticator request was accepted",
)


def _safe_message(value: object) -> str:
    text = _URL.sub("<redacted URL>", str(value)).replace("\x00", "")
    data = text.encode("utf-8", "replace")[:_ERROR_LIMIT]
    return data.decode("utf-8", "ignore") or "Worker failed."


def _valid_watched(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= 128
        and all(
            isinstance(item, dict)
            and set(item) == {"path", "digest"}
            and isinstance(item["path"], str)
            and isinstance(item["digest"], str)
            and _HEX64.fullmatch(item["digest"]) is not None
            for item in value
        )
    )


def _read_bootstrap() -> dict[str, Any]:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(0, min(_IO_CHUNK, _BOOTSTRAP_LIMIT + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > _BOOTSTRAP_LIMIT:
            raise ValueError("Worker bootstrap exceeded 64 KiB.")
    try:
        value = json.loads(b"".join(chunks))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Worker bootstrap was malformed.") from exc
    if not isinstance(value, dict):
        raise ValueError("Worker bootstrap must be an object.")
    return value


def _validate_bootstrap(value: dict[str, Any]) -> tuple[Path, str, str, dict[str, Any], float]:
    if value.get("protocol") != PROTOCOL or isinstance(value.get("protocol"), bool):
        raise ValueError("Unsupported worker protocol.")
    directory_value = value.get("directory")
    instance = value.get("instance")
    token = value.get("token")
    context = value.get("context")
    idle = value.get("idle_seconds", IDLE_SECONDS)
    if not isinstance(directory_value, str) or not os.path.isabs(directory_value):
        raise ValueError("Worker directory must be absolute.")
    if not isinstance(instance, str) or _HEX32.fullmatch(instance) is None:
        raise ValueError("Worker instance is invalid.")
    if not isinstance(token, str) or _HEX64.fullmatch(token) is None:
        raise ValueError("Worker token is invalid.")
    if not isinstance(context, dict):
        raise ValueError("Worker context is invalid.")
    project_dir = context.get("project_dir")
    profiles_dir = context.get("profiles_dir")
    target = context.get("target")
    if not isinstance(project_dir, str) or not os.path.isabs(project_dir):
        raise ValueError("Worker project directory must be absolute.")
    if profiles_dir is not None and (
        not isinstance(profiles_dir, str) or not os.path.isabs(profiles_dir)
    ):
        raise ValueError("Worker profiles directory must be absolute or null.")
    if target is not None and not isinstance(target, str):
        raise ValueError("Worker target must be a string or null.")
    if (
        isinstance(idle, bool)
        or not isinstance(idle, (int, float))
        or not math.isfinite(idle)
        or idle <= 0
    ):
        raise ValueError("Worker idle timeout is invalid.")

    directory = Path(directory_value)
    info = directory.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("Worker directory must be a real directory.")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Worker directory must be owned by this user with mode 0700.")
    return directory, instance.lower(), token.lower(), dict(context), float(idle)


def _write_startup_error(directory: Path, instance: str, message: object) -> None:
    path = directory / ("e-" + instance)
    payload = json.dumps({"error": _safe_message(message)}, ensure_ascii=False).encode("utf-8")
    temporary = directory / (".e-" + instance)
    created = False
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(temporary, path)
    except OSError:
        pass
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def watch_files(paths: list[str], token: str) -> list[dict[str, str]]:
    """Return keyed, content-free snapshots of credential files.

    Credential symlinks are followed just as the adapter follows them, so rotating
    a symlink target invalidates the session. Opens are nonblocking and no more
    than 4 MiB is read. Missing, non-regular, unreadable, and over-limit files have distinct
    keyed states so a later snapshot still detects relevant changes.
    """

    key = bytes.fromhex(token)
    watched: list[dict[str, str]] = []
    for value in sorted({path for path in paths if isinstance(path, str)}):
        state = b"missing"
        metadata = b""
        fd = -1
        try:
            fd = os.open(value, os.O_RDONLY | os.O_NONBLOCK)
            info = os.fstat(fd)
            metadata = json.dumps(
                (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                    stat.S_IMODE(info.st_mode),
                )
            ).encode()
            if not stat.S_ISREG(info.st_mode):
                state = b"not-regular"
            elif info.st_size > _FILE_LIMIT:
                state = b"too-large"
            else:
                chunks: list[bytes] = []
                remaining = _FILE_LIMIT + 1
                while remaining:
                    chunk = os.read(fd, min(_IO_CHUNK, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if sum(map(len, chunks)) > _FILE_LIMIT:
                    state = b"too-large"
                else:
                    state = b"file\0" + b"".join(chunks)
        except FileNotFoundError:
            state = b"missing"
        except OSError:
            state = b"unreadable"
        finally:
            if fd >= 0:
                os.close(fd)
        digest = hmac.new(key, metadata + b"\0" + state, hashlib.sha256).hexdigest()
        watched.append({"path": value, "digest": digest})
    return watched


def _child_scrub(error: object, session_module: Any) -> str:
    try:
        scrub = session_module._scrub_error
        return _safe_message(scrub(error))
    except Exception:
        try:
            exceptions = __import__("dbt_common.exceptions", fromlist=["x"])
            return _safe_message(exceptions.scrub_secrets(str(error), exceptions.env_secrets()))
        except Exception:
            # If dbt's registry is unavailable, do not risk reflecting an
            # import/authentication exception that may contain credentials.
            return "Native dbt session failed safely."


def _child_retirement_reason(error: BaseException, session_module: Any) -> str:
    """Classify a fatal child error without reflecting exception-controlled text."""
    is_session_error = False
    try:
        is_session_error = isinstance(error, session_module.SessionError)
    except Exception:
        pass
    if is_session_error:
        reason = validate_retirement_reason(getattr(error, "reason", None))
        return reason or "session_error"
    return "non_dbt_error"


def _arm_parent_death(parent_pid: int) -> None:
    if sys.platform.startswith("linux"):
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
                raise OSError(ctypes.get_errno(), "prctl")
            if os.getppid() != parent_pid:
                os.kill(os.getpid(), signal.SIGKILL)
        except Exception:
            pass

    def watch() -> None:
        while True:
            time.sleep(0.25)
            if os.getppid() != parent_pid:
                os._exit(1)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


def _blocking_receive(sock: socket.socket, decoder: Decoder) -> dict[str, Any] | None:
    while True:
        message = decoder.pop()
        if message is not None:
            return message
        data = sock.recv(_IO_CHUNK)
        if not data:
            return None
        decoder.feed(data)


def _child_send(sock: socket.socket, message: dict[str, Any]) -> None:
    sock.sendall(encode(message))


def _session_main(fd_text: str) -> int:
    try:
        fd = int(fd_text)
    except ValueError:
        return 2
    parent_pid = os.getppid()
    _arm_parent_death(parent_pid)
    sock = socket.socket(fileno=fd)
    decoder = Decoder()
    session_module: Any = None
    dbt_session: Any = None
    try:
        first = _blocking_receive(sock, decoder)
        if first is None:
            return 1
        context = first.get("context")
        token = first.get("token")
        if (
            not isinstance(context, dict)
            or not isinstance(token, str)
            or _HEX64.fullmatch(token) is None
        ):
            _child_send(sock, {"type": "error", "message": "Invalid session bootstrap."})
            return 1
        import _session as session_module  # type: ignore[no-redef]

        # The helper lives beside dbt_enhance/dbt.py in an installed wheel. That
        # directory must not shadow the project's real top-level dbt package.
        helper_directory = str(Path(__file__).resolve().parent)
        sys.path[:] = [entry for entry in sys.path if os.path.realpath(entry) != helper_directory]
        versions = session_module.compatibility()
        watched: list[dict[str, str]] = []

        def on_config(paths: list[str]) -> None:
            nonlocal watched
            # Native flags reveal the profile path before profile loading; key
            # paths become known afterward. Preserve each first-read baseline.
            snapshots = {item["path"]: item for item in watched}
            for item in watch_files(paths, token):
                snapshots.setdefault(item["path"], item)
            watched = [snapshots[path] for path in sorted(snapshots)]

        dbt_session = session_module.DbtSession(context, on_config=on_config)
        _child_send(sock, {"type": "ready", "versions": versions})
        while True:
            command = _blocking_receive(sock, decoder)
            if command is None:
                return 0
            kind = command.get("type")
            if kind == "close":
                try:
                    dbt_session.close()
                finally:
                    _child_send(sock, {"type": "closed"})
                return 0
            if kind != "execute":
                raise ValueError("Invalid session command.")
            request_id = command.get("request_id")
            sql = command.get("sql")
            limit = command.get("limit")
            try:
                result = dbt_session.execute(sql, limit)
            except session_module.QueryError as error:
                # QueryError means dbt completed rollback/request cleanup and
                # verified that the native connection remains usable. Preserve
                # any credential baselines discovered by the failed request.
                _child_send(
                    sock,
                    {
                        "type": "error",
                        "request_id": request_id,
                        "message": _child_scrub(error, session_module),
                        "reusable": True,
                        "watched": watched,
                    },
                )
                continue
            try:
                _child_send(
                    sock,
                    {
                        "type": "result",
                        "request_id": request_id,
                        "result": result,
                        "watched": watched,
                    },
                )
            except WireError as error:
                _child_send(
                    sock,
                    {
                        "type": "error",
                        "request_id": request_id,
                        "message": str(error),
                    },
                )
                return 1
    except BaseException as error:
        try:
            _child_send(
                sock,
                {
                    "type": "error",
                    "message": _child_scrub(error, session_module),
                    "retirement_reason": _child_retirement_reason(error, session_module),
                },
            )
        except BaseException:
            pass
        return 1
    finally:
        if dbt_session is not None:
            try:
                dbt_session.close()
            except BaseException:
                pass
        sock.close()


@dataclass
class _Client:
    sock: socket.socket
    decoder: Decoder = field(default_factory=lambda: Decoder(MAX_REQUEST))
    output: bytearray = field(default_factory=bytearray)
    authenticated: bool = False
    deadline: float = 0.0
    command_seen: bool = False
    close_after_write: bool = False
    result_pending: bool = False


class _Supervisor:
    def __init__(
        self,
        directory: Path,
        instance: str,
        token: str,
        context: dict[str, Any],
        idle_seconds: float,
    ) -> None:
        self.directory = directory
        self.instance = instance
        self.token = token
        self.context = context
        self.idle_seconds = idle_seconds
        self.socket_path = directory / ("s-" + instance)
        self.selector = selectors.DefaultSelector()
        self.listener: socket.socket | None = None
        self.socket_identity: tuple[int, int] | None = None
        self.child_socket: socket.socket | None = None
        self.child_decoder = Decoder()
        self.child_output = bytearray()
        self.child: subprocess.Popen[bytes] | None = None
        self.clients: dict[int, _Client] = {}
        self.state = "starting"
        self.versions: dict[str, Any] = {}
        self.active: _Client | None = None
        self.request_id: str | None = None
        self.request_deadline = 0.0
        self.idle_deadline = 0.0
        self.start_deadline = time.monotonic() + _START_SECONDS
        self.had_authenticated_client = False
        self.child_output_count = 0
        self.output_overlap = b"\n"
        self.sso_sent = False
        self.pending_command: dict[str, Any] | None = None
        self.exit_deadline: float | None = None
        self.startup_error: str | None = None

    def _bind(self) -> None:
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise RuntimeError("Worker socket already exists.")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Publish with final permissions: clients may lstat immediately after
            # bind, before a subsequent chmod would otherwise close that race.
            previous_umask = os.umask(0o177)
            try:
                listener.bind(str(self.socket_path))
            finally:
                os.umask(previous_umask)
            os.chmod(self.socket_path, 0o600)
            info = os.lstat(self.socket_path)
            self.socket_identity = (info.st_dev, info.st_ino)
            listener.listen(_MAX_CONNECTIONS)
            listener.setblocking(False)
        except BaseException:
            listener.close()
            raise
        self.listener = listener
        self.selector.register(listener, selectors.EVENT_READ, "listen")

    def _spawn(self) -> None:
        parent_sock, child_sock = socket.socketpair()
        parent_sock.setblocking(False)
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--session",
            str(child_sock.fileno()),
        ]
        try:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                pass_fds=(child_sock.fileno(),),
                start_new_session=True,
                close_fds=True,
            )
        finally:
            child_sock.close()
        self.child = child
        self.child_socket = parent_sock
        self.selector.register(parent_sock, selectors.EVENT_READ, "child")
        if child.stdout is not None:
            os.set_blocking(child.stdout.fileno(), False)
            self.selector.register(child.stdout, selectors.EVENT_READ, "output")
        self._queue_child({"context": self.context, "token": self.token})

    def _queue_child(self, message: dict[str, Any]) -> None:
        assert self.child_socket is not None
        self.child_output.extend(encode(message))
        self.selector.modify(
            self.child_socket, selectors.EVENT_READ | selectors.EVENT_WRITE, "child"
        )

    def _write_child(self) -> None:
        assert self.child_socket is not None
        try:
            sent = self.child_socket.send(self.child_output)
            del self.child_output[:sent]
            if not self.child_output:
                self.selector.modify(self.child_socket, selectors.EVENT_READ, "child")
        except BlockingIOError:
            pass
        except OSError:
            self._retire("Native dbt session could not accept the request.")

    def _queue(self, client: _Client, message: dict[str, Any], close: bool = False) -> None:
        try:
            client.output.extend(encode(message))
        except WireError:
            client.output.extend(
                encode({"type": "error", "message": "Worker response was too large."})
            )
            close = True
        client.close_after_write = client.close_after_write or close
        try:
            self.selector.modify(client.sock, selectors.EVENT_READ | selectors.EVENT_WRITE, client)
        except (KeyError, ValueError, OSError):
            pass

    def _close_client(self, client: _Client) -> None:
        if self.active is client and self.state == "active":
            self._retire("Active client disconnected.", retirement_reason="client_disconnect")
        fd = client.sock.fileno()
        try:
            self.selector.unregister(client.sock)
        except Exception:
            pass
        try:
            client.sock.close()
        except OSError:
            pass
        self.clients.pop(fd, None)

    def _accept(self, now: float) -> None:
        assert self.listener is not None
        try:
            sock, _ = self.listener.accept()
        except OSError:
            return
        if len(self.clients) >= _MAX_CONNECTIONS or self.state == "retiring":
            sock.close()
            return
        sock.setblocking(False)
        client = _Client(sock=sock, deadline=now + _HELLO_SECONDS)
        self.clients[sock.fileno()] = client
        self.selector.register(sock, selectors.EVENT_READ, client)

    def _hello(self, client: _Client, message: dict[str, Any], now: float) -> None:
        protocol = message.get("protocol")
        supplied_instance = message.get("instance")
        supplied_token = message.get("token")
        valid = (
            message.get("type") == "hello"
            and protocol == PROTOCOL
            and not isinstance(protocol, bool)
            and isinstance(supplied_instance, str)
            and _HEX32.fullmatch(supplied_instance) is not None
            and hmac.compare_digest(supplied_instance, self.instance)
            and isinstance(supplied_token, str)
            and _HEX64.fullmatch(supplied_token) is not None
            and hmac.compare_digest(supplied_token, self.token)
        )
        if not valid:
            self._close_client(client)
            return
        client.authenticated = True
        client.deadline = (
            self.start_deadline if self.state == "starting" else now + _COMMAND_SECONDS
        )
        self.had_authenticated_client = True
        if self.state == "starting":
            self._queue(
                client, {"type": "starting", "protocol": PROTOCOL, "instance": self.instance}
            )
        elif self.state == "ready":
            self._send_ready(client)
        elif self.state == "active":
            self._send_ready(client)

    def _send_ready(self, client: _Client) -> None:
        assert self.child is not None
        self._queue(
            client,
            {
                "type": "ready",
                "protocol": PROTOCOL,
                "instance": self.instance,
                "pid": os.getpid(),
                "session_pid": self.child.pid,
                "versions": self.versions,
            },
        )

    @staticmethod
    def _valid_request(message: dict[str, Any]) -> tuple[bool, str | None]:
        request_id = message.get("request_id")
        sql = message.get("sql")
        limit = message.get("limit")
        timeout = message.get("timeout")
        try:
            sql_size = len(sql.encode("utf-8")) if isinstance(sql, str) else MAX_SQL + 1
        except UnicodeEncodeError:
            sql_size = MAX_SQL + 1
        valid = (
            message.get("type") == "execute"
            and isinstance(request_id, str)
            and _HEX32.fullmatch(request_id) is not None
            and isinstance(sql, str)
            and sql_size <= MAX_SQL
            and isinstance(limit, int)
            and not isinstance(limit, bool)
            and 1 <= limit <= 10_000
            and isinstance(timeout, (int, float))
            and not isinstance(timeout, bool)
            and math.isfinite(timeout)
            and timeout > 0
        )
        return valid, request_id if isinstance(request_id, str) and _HEX32.fullmatch(
            request_id
        ) else None

    def _command(self, client: _Client, message: dict[str, Any], now: float) -> None:
        if client.command_seen:
            self._queue(
                client,
                {
                    "type": "error",
                    **({"request_id": self.request_id} if self.active is client else {}),
                    "message": "Malformed worker request.",
                },
                close=True,
            )
            self._retire("Malformed authenticated worker request.", offender=client)
            return
        client.command_seen = True
        if message.get("type") == "stop":
            self._queue(client, {"type": "stopped"}, close=True)
            self._retire("Worker was stopped.", stopped_by=client)
            return
        valid, request_id = self._valid_request(message)
        if not valid:
            self._queue(
                client,
                {
                    "type": "error",
                    **({"request_id": request_id} if request_id else {}),
                    "message": "Malformed worker request.",
                },
                close=True,
            )
            self._retire("Malformed authenticated worker request.", offender=client)
            return
        if self.state == "starting":
            self._queue(
                client,
                {"type": "error", "request_id": request_id, "message": "Worker is still starting."},
                close=True,
            )
            return
        if self.state == "active":
            self._queue(
                client,
                {"type": "error", "request_id": request_id, "message": "Worker is busy."},
                close=True,
            )
            return
        if self.state != "ready" or self.child_socket is None:
            self._queue(
                client,
                {"type": "error", "request_id": request_id, "message": "Worker is unavailable."},
                close=True,
            )
            return
        self.state = "active"
        self.active = client
        self.request_id = request_id
        self.request_deadline = now + float(message["timeout"])
        self.child_output_count = 0
        self.output_overlap = b"\n"
        self.sso_sent = False
        client.deadline = self.request_deadline + _FLUSH_SECONDS
        self.pending_command = {
            "type": "execute",
            "request_id": request_id,
            "sql": message["sql"],
            "limit": message["limit"],
        }
        # Forwarding is deferred until _write_client has actually put this
        # accepted frame on the client socket.  A fast child can therefore
        # never begin work before acceptance is observable by the caller.
        self._queue(client, {"type": "accepted", "request_id": request_id})

    def _read_client(self, client: _Client, now: float) -> None:
        try:
            data = client.sock.recv(_IO_CHUNK)
            if not data:
                self._close_client(client)
                return
            client.decoder.feed(data)
            while True:
                message = client.decoder.pop()
                if message is None:
                    break
                if not client.authenticated:
                    self._hello(client, message, now)
                else:
                    self._command(client, message, now)
                if client.sock.fileno() < 0 or client.close_after_write:
                    break
        except WireError:
            if client.authenticated:
                self._queue(
                    client,
                    {"type": "error", "message": "Malformed worker request."},
                    close=True,
                )
                self._retire("Malformed authenticated worker request.", offender=client)
            else:
                self._close_client(client)
        except OSError:
            self._close_client(client)

    def _write_client(self, client: _Client) -> None:
        if not client.output:
            try:
                self.selector.modify(client.sock, selectors.EVENT_READ, client)
            except (KeyError, ValueError, OSError):
                return
            if client.close_after_write:
                self._close_client(client)
            return
        try:
            sent = client.sock.send(client.output)
            del client.output[:sent]
        except BlockingIOError:
            return
        except OSError:
            self._close_client(client)
            return
        if not client.output and self.active is client and self.pending_command is not None:
            self._forward_pending()
        if not client.output and client.result_pending and self.state == "active":
            client.result_pending = False
            self.active = None
            self.request_id = None
            self.state = "ready"
            self.idle_deadline = time.monotonic() + self.idle_seconds
        if not client.output and client.close_after_write:
            self._close_client(client)

    def _forward_pending(self) -> None:
        command = self.pending_command
        if command is None or self.child_socket is None or self.active is None:
            return
        self.pending_command = None
        self._queue(
            self.active,
            {"type": "progress", "request_id": self.request_id, "status": "running"},
        )
        try:
            # Never block the supervisor on an unresponsive native child. Large
            # frames drain incrementally while stop, EOF, and deadlines remain live.
            self._queue_child(command)
        except (OSError, WireError):
            self._retire("Native dbt session could not accept the request.")

    def _child_message(self, message: dict[str, Any], now: float) -> None:
        kind = message.get("type")
        if self.state == "starting":
            if kind == "ready" and isinstance(message.get("versions"), dict):
                self.versions = message["versions"]
                self.state = "ready"
                self.idle_deadline = now + self.idle_seconds
                for client in list(self.clients.values()):
                    if client.authenticated:
                        client.deadline = now + _COMMAND_SECONDS
                        self._send_ready(client)
                return
            error = _safe_message(message.get("message", "Native dbt session failed to start."))
            self.startup_error = error
            retirement_reason = validate_retirement_reason(message.get("retirement_reason"))
            self._retire(error, retirement_reason=retirement_reason or "worker_failure")
            return
        if self.state != "active":
            self._retire("Native dbt session sent an unexpected response.")
            return
        if (
            kind == "result"
            and message.get("request_id") == self.request_id
            and isinstance(message.get("result"), dict)
            and _valid_watched(message.get("watched"))
        ):
            assert self.active is not None
            self.active.result_pending = True
            self._queue(
                self.active,
                {
                    "type": "result",
                    "request_id": self.request_id,
                    "result": message["result"],
                    "watched": message["watched"],
                },
            )
            return
        if (
            kind == "error"
            and message.get("request_id") == self.request_id
            and message.get("reusable") is True
            and isinstance(message.get("message"), str)
            and _valid_watched(message.get("watched"))
        ):
            assert self.active is not None
            self.active.result_pending = True
            self._queue(
                self.active,
                {
                    "type": "error",
                    "request_id": self.request_id,
                    "message": _safe_message(message["message"]),
                    "reusable": True,
                    "watched": message["watched"],
                    "sso": self.sso_sent,
                },
            )
            return
        error = _safe_message(message.get("message", "Native dbt session failed."))
        retirement_reason = validate_retirement_reason(message.get("retirement_reason"))
        self._retire(error, retirement_reason=retirement_reason or "worker_failure")

    def _read_child(self, now: float) -> None:
        assert self.child_socket is not None
        try:
            data = self.child_socket.recv(_IO_CHUNK)
            if not data:
                self._retire("Native dbt session exited unexpectedly.")
                return
            self.child_decoder.feed(data)
            while True:
                message = self.child_decoder.pop()
                if message is None:
                    break
                # stdout writes happen before the child's terminal protocol send.
                # Drain bytes already available so SSO/output-cap state does not
                # depend on which ready descriptor the selector returned first.
                while self._read_output():
                    pass
                if self.state == "retiring":
                    return
                self._child_message(message, now)
        except (OSError, WireError):
            self._retire("Native dbt session sent an invalid response.")

    def _read_output(self) -> bool:
        if self.child is None or self.child.stdout is None:
            return False
        try:
            data = os.read(self.child.stdout.fileno(), _IO_CHUNK)
        except BlockingIOError:
            return False
        except OSError:
            data = b""
        if not data:
            try:
                self.selector.unregister(self.child.stdout)
            except Exception:
                pass
            return False
        if self.state in {"starting", "active"}:
            self.child_output_count += len(data)
            if self.child_output_count > MAX_FRAME:
                self._retire("dbt output exceeded the 16 MiB limit.")
                return False
        sample = (self.output_overlap + data).lower()
        self.output_overlap = sample[-256:]
        if (
            self.state == "active"
            and not self.sso_sent
            and any(mark in sample for mark in _SSO_MARKERS)
        ):
            self.sso_sent = True
            if self.active is not None:
                self._queue(
                    self.active,
                    {"type": "progress", "request_id": self.request_id, "status": "sso"},
                )
        return True

    def _kill_child(self) -> None:
        child = self.child
        if child is None or child.poll() is not None:
            return
        # Ask the native session to run its idempotent close on its main thread.
        # A query may be blocking there, so this request is always time-bounded.
        if self.child_socket is not None and not self.child_output:
            try:
                self.child_socket.sendall(encode({"type": "close"}))
            except (OSError, WireError):
                pass
        try:
            child.wait(timeout=0.35)
            return
        except subprocess.TimeoutExpired:
            pass
        # Only the Popen-owned process group is ever signalled.
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            child.wait(timeout=0.35)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _retire(
        self,
        reason: str,
        *,
        offender: _Client | None = None,
        stopped_by: _Client | None = None,
        retirement_reason: str = "worker_failure",
    ) -> None:
        if self.state == "retiring":
            return
        retirement_reason = validate_retirement_reason(retirement_reason) or "worker_failure"
        was_starting = self.state == "starting"
        self.state = "retiring"
        if (
            self.active is not None
            and self.active is not offender
            and self.active is not stopped_by
        ):
            self._queue(
                self.active,
                {
                    "type": "error",
                    "request_id": self.request_id,
                    "message": _safe_message(reason),
                    "sso": self.sso_sent,
                    "retirement_reason": retirement_reason,
                },
                close=True,
            )
        if was_starting:
            self.startup_error = _safe_message(reason)
            for client in list(self.clients.values()):
                if client.authenticated and client is not offender and client is not stopped_by:
                    self._queue(
                        client,
                        {
                            "type": "error",
                            "message": self.startup_error,
                            "retirement_reason": retirement_reason,
                        },
                        close=True,
                    )
        if self.listener is not None:
            try:
                self.selector.unregister(self.listener)
            except Exception:
                pass
            self.listener.close()
            self.listener = None
        self._kill_child()
        self.exit_deadline = time.monotonic() + _FLUSH_SECONDS

    def _deadlines(self, now: float) -> None:
        for client in list(self.clients.values()):
            if now > client.deadline and not client.output:
                self._close_client(client)
        if self.state == "starting":
            authenticated = any(client.authenticated for client in self.clients.values())
            if now > self.start_deadline or (self.had_authenticated_client and not authenticated):
                self._retire("Native dbt session startup was cancelled or timed out.")
        elif self.state == "ready" and now > self.idle_deadline:
            self._retire("Worker idle timeout expired.")
        elif self.state == "active" and now > self.request_deadline:
            self._retire("dbt request timed out.", retirement_reason="timeout")

    def run(self) -> int:
        try:
            self._bind()
            self._spawn()
            while True:
                now = time.monotonic()
                self._deadlines(now)
                if self.state == "retiring" and (
                    not any(client.output for client in self.clients.values())
                    or (self.exit_deadline is not None and now >= self.exit_deadline)
                ):
                    break
                for key, mask in self.selector.select(0.05):
                    data = key.data
                    if data == "listen":
                        self._accept(time.monotonic())
                    elif data == "child":
                        if mask & selectors.EVENT_READ:
                            self._read_child(time.monotonic())
                        if mask & selectors.EVENT_WRITE and self.state != "retiring":
                            self._write_child()
                    elif data == "output":
                        self._read_output()
                    else:
                        client = data
                        if mask & selectors.EVENT_READ:
                            self._read_client(client, time.monotonic())
                        if mask & selectors.EVENT_WRITE and client.sock.fileno() >= 0:
                            self._write_client(client)
                if (
                    self.child is not None
                    and self.child.poll() is not None
                    and self.state != "retiring"
                ):
                    self._retire("Native dbt session exited unexpectedly.")
            return 0
        except BaseException as error:
            self.startup_error = _safe_message(error)
            return 1
        finally:
            self._kill_child()
            for client in list(self.clients.values()):
                self._close_client(client)
            if self.child_socket is not None:
                try:
                    self.selector.unregister(self.child_socket)
                except Exception:
                    pass
                self.child_socket.close()
            self.selector.close()
            self._unlink_own_socket()
            if self.startup_error and not self.had_authenticated_client:
                _write_startup_error(self.directory, self.instance, self.startup_error)

    def _unlink_own_socket(self) -> None:
        if self.socket_identity is None:
            return
        try:
            info = os.lstat(self.socket_path)
            if (info.st_dev, info.st_ino) == self.socket_identity and stat.S_ISSOCK(info.st_mode):
                os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _error_destination(value: dict[str, Any]) -> tuple[Path, str] | None:
    """Find a safe error-file destination before full bootstrap validation."""

    directory_value = value.get("directory")
    instance = value.get("instance")
    if (
        not isinstance(directory_value, str)
        or not os.path.isabs(directory_value)
        or not isinstance(instance, str)
        or _HEX32.fullmatch(instance) is None
    ):
        return None
    directory = Path(directory_value)
    try:
        info = directory.lstat()
    except OSError:
        return None
    if (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700
    ):
        return directory, instance.lower()
    return None


def _supervisor_main() -> int:
    directory: Path | None = None
    instance: str | None = None
    try:
        value = _read_bootstrap()
        destination = _error_destination(value)
        if destination is not None:
            directory, instance = destination
        directory, instance, token, context, idle = _validate_bootstrap(value)
        return _Supervisor(directory, instance, token, context, idle).run()
    except BaseException as error:
        if directory is not None and instance is not None:
            _write_startup_error(directory, instance, error)
        return 1


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--session":
        return _session_main(sys.argv[2])
    if len(sys.argv) != 1:
        return 2
    return _supervisor_main()


if __name__ == "__main__":
    raise SystemExit(main())
