"""Dispatch dbt previews to a persistent worker or isolated subprocess.

The worker backend is the default. The explicit process backend uses dbt's public
command line, isolating its global state and making cancellation reliable.
"""

from __future__ import annotations

import json
import math
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from typing import Any

from sqlparse import lexer, tokens

from .models import (
    SSO_GUIDANCE,
    SSO_STATUS,
    BackendChoiceRequired,
    EnhanceError,
    QueryRequest,
    QueryResult,
    QueryStatus,
)

_IS_DARWIN = sys.platform == "darwin"
_MAX_SQL_BYTES = 1024 * 1024
_MAX_CAPTURE_BYTES = 16 * 1024 * 1024
_OUTPUT_SIZE_ERROR = (
    "dbt output exceeded the 16 MiB safety limit. Lower --limit or "
    "select fewer columns and try again."
)
_CANCEL_POLL_SECONDS = 0.05
_TERMINATE_GRACE_SECONDS = 0.5
_SSO_MARKERS = (
    b"\nInitiating login request with your identity provider.",
    b"\nA browser window should have opened",
    b"\nWe were unable to open a browser window",
    b"\nEnter the URL the SSO URL redirected you to:",
)
_EMPTY_COLUMNS_WARNING = (
    "The query returned no rows; dbt's JSON preview does not include column "
    "metadata, so columns are unavailable."
)


def discover_project(file: Path) -> Path:
    """Return the nearest dbt project containing an absolute source path.

    Search begins at the source file's parent and only proceeds upward.  In
    particular, the process working directory and sibling directories are never
    searched.
    """

    source = Path(file).expanduser()
    if not source.is_absolute():
        raise EnhanceError(
            f"Cannot find a dbt project for relative source path {str(file)!r}; "
            "provide an absolute file path."
        )

    directory = source.resolve(strict=False).parent
    for candidate_dir in (directory, *directory.parents):
        marker = candidate_dir / "dbt_project.yml"
        if marker.is_file():
            return candidate_dir

    raise EnhanceError(f"No dbt_project.yml was found in {directory} or any of its parents.")


def find_dbt(project_dir: Path, override: Path | None = None) -> Path:
    """Locate dbt, preferring project and monorepo virtual environments."""

    if override is not None:
        executable = Path(override).expanduser().resolve(strict=False)
        if not _is_executable(executable):
            raise EnhanceError(
                f"The configured dbt executable is not an executable file: {executable}"
            )
        return executable

    project = Path(project_dir).expanduser().resolve(strict=False)
    unusable: list[Path] = []
    for directory in (project, *project.parents):
        candidate = directory / ".venv" / "bin" / "dbt"
        if _is_executable(candidate):
            return candidate
        if candidate.exists():
            unusable.append(candidate)

    path_dbt = shutil.which("dbt")
    if path_dbt is not None:
        executable = Path(path_dbt).resolve(strict=False)
        if _is_executable(executable):
            return executable

    detail = ""
    if unusable:
        detail = " Non-executable candidates: " + ", ".join(map(str, unusable)) + "."
    raise EnhanceError(
        "Could not find an executable dbt. Install dbt in .venv, add dbt to "
        f"PATH, or configure an explicit dbt executable.{detail}"
    )


def run_query(
    request: QueryRequest,
    cancel: threading.Event | None = None,
    on_status: Callable[[str], None] | None = None,
) -> QueryResult:
    """Execute once, with best-effort metadata-only diagnostics at the caller seam."""
    started = time.monotonic()
    try:
        result = _execute_query(request, cancel, on_status)
    except BackendChoiceRequired:
        # Human selection is not a failed warehouse query and is never retried here.
        raise
    except EnhanceError as exc:
        _record_diagnostic(request, time.monotonic() - started, error=exc)
        raise
    if isinstance(result, QueryResult):
        _record_diagnostic(request, time.monotonic() - started, result=result)
    return result


