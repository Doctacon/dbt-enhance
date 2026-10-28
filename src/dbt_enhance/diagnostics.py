"""Private, bounded diagnostics with a deliberately narrow data vocabulary."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_FAILURES = 20
_MAX_FILE_BYTES = 256 * 1024
_MAX_TIMING_SECONDS = 7 * 24 * 60 * 60
_STATE_NAME = re.compile(r"^[0-9a-f]{64}\.json$")
_TEMP_NAME = re.compile(r"^\.diagnostics-[0-9a-f]{16}$")
_VERSION = re.compile(
    r"^(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?"
    r"(?:\.post[0-9]+)?(?:\.dev[0-9]+)?"
    r"(?:\+[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*)?$"
)

_OUTCOMES = {"success", "failure", "cancelled", "timeout"}
_STAGES = {
    "preflight",
    "queue",
    "compilation",
    "connection",
    "execution",
    "fetch",
    "render",
    "cleanup",
}
_REASONS = {
    "query_failed",
    "configuration_invalid",
    "target_not_allowed",
    "backend_choice_required",
    "unsupported_environment",
    "timeout",
    "cancelled",
    "output_limit",
    "worker_failure",
    "sql_error",
    "compilation_error",
    "connection_error",
    "unknown",
}
_BACKENDS = {"auto", "process", "worker", "unknown"}
_CANCELLATION = {"not_requested", "requested_unverified", "confirmed", "unknown"}
_SUPPORT_STATUSES = {
    "certified",
    "provisional",
    "best_effort",
    "best-effort",
    "unsupported",
    "unknown",
}
_PLATFORMS = {"linux", "macos", "windows", "other"}
_VERSION_KEYS = {
    "tool",
    "python",
    "dbt-core",
    "dbt-adapter",
    "dbt-duckdb",
    "dbt-snowflake",
    "snowflake-connector-python",
}
_VERSION_ALIASES = {
    "dbt_core": "dbt-core",
    "dbt_adapter": "dbt-adapter",
    "dbt_duckdb": "dbt-duckdb",
    "dbt_snowflake": "dbt-snowflake",
    "snowflake_connector_python": "snowflake-connector-python",
}
_TIMING_KEYS = {
    "preflight",
    "queue",
    "compilation",
    "connection",
    "execution",
    "fetch",
    "render",
    "cleanup",
    "elapsed",
    "total",
}


class DiagnosticStore:
    """Persist only sanitized diagnostic facts, keyed by an opaque project digest."""

    def __init__(
        self,
        root: Path | None = None,
        clock: Callable[[], float] = time.time,
        enabled: bool = True,
    ):
        self.root = root if root is not None else _default_root()
        self.clock = clock
        self.enabled = enabled

    def record_latest(self, project: Path, status: Mapping[str, object]) -> None:
        if not self.enabled:
            return
        try:
            now = _valid_clock(self.clock())
            record = _sanitize_record(status, timestamp=now)
            if record is None:
                return
            root = self._root(create=True)
            if root is None:
                return
            salt = self._salt(root, create=True)
            if salt is None:
                return
            self._sweep(root, now)
            path = self._state_path(root, salt, project)
            state = self._read_state(path)
            state["latest"] = record
            state = _prune_state(state, now)
            self._write_state(path, state)
        except (OSError, ValueError, TypeError):
            return

    def record_failure(self, project: Path, failure: Mapping[str, object]) -> None:
        if not self.enabled:
            return
        try:
            now = _valid_clock(self.clock())
            record = _sanitize_record(failure, timestamp=now, force_failure=True)
            if record is None:
                return
            root = self._root(create=True)
            if root is None:
                return
            salt = self._salt(root, create=True)
            if salt is None:
                return
            self._sweep(root, now)
            path = self._state_path(root, salt, project)
            state = self._read_state(path)
            failures = state.get("failures", [])
            if not isinstance(failures, list):
                failures = []
            failures.append(record)
            state = {"latest": record, "failures": failures}
            self._write_state(path, _prune_state(state, now))
        except (OSError, ValueError, TypeError):
            return

    def latest(self, project: Path) -> dict[str, object] | None:
        if not self.enabled:
            return None
        try:
            state = self._load_project(project)
            latest = state.get("latest")
            return dict(latest) if isinstance(latest, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def failures(self, project: Path) -> tuple[dict[str, object], ...]:
        if not self.enabled:
            return ()
        try:
            state = self._load_project(project)
            failures = state.get("failures", [])
            if not isinstance(failures, list):
                return ()
            return tuple(dict(item) for item in failures if isinstance(item, dict))
        except (OSError, ValueError, TypeError):
            return ()

    def purge(self, project: Path | None = None) -> None:
        """Delete only recognized, privately owned diagnostic files."""

        try:
            root = self._root(create=False)
            if root is None:
                return
            if project is not None:
                salt = self._salt(root, create=False)
                if salt is None:
                    return
                _safe_unlink(self._state_path(root, salt, project))
                return
            for child in root.iterdir():
                if (
                    _STATE_NAME.fullmatch(child.name) or _TEMP_NAME.fullmatch(child.name)
                ) and _safe_file(child):
                    child.unlink()
            salt_path = root / "salt"
            if _safe_file(salt_path):
                salt_path.unlink()
            try:
                root.rmdir()
            except OSError:
                pass
        except (OSError, ValueError, TypeError):
            return

    def _load_project(self, project: Path) -> dict[str, Any]:
        now = _valid_clock(self.clock())
        root = self._root(create=False)
        if root is None:
            return {"latest": None, "failures": []}
        salt = self._salt(root, create=False)
        if salt is None:
            return {"latest": None, "failures": []}
        self._sweep(root, now)
        path = self._state_path(root, salt, project)
        original = self._read_state(path)
        pruned = _prune_state(original, now)
        if pruned != original:
            if pruned["latest"] is None and not pruned["failures"]:
                _safe_unlink(path)
            else:
                self._write_state(path, pruned)
        return pruned

    def _root(self, *, create: bool) -> Path | None:
        try:
            info = self.root.lstat()
        except FileNotFoundError:
            if not create:
                return None
            _mkdir_private_chain(self.root)
            info = self.root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            return None
        return self.root

    def _salt(self, root: Path, *, create: bool) -> bytes | None:
        path = root / "salt"
        if _safe_file(path):
            value = _read_private(path, maximum=64)
            return value if len(value) == 32 else None
        if path.exists() or not create:
            return None
        value = secrets.token_bytes(32)
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(value)
            return value
        except FileExistsError:
            if not _safe_file(path):
                return None
            existing = _read_private(path, maximum=64)
            return existing if len(existing) == 32 else None

    @staticmethod
    def _state_path(root: Path, salt: bytes, project: Path) -> Path:
        canonical = os.fsencode(project.expanduser().resolve(strict=False))
        key = hmac.digest(salt, canonical, hashlib.sha256).hex()
        return root / f"{key}.json"

    def _read_state(self, path: Path) -> dict[str, Any]:
        try:
            if not _safe_file(path):
                return {"latest": None, "failures": []}
            raw = _read_private(path, maximum=_MAX_FILE_BYTES)
            value = json.loads(raw)
        except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return {"latest": None, "failures": []}
        return value if isinstance(value, dict) else {"latest": None, "failures": []}

    def _write_state(self, path: Path, state: Mapping[str, object]) -> None:
        raw = json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if len(raw) > _MAX_FILE_BYTES:
            raise ValueError("diagnostic state exceeds its bound")
        if path.exists() and not _safe_file(path):
            return
        temporary = path.with_name(f".diagnostics-{secrets.token_hex(8)}")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _sweep(self, root: Path, now: float) -> None:
        for child in root.iterdir():
            if not _STATE_NAME.fullmatch(child.name) or not _safe_file(child):
                continue
            original = self._read_state(child)
            pruned = _prune_state(original, now)
            if pruned["latest"] is None and not pruned["failures"]:
                _safe_unlink(child)
            elif pruned != original:
                self._write_state(child, pruned)


def build_shareable_report(
    latest: Mapping[str, object] | None, failures: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    """Build JSON-safe output by sanitizing independently of persisted state."""

    safe_latest = _sanitize_record(latest) if isinstance(latest, Mapping) else None
    safe_failures: list[dict[str, object]] = []
    if isinstance(failures, Sequence) and not isinstance(failures, (str, bytes)):
        for failure in failures[-_MAX_FAILURES:]:
            if not isinstance(failure, Mapping):
                continue
            sanitized = _sanitize_record(failure, force_failure=True)
            if sanitized is not None:
                safe_failures.append(sanitized)
    return {"schema": 1, "latest": safe_latest, "failures": safe_failures}


def _default_root() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "dbt-enhance" / "diagnostics"


def _sanitize_record(
    value: Mapping[str, object] | None,
    *,
    timestamp: float | None = None,
    force_failure: bool = False,
) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, object] = {}
    selected_timestamp = timestamp if timestamp is not None else value.get("timestamp")
    if _bounded_number(selected_timestamp, maximum=10**12):
        result["timestamp"] = float(selected_timestamp)

    outcome = value.get("outcome")
    if force_failure:
        result["outcome"] = (
            outcome
            if isinstance(outcome, str) and outcome in {"failure", "cancelled", "timeout"}
            else "failure"
        )
    elif isinstance(outcome, str) and outcome in _OUTCOMES:
        result["outcome"] = outcome
    else:
        return None

    for key, allowed in (
        ("stage", _STAGES),
        ("backend", _BACKENDS),
        ("cancellation", _CANCELLATION),
    ):
        selected = value.get(key)
        if isinstance(selected, str) and selected in allowed:
            result[key] = selected
    reason = value.get("reason", value.get("code"))
    if isinstance(reason, str) and reason in _REASONS:
        result["reason"] = reason

    support = value.get("support_status", value.get("support_tier"))
    if isinstance(support, str) and support in _SUPPORT_STATUSES:
        result["support_status"] = support

    platform = value.get("platform")
    aliases = {"darwin": "macos", "win32": "windows"}
    if isinstance(platform, str):
        platform = aliases.get(platform, platform)
    if isinstance(platform, str) and platform in _PLATFORMS:
        result["platform"] = platform

    versions = value.get("versions")
    safe_versions: dict[str, str] = {}
    if isinstance(versions, Mapping):
        candidates = {key: versions.get(key) for key in _VERSION_KEYS}
        for alias, canonical in _VERSION_ALIASES.items():
            if candidates[canonical] is None:
                candidates[canonical] = versions.get(alias)
        for key, version in candidates.items():
            if (
                isinstance(version, str)
                and len(version) <= 64
                and _VERSION.fullmatch(version) is not None
            ):
                safe_versions[key] = version
    if safe_versions:
        result["versions"] = safe_versions

    timings = value.get("timings")
    safe_timings: dict[str, float] = {}
    if isinstance(timings, Mapping):
        for key in _TIMING_KEYS:
            timing = timings.get(key)
            if _bounded_number(timing, maximum=_MAX_TIMING_SECONDS):
                safe_timings[key] = float(timing)
    if safe_timings:
        result["timings"] = safe_timings
    return result


def _prune_state(value: Mapping[str, object], now: float) -> dict[str, Any]:
    cutoff = now - _RETENTION_SECONDS

    def retained(candidate: object, *, failure: bool = False) -> dict[str, object] | None:
        if not isinstance(candidate, Mapping):
            return None
        sanitized = _sanitize_record(candidate, force_failure=failure)
        if sanitized is None:
            return None
        timestamp = sanitized.get("timestamp")
        if not isinstance(timestamp, float) or not cutoff <= timestamp <= now + 300:
            return None
        return sanitized

    latest = retained(value.get("latest"))
    raw_failures = value.get("failures", [])
    failures: list[dict[str, object]] = []
    if isinstance(raw_failures, list):
        for item in raw_failures:
            sanitized = retained(item, failure=True)
            if sanitized is not None:
                failures.append(sanitized)
    return {"latest": latest, "failures": failures[-_MAX_FAILURES:]}


def _valid_clock(value: object) -> float:
    if not _bounded_number(value, maximum=10**12):
        raise ValueError("invalid diagnostics clock")
    return float(value)


def _bounded_number(value: object, *, maximum: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and 0 <= value <= maximum
        and math.isfinite(float(value))
    )


def _mkdir_private_chain(path: Path) -> None:
    missing: list[Path] = []
    candidate = path
    while True:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            missing.append(candidate)
            parent = candidate.parent
            if parent == candidate:
                raise OSError("diagnostics path has no existing ancestor") from None
            candidate = parent
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("diagnostics parent is not a non-symlink directory")
        break
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise OSError("diagnostics directory is not private")


def _safe_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
    )


def _read_private(path: Path, *, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ValueError("diagnostic file is not private")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("diagnostic file exceeds its bound")
        return raw
    finally:
        os.close(descriptor)


def _safe_unlink(path: Path) -> None:
    if _safe_file(path):
        path.unlink()
