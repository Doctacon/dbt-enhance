"""Native, non-executing dbt context resolution."""

from __future__ import annotations

import contextlib
import json
import math
import os
import select
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .dbt import _child_environment, discover_project, find_dbt
from .models import EnhanceError, QueryRequest, QueryStatus, ResolvedContext
from .worker import project_python

_HELPER = Path(__file__).with_name("_project_helper.py")
_LIMIT = 64 * 1024
_POLL = 0.05


def _notify(callback: Callable[[str], None] | None, message: str, request: QueryRequest) -> None:
    if callback is None:
        return
    try:
        callback(QueryStatus(message, stage="preflight", backend=request.backend))
    except Exception:
        pass


def _profiles_dir(project: Path, value: Path | None) -> str | None:
    if value is None:
        return None
    expanded = value.expanduser()
    if not expanded.is_absolute():
        expanded = project / expanded
    return str(expanded.absolute())


def _deadline_seconds(request: QueryRequest, timeout: float | None) -> float:
    seconds = request.timeout if timeout is None else timeout
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not math.isfinite(float(seconds))
        or seconds <= 0
    ):
        raise EnhanceError(
            "Preflight timeout must be a positive, finite number of seconds.",
            code="invalid_request",
            stage="preflight",
        )
    return float(seconds)


def resolve_context(
    request: QueryRequest,
    cancel: threading.Event | None = None,
    timeout: float | None = None,
    on_status: Callable[[str], None] | None = None,
) -> ResolvedContext:
    """Resolve target and adapter through dbt's native configuration lifecycle.

    The helper stops before adapter registration, manifest parsing, compilation,
    connection creation, or query execution.
    """

    seconds = _deadline_seconds(request, timeout)
    if request.allowed_targets is not None and (
        not isinstance(request.allowed_targets, (tuple, list))
        or not all(isinstance(item, str) for item in request.allowed_targets)
    ):
        raise EnhanceError(
            "Allowed targets must be a sequence of target names or None.",
            code="invalid_request",
            stage="preflight",
        )
    cancellation = cancel or threading.Event()
    if cancellation.is_set():
        raise EnhanceError("Context resolution was cancelled.", code="cancelled", stage="preflight")
    project = discover_project(request.file)
    executable = find_dbt(project, request.dbt)
    try:
        interpreter = project_python(executable)
    except EnhanceError as error:
        raise EnhanceError(str(error), code="unsupported_environment", stage="preflight") from None

    context: dict[str, Any] = {
        "project_dir": str(project),
        "profiles_dir": _profiles_dir(project, request.profiles_dir),
        "target": request.target,
        "allowed_targets": (
            list(request.allowed_targets) if request.allowed_targets is not None else None
        ),
    }
    try:
        payload = json.dumps(context, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise EnhanceError(
            "Native dbt context is invalid.", code="invalid_request", stage="preflight"
        ) from None
    if len(payload) > _LIMIT:
        raise EnhanceError(
            "Native dbt context exceeds its safety limit.",
            code="invalid_request",
            stage="preflight",
        )

    _notify(on_status, "Resolving native dbt context", request)
    environment = _child_environment()
    process = subprocess.Popen(
        [str(interpreter), str(_HELPER)],
        cwd=project,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        bufsize=0,
    )
    output = bytearray()
    pending = memoryview(payload)
    end = time.monotonic() + seconds
    try:
        assert process.stdin is not None and process.stdout is not None
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        while True:
            if cancellation.is_set():
                raise EnhanceError(
                    "Context resolution was cancelled.", code="cancelled", stage="preflight"
                )
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise EnhanceError(
                    f"Native dbt context resolution timed out after {seconds:g} seconds.",
                    code="timeout",
                    stage="preflight",
                )
            readers = [process.stdout] if len(output) <= _LIMIT else []
            writers = [process.stdin] if pending else []
            ready_read, ready_write, _ = select.select(readers, writers, [], min(_POLL, remaining))
            if ready_write:
                try:
                    count = os.write(process.stdin.fileno(), pending)
                    pending = pending[count:]
                    if not pending:
                        process.stdin.close()
                except BlockingIOError:
                    pass
            if ready_read:
                try:
                    chunk = os.read(process.stdout.fileno(), 65536)
                except BlockingIOError:
                    chunk = None
                if chunk:
                    output.extend(chunk)
                    if len(output) > _LIMIT:
                        raise EnhanceError(
                            "Native dbt context response exceeded its safety limit.",
                            code="preflight_failed",
                            stage="preflight",
                        )
            if process.poll() is not None:
                while len(output) <= _LIMIT:
                    try:
                        chunk = os.read(process.stdout.fileno(), 65536)
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                break
    except BaseException:
        with contextlib.suppress(Exception):
            os.killpg(process.pid, 15)
        with contextlib.suppress(Exception):
            process.wait(timeout=0.25)
        if process.poll() is None:
            with contextlib.suppress(Exception):
                os.killpg(process.pid, 9)
        raise
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
    if len(output) > _LIMIT:
        raise EnhanceError(
            "Native dbt context response exceeded its safety limit.",
            code="preflight_failed",
            stage="preflight",
        )
    try:
        response = json.loads(output)
    except (ValueError, UnicodeError, RecursionError):
        raise EnhanceError(
            "Native dbt context helper returned an invalid response.",
            code="preflight_failed",
            stage="preflight",
        ) from None
    if not isinstance(response, dict) or response.get("ok") is not True:
        code = response.get("code") if isinstance(response, dict) else None
        message = response.get("message") if isinstance(response, dict) else None
        if code not in {"target_not_allowed", "unsupported_environment", "preflight_failed"}:
            code = "preflight_failed"
        if not isinstance(message, str) or not message:
            message = "Native dbt configuration could not be resolved safely."
        raise EnhanceError(message[:4096], code=code, stage="preflight")
    target = response.get("target")
    adapter = response.get("adapter")
    versions = response.get("versions")
    if (
        not isinstance(target, str)
        or not target
        or not isinstance(adapter, str)
        or not adapter
        or not isinstance(versions, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in versions.items()
        )
    ):
        raise EnhanceError(
            "Native dbt context helper returned malformed metadata.",
            code="preflight_failed",
            stage="preflight",
        )
    _notify(on_status, "Native dbt target verified", request)
    return ResolvedContext(
        project_dir=project,
        dbt_executable=executable,
        project_python=interpreter,
        target=target,
        adapter=adapter,
        versions=dict(versions),
    )