def _record_diagnostic(
    request: QueryRequest,
    elapsed: float,
    *,
    result: QueryResult | None = None,
    error: EnhanceError | None = None,
) -> None:
    """Optional local history must never replace the query's real outcome."""
    if request.diagnostics is not True:
        return
    try:
        from .diagnostics import DiagnosticStore

        project = result.project_dir if result else discover_project(request.file)
        code = error.code if error else None
        outcome = "success" if error is None else "failure"
        if code in {"timeout", "cancelled"}:
            outcome = code
        metadata = {
            "outcome": outcome,
            "reason": code,
            "cancellation": getattr(error, "cancellation", None),
            "stage": error.stage if error else "execution",
            "backend": result.backend if result and result.backend else request.backend,
            "support_status": result.support_tier if result else "unknown",
            "platform": "macos"
            if sys.platform == "darwin"
            else "linux"
            if sys.platform.startswith("linux")
            else "other",
            "versions": {
                "tool": version("dbt-enhance"),
                "python": platform.python_version(),
                **(result.versions if result else {}),
            },
            "timings": {**(result.timings if result else {}), "total": elapsed},
        }
        store = DiagnosticStore()
        if error is not None:
            store.record_failure(project, metadata)
        else:
            store.record_latest(project, metadata)
    except Exception:  # noqa: BLE001 - optional diagnostics cannot fail a query
        pass


def _execute_query(
    request: QueryRequest,
    cancel: threading.Event | None = None,
    on_status: Callable[[str], None] | None = None,
) -> QueryResult:
    """Compile and execute one inline query with dbt Core.

    All user-facing failures are raised as :class:`EnhanceError`. The request
    selects either the default persistent worker or the explicit process backend.
    The caller can set ``cancel`` from another thread to cancel active work.
    """

    _validate_request(request)
    if request.backend == "auto":
        from .preflight import resolve_context
        from .support import select_backend

        started = time.monotonic()
        try:
            context = resolve_context(request, cancel=cancel, on_status=on_status)
        except EnhanceError as exc:
            if exc.code != "unsupported_environment" or request.allowed_targets is not None:
                raise
            # No SQL was compiled or dispatched. An unverified interpreter can
            # only be offered the explicit public process path, never a worker.
            context = None
        choice = select_backend("auto", context)
        if context is not None:
            _notify(
                on_status,
                QueryStatus(
                    "Native target verified — selecting execution backend",
                    stage="preflight",
                    target=context.target,
                    target_verified=True,
                    backend="auto",
                    support_tier=choice.tier,
                ),
            )
        if choice.backend is None:
            raise BackendChoiceRequired(choice.choices, context=context)
        remaining = float(request.timeout) - (time.monotonic() - started)
        if remaining <= 0:
            raise EnhanceError(
                "Query timed out during preflight.", code="timeout", stage="preflight"
            )
        request = replace(
            request,
            backend=choice.backend,
            backend_origin="certified_default",
            target=context.target if context is not None else request.target,
            timeout=remaining,
        )
    if request.backend == "worker":
        from .worker import run_query as run_worker_query

        return run_worker_query(request, cancel, on_status)
    cancel_event = cancel if cancel is not None else threading.Event()
    if cancel_event.is_set():
        raise EnhanceError(
            "Query was cancelled before dbt started.", code="cancelled", stage="preflight"
        )

    project_dir = discover_project(request.file)
    marker = project_dir / "dbt_project.yml"
    if not marker.is_file():  # Defensive: the project may have disappeared.
        raise EnhanceError(f"dbt project file no longer exists: {marker}")
    dbt = find_dbt(project_dir, request.dbt)

    started = time.monotonic()
    _notify(on_status, "Running dbt query")

    try:
        from .preflight import resolve_context
        from .settings import check_target_allowed

        try:
            context = resolve_context(request, cancel=cancel_event, on_status=on_status)
        except EnhanceError as exc:
            # The public process escape hatch still supports custom wrappers and
            # unadmitted Core lines, but NEVER when target proof is required.
            if exc.code != "unsupported_environment" or request.allowed_targets is not None:
                raise
            context = None
        if context is not None:
            check_target_allowed(request.allowed_targets, project_dir, context.target)
            # Pin the proven native target. A later default-target change cannot
            # redirect this invocation under a previously approved name.
            request = replace(request, target=context.target)
            _notify(
                on_status,
                QueryStatus(
                    "Native target verified — executing preview",
                    stage="execution",
                    target=context.target,
                    target_verified=True,
                    backend="process",
                    support_tier="best-effort",
                ),
            )
        remaining = float(request.timeout) - (time.monotonic() - started)
        if remaining <= 0:
            raise EnhanceError(
                "Query timed out during preflight.", code="timeout", stage="preflight"
            )
        # Let dbt resolve its normal artifact/cache paths, including project and
        # environment overrides. This reuses and updates partial_parse.msgpack.
        command = [
            str(dbt),
            "show",
            "--inline",
            _preview_sql(request.sql),
            "--output",
            "json",
            "--log-format",
            "json",
            "--log-level",
            "debug",  # ArtifactWritten reports the resolved run_results.json path.
            "--no-use-colors",
            "--no-send-anonymous-usage-stats",
            "--no-upload-to-artifacts-ingest-api",
            "--write-json",
            "--limit",
            str(request.limit),
            "--project-dir",
            str(project_dir),
        ]
        if request.target is not None:
            command.extend(("--target", request.target))
        if request.profiles_dir is not None:
            command.extend(("--profiles-dir", str(request.profiles_dir)))

        return_code, output = _run_process(
            command,
            cwd=project_dir,
            timeout=remaining,
            cancel=cancel_event,
            on_status=on_status,
        )
        if return_code != 0:
            detail = _dbt_error_detail(output)
            raise EnhanceError(f"dbt query failed with exit code {return_code}: {detail}")

        events = _json_events(output)
        event = _show_event(events)
        preview = _parse_preview(event["data"])
        compiled_sql = _compiled_sql(project_dir, events, event)
        columns, rows, warnings = _rows_from_preview(preview)

        elapsed = time.monotonic() - started
        _notify(on_status, f"Query completed in {elapsed:.2f}s")
        return QueryResult(
            columns=columns,
            rows=rows,
            compiled_sql=compiled_sql,
            elapsed=elapsed,
            project_dir=project_dir,
            limit=request.limit,
            target=request.target,
            warnings=warnings,
            target_verified=context is not None,
            backend="process",
            adapter=context.adapter if context else None,
            support_tier="best-effort",
            session_state="isolated",
            versions=dict(context.versions) if context else {},
            timings={"total": elapsed},
            sql_provenance="process_run_results",
        )
    except EnhanceError:
        raise
    except OSError as exc:
        raise EnhanceError(f"Could not run dbt: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        # The UI contract must never expose an implementation traceback.
        raise EnhanceError(f"Could not process dbt's result: {exc}") from None


def _preview_sql(sql: str) -> str:
    """Normalize a selection for dbt's appended preview LIMIT, without rendering Jinja.

    Strip one terminal delimiter, ignoring literals/comments. If the selection
    has a literal top-level LIMIT, nest it so dbt can still enforce the requested
    cap. Leave other queries unchanged (particularly their outer ORDER BY).
    Clauses or delimiters generated entirely by Jinja cannot be detected here.
    """
    pieces = list(lexer.tokenize(sql))
    for index in range(len(pieces) - 1, -1, -1):
        kind, value = pieces[index]
        if kind in tokens.Whitespace or kind in tokens.Comment:
            continue
        if kind is tokens.Punctuation and value == ";":
            del pieces[index]
        break
    normalized = "".join(value for _, value in pieces)
    depth = 0
    template_end: str | None = None
    for index, (kind, value) in enumerate(pieces):
        following = pieces[index + 1][1] if index + 1 < len(pieces) else ""
        pair = value + following
        if template_end is not None:
            if pair == template_end:
                template_end = None
            continue
        if pair in {"{{", "{%", "{#"}:
            template_end = {"{{": "}}", "{%": "%}", "{#": "#}"}[pair]
        elif kind is tokens.Punctuation:
            if value == "(":
                depth += 1
            elif value == ")":
                depth -= 1
        elif depth == 0 and kind in tokens.Keyword and value.upper() == "LIMIT":
            return f"select * from (\n{normalized}\n) dbt_enhance_preview"
    return normalized


def _validate_request(request: QueryRequest) -> None:
    def invalid(message: str) -> EnhanceError:
        return EnhanceError(message, code="invalid_request", stage="preflight")

    def valid_name(value: object) -> bool:
        return isinstance(value, str) and bool(value) and not any(c in value for c in "\0\r\n")

    if not isinstance(request.backend, str) or request.backend not in {"auto", "process", "worker"}:
        raise invalid("Query backend must be 'auto', 'process', or 'worker'.")
    if not isinstance(request.sql, str) or not request.sql.strip():
        raise invalid("SQL must not be empty.")
    try:
        sql_size = len(request.sql.encode("utf-8"))
    except UnicodeEncodeError:
        raise invalid("SQL must be valid UTF-8 text.") from None
    if sql_size > _MAX_SQL_BYTES:
        raise invalid("SQL must not exceed 1 MiB when encoded as UTF-8.")
    for value, name in (
        (request.timeout, "Query timeout"),
        (request.worker_idle_seconds, "Worker idle timeout"),
    ):
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        try:
            valid = valid and math.isfinite(float(value)) and value > 0
        except OverflowError:
            valid = False
        if not valid:
            raise invalid(f"{name} must be a positive, finite number of seconds.")
    if request.worker_idle_seconds > 86400:
        raise invalid("Worker idle timeout must not exceed 86400 seconds.")
    if (
        isinstance(request.limit, bool)
        or not isinstance(request.limit, int)
        or not 1 <= request.limit <= 10_000
    ):
        raise invalid("Query limit must be an integer between 1 and 10000.")
    if not isinstance(request.file, Path):
        raise invalid("The source file must be provided as a pathlib.Path.")
    for value, name in (
        (request.profiles_dir, "Profiles directory"),
        (request.dbt, "dbt executable"),
    ):
        if value is not None and not isinstance(value, Path):
            raise invalid(f"{name} must be a pathlib.Path or None.")
    if request.target is not None and not valid_name(request.target):
        raise invalid("Target must be a nonempty name without NUL or newlines.")
    if request.allowed_targets is not None and (
        not isinstance(request.allowed_targets, (tuple, list))
        or not all(valid_name(target) for target in request.allowed_targets)
    ):
        raise invalid("Allowed targets must be a sequence of nonempty target names or None.")
    if not isinstance(request.diagnostics, bool):
        raise invalid("Diagnostics must be a boolean.")
    if not isinstance(request.theme, str) or request.theme not in {"auto", "light", "dark"}:
        raise invalid("Theme must be auto, light, or dark.")
    if not valid_name(request.backend_origin):
        raise invalid("Backend origin must be a nonempty name.")


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


class _BoundedOutput:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self._data = bytearray()
        self._overflowed = False
        self._sso_pending = False
        self._tail = b"\n"
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        with self._lock:
            remaining = self.maximum - len(self._data)
            if remaining > 0:
                self._data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self._overflowed = True
            # Match only connector prompt lines, not arbitrary SQL/rows inside JSON.
            # Retain a tiny overlap so a prompt split across pipe reads is recognized.
            tail = self._tail + chunk
            self._sso_pending |= any(marker in tail for marker in _SSO_MARKERS)
            self._tail = tail[-256:]

    def sso_pending(self) -> bool:
        with self._lock:
            return self._sso_pending

    def text(self) -> str:
        with self._lock:
            return bytes(self._data).decode("utf-8", errors="replace")

    def overflowed(self) -> bool:
        with self._lock:
            return self._overflowed


def _child_environment() -> dict[str, str]:
    """Shared preview defaults without modifying the editor's environment."""
    environment = os.environ.copy()
    # Connector prompts must reach the reader while authentication is pending,
    # rather than remaining in Python's block-buffered redirected stdout.
    environment["PYTHONUNBUFFERED"] = "1"
    # Opt out of optional cloud-platform probing where the connector supports it.
    # Preserve explicit operator preferences and all profile/Jinja environment inputs.
    environment.setdefault("SNOWFLAKE_DISABLE_PLATFORM_DETECTION", "true")
    # A preview does not materialize models; skip eager whole-project relation
    # discovery. Introspection still queries on demand. Allow projects to opt back in.
    environment.setdefault("DBT_POPULATE_CACHE", "false")
    return environment


def _run_process(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    cancel: threading.Event,
    on_status: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    environment = _child_environment()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise EnhanceError(f"Could not start dbt executable {command[0]}: {exc}") from None

    captured = _BoundedOutput(_MAX_CAPTURE_BYTES)

    def drain() -> None:
        assert process.stdout is not None
        while chunk := process.stdout.read1(8192):
            captured.append(chunk)

    reader = threading.Thread(target=drain, name="dbt-output-reader", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    reader_stuck = False
    sso_reported = False
    failure: BaseException | None = None
    try:
        while process.poll() is None:
            if cancel.is_set():
                raise EnhanceError("Query was cancelled.", code="cancelled")
            if captured.overflowed():
                raise EnhanceError(_OUTPUT_SIZE_ERROR, code="output_limit")
            if time.monotonic() >= deadline:
                detail = f"dbt query timed out after {timeout:g} seconds."
                if captured.sso_pending():
                    detail += "\n" + SSO_GUIDANCE
                raise EnhanceError(detail, code="timeout")
            if captured.sso_pending() and not sso_reported:
                sso_reported = True
                _notify(on_status, SSO_STATUS)
            time.sleep(_CANCEL_POLL_SECONDS)
    except BaseException as exc:
        failure = exc
    finally:
        # No unbounded waits: WSL/browser interop may fail to reap after SIGKILL.
        # Preserve the primary timeout/cancellation if cleanup itself fails.
        # A descendant can also keep stdout open; never close a BufferedReader
        # while its reader thread holds the internal lock.
        try:
            if isinstance(failure, EnhanceError) and failure.code in {"cancelled", "timeout"}:
                # dbt's public CLI handles KeyboardInterrupt by calling its
                # adapter cancellation path. Give that request a short grace,
                # then retain the independent TERM/KILL safety net. An accepted
                # signal is NOT evidence that warehouse work actually stopped.
                failure.cancellation = "requested_unverified"
                try:
                    _signal_group(process, signal.SIGINT)
                    process.wait(timeout=0.25)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            _terminate_process_group(process)
        except (EnhanceError, OSError) as exc:
            if failure is None:
                failure = exc
        finally:
            reader.join(timeout=1.0)
            reader_stuck = reader.is_alive()
            if not reader_stuck and process.stdout is not None:
                process.stdout.close()

    if failure is not None:
        raise failure
    if reader_stuck:
        raise EnhanceError(
            "dbt exited but a child process kept its output stream open; "
            "process-group cleanup could not complete."
        )
    if captured.overflowed():
        raise EnhanceError(_OUTPUT_SIZE_ERROR, code="output_limit")

    return process.returncode, captured.text()


def _signal_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # XNU's killpg1 can return EPERM when only zombies remain in a group.
        # Do not lose a successful result to that cleanup race; real permission
        # errors (including a still-running leader) must remain visible.
        if not _IS_DARWIN or process.poll() is None:
            raise


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate dbt and every local child it placed in its process group."""

    _signal_group(process, signal.SIGTERM)

    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass

    # Kill the group even when its leader exited: a child may still be alive.
    _signal_group(process, signal.SIGKILL)

    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            raise EnhanceError(
                "dbt did not exit after termination; local cleanup could not complete. "
                "Check for remaining dbt processes and running warehouse queries."
            ) from None


def _json_events(output: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _show_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        info = event.get("info")
        if isinstance(info, dict) and info.get("name") == "ShowNode":
            data = event.get("data")
            if isinstance(data, dict):
                return event
    raise EnhanceError("dbt exited successfully but did not emit a ShowNode JSON preview event.")


def _parse_preview(event: dict[str, Any]) -> list[dict[str, object]]:
    raw = event.get("preview")
    if not isinstance(raw, str):
        raise EnhanceError("dbt's ShowNode event did not contain a JSON preview.")
    try:
        preview = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EnhanceError(f"dbt emitted an invalid JSON preview: {exc.msg}") from None
    if not isinstance(preview, list) or not all(isinstance(row, dict) for row in preview):
        raise EnhanceError("dbt's JSON preview was not a list of row objects.")
    return preview


def _compiled_sql(
    project_dir: Path, events: list[dict[str, Any]], show_event: dict[str, Any]
) -> str:
    invocation_id = show_event["info"].get("invocation_id")
    if not isinstance(invocation_id, str) or not invocation_id:
        raise EnhanceError("dbt's ShowNode event is missing its invocation ID.")

    artifact: Path | None = None
    for event in reversed(events):
        info, data = event.get("info"), event.get("data")
        if not isinstance(info, dict) or not isinstance(data, dict):
            continue
        if info.get("name") != "ArtifactWritten" or info.get("invocation_id") != invocation_id:
            continue
        path = data.get("artifact_path")
        if isinstance(path, str) and Path(path).name == "run_results.json":
            artifact = project_dir / path  # Absolute paths remain absolute.
            break
    if artifact is None:
        raise EnhanceError("dbt did not report a run_results.json artifact for this query.")
    try:
        document = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnhanceError(
            f"Could not read dbt's compiled run_results.json artifact: {exc}"
        ) from None

    metadata = document.get("metadata") if isinstance(document, dict) else None
    if not isinstance(metadata, dict) or metadata.get("invocation_id") != invocation_id:
        raise EnhanceError(
            "dbt's run_results.json belongs to another invocation. A concurrent dbt command "
            "may have replaced it; retry after that command finishes."
        )
    results = document.get("results") if isinstance(document, dict) else None
    if not isinstance(results, list):
        raise EnhanceError("dbt's run_results.json artifact has no results list.")

    unique_id = show_event["data"].get("unique_id")
    matching = [
        item
        for item in results
        if isinstance(item, dict) and (unique_id is None or item.get("unique_id") == unique_id)
    ]
    if len(matching) != 1:
        raise EnhanceError(
            "dbt exited successfully but its compiled artifact did not contain "
            "the inline preview result."
        )

    result = matching[0]
    status = result.get("status")
    if status != "success":
        raise EnhanceError(
            "dbt exited successfully but its inline artifact reported "
            f"status {status!r}, not 'success'."
        )
    compiled = result.get("compiled_code")
    if not isinstance(compiled, str) or not compiled.strip():
        raise EnhanceError(
            "dbt exited successfully but the inline result had no compiled SQL artifact."
        )
    return compiled


def _rows_from_preview(
    preview: list[dict[str, object]],
) -> tuple[list[str], list[list[object]], list[str]]:
    if not preview:
        return [], [], [_EMPTY_COLUMNS_WARNING]
    columns = list(preview[0])
    rows = [[row.get(column) for column in columns] for row in preview]
    return columns, rows, []


def _dbt_error_detail(output: str) -> str:
    raw = b"\n" + output.encode("utf-8")
    sso_seen = any(marker in raw for marker in _SSO_MARKERS)
    if (sso_seen and "EOF when reading a line" in output) or "SSO URL contained no token" in output:
        return SSO_GUIDANCE
    messages: list[str] = []
    for event in _json_events(output):
        info = event.get("info")
        if not isinstance(info, dict) or info.get("level") not in {"error", "fatal"}:
            continue
        message = info.get("msg")
        if isinstance(message, str) and message.strip():
            messages.append(message.strip())
    if messages:
        return messages[-1]
    if sso_seen:
        # Plain connector diagnostics can contain secret-bearing login URLs.
        # Do not echo those into the UI or machine-readable CLI error stream.
        return SSO_GUIDANCE

    text = output.strip()
    if text:
        return text[-4000:]
    return "dbt produced no error details; rerun dbt directly for diagnostics."


def _notify(callback: Callable[[str], None] | None, status: str) -> None:
    if callback is None:
        return
    try:
        callback(status)
    except Exception:  # noqa: BLE001
        # Progress reporting must not interrupt or leak a running dbt process.
        return
