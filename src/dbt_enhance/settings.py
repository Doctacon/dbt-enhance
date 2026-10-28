"""Strict, side-effect-free user preference loading and resolution."""

from __future__ import annotations

import math
import os
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dbt_enhance.models import EnhanceError

_OPERATIONAL_KEYS = (
    "limit",
    "timeout",
    "target",
    "profiles_dir",
    "dbt",
    "backend",
    "worker_idle_seconds",
    "diagnostics",
    "theme",
)
_BUILTINS: dict[str, object] = {
    "limit": 500,
    "timeout": 120.0,
    "target": None,
    "profiles_dir": None,
    "dbt": None,
    "backend": "worker",
    "worker_idle_seconds": 600.0,
    "diagnostics": True,
    "theme": "auto",
}
_ENVIRONMENT_KEYS = {
    "DBT_ENHANCE_LIMIT": "limit",
    "DBT_ENHANCE_TIMEOUT": "timeout",
    "DBT_ENHANCE_TARGET": "target",
    "DBT_ENHANCE_PROFILES_DIR": "profiles_dir",
    "DBT_ENHANCE_DBT": "dbt",
    "DBT_ENHANCE_BACKEND": "backend",
    "DBT_ENHANCE_WORKER_IDLE_SECONDS": "worker_idle_seconds",
    "DBT_ENHANCE_DIAGNOSTICS": "diagnostics",
    "DBT_ENHANCE_THEME": "theme",
}
_MAX_CONFIG_BYTES = 1024 * 1024


@dataclass(frozen=True)
class UserSettings:
    """Validated contents of the user-owned TOML file."""

    schema: int = 1
    global_values: dict[str, object] = field(default_factory=dict)
    projects: dict[Path, dict[str, object]] = field(default_factory=dict)
    safety: dict[Path, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class EffectiveSettings:
    """Operational values, their provenance, and non-overridable safety policy."""

    values: dict[str, object]
    sources: dict[str, str]
    allowed_targets: tuple[str, ...] | None


def settings_path(environment: Mapping[str, str] | None = None) -> Path:
    """Return the sole supported user configuration path."""

    environment = os.environ if environment is None else environment
    base = environment.get("XDG_CONFIG_HOME")
    if base:
        return Path(base).expanduser() / "dbt-enhance" / "config.toml"
    return Path.home() / ".config" / "dbt-enhance" / "config.toml"


def load_user_settings(path: Path | None = None) -> UserSettings:
    """Load and fully validate user settings without writing anything."""

    candidate = path if path is not None else settings_path()
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise _invalid("User settings must be a regular file, not a pipe or device.")
            # Dotfile symlinks remain supported; validate the opened target rather
            # than trusting an earlier path check. Read-only root-managed targets
            # are also safe, but another user must not be able to edit the policy.
            if info.st_uid not in {os.getuid(), 0} or info.st_mode & 0o022:
                raise _invalid("User settings must not be owned or writable by other users.")
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return UserSettings()
    except OSError as exc:
        raise _invalid(f"Cannot read user settings: {exc}") from exc
    if len(raw) > _MAX_CONFIG_BYTES:
        raise _invalid("User settings exceed the 1 MiB limit.")
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _invalid(f"User settings are not valid TOML: {exc}") from exc

    _only_keys(document, {"schema", "global", "project", "safety"}, "top level")
    schema = document.get("schema", 1)
    if isinstance(schema, bool) or schema != 1:
        raise _invalid("User settings schema must be 1.")

    global_table = document.get("global", {})
    if not isinstance(global_table, dict):
        raise _invalid("[global] must be a table.")
    global_values = _validate_values(global_table, source="TOML")

    project_entries = document.get("project", [])
    if not isinstance(project_entries, list):
        raise _invalid("[[project]] must be an array of tables.")
    projects: dict[Path, dict[str, object]] = {}
    for entry in project_entries:
        if not isinstance(entry, dict):
            raise _invalid("Each [[project]] entry must be a table.")
        _only_keys(entry, {"path", *_OPERATIONAL_KEYS}, "[[project]]")
        project = _project_root(entry.get("path"), "project.path")
        if project in projects:
            raise _invalid("Duplicate canonical [[project]] path.")
        projects[project] = _validate_values(
            {key: value for key, value in entry.items() if key != "path"}, source="TOML"
        )

    safety_table = document.get("safety", {})
    if not isinstance(safety_table, dict):
        raise _invalid("[safety] must be a table.")
    _only_keys(safety_table, {"project_targets"}, "[safety]")
    policy_entries = safety_table.get("project_targets", [])
    if not isinstance(policy_entries, list):
        raise _invalid("[[safety.project_targets]] must be an array of tables.")
    safety: dict[Path, tuple[str, ...]] = {}
    for entry in policy_entries:
        if not isinstance(entry, dict):
            raise _invalid("Each safety project policy must be a table.")
        _only_keys(entry, {"project", "targets"}, "[[safety.project_targets]]")
        project = _project_root(entry.get("project"), "safety project")
        if project in safety:
            raise _invalid("Duplicate canonical safety project policy.")
        targets = entry.get("targets")
        if not isinstance(targets, list):
            raise _invalid("Safety targets must be an array of strings.")
        normalized = tuple(_string(value, "safety target") for value in targets)
        if len(set(normalized)) != len(normalized):
            raise _invalid("Safety targets must not contain duplicates.")
        safety[project] = normalized

    return UserSettings(schema=1, global_values=global_values, projects=projects, safety=safety)


def resolve_settings(
    project_dir: Path | None,
    cli: Mapping[str, object] | None = None,
    environment: Mapping[str, str] | None = None,
    user: UserSettings | None = None,
) -> EffectiveSettings:
    """Resolve CLI > environment > project > global > builtin preferences."""

    environment = os.environ if environment is None else environment
    user = load_user_settings() if user is None else user
    if isinstance(user.schema, bool) or user.schema != 1:
        raise _invalid("User settings schema must be 1.")

    values = dict(_BUILTINS)
    sources = {key: "builtin" for key in _OPERATIONAL_KEYS}
    _apply(values, sources, user.global_values, "global", value_source="TOML")

    canonical_project = project_dir.expanduser().resolve(strict=False) if project_dir else None
    if canonical_project is not None:
        _apply(
            values,
            sources,
            user.projects.get(canonical_project, {}),
            "project",
            value_source="TOML",
        )

    # Only documented variables are preferences. Unknown names remain native
    # profile/Jinja inputs, including development switches; do not reserve or
    # rewrite an existing project's environment namespace.
    environment_values = {
        setting: environment[name]
        for name, setting in _ENVIRONMENT_KEYS.items()
        if name in environment
    }
    _apply(
        values,
        sources,
        environment_values,
        "environment",
        value_source="environment",
    )

    cli_values = {} if cli is None else dict(cli)
    _only_keys(cli_values, set(_OPERATIONAL_KEYS), "CLI settings")
    cli_values = {key: value for key, value in cli_values.items() if value is not None}
    _apply(values, sources, cli_values, "cli", value_source="CLI")

    policy = user.safety.get(canonical_project) if canonical_project is not None else None
    return EffectiveSettings(values=values, sources=sources, allowed_targets=policy)


def check_target_allowed(
    policy: tuple[str, ...] | None, project_dir: Path, target_name: str | None
) -> None:
    """Reject a target not present in the project's immutable safety policy."""

    if policy is not None and target_name not in policy:
        raise EnhanceError(
            f"The selected target is not allowed for the dbt project at {project_dir}.",
            code="target_not_allowed",
            stage="preflight",
        )


def _apply(
    values: dict[str, object],
    sources: dict[str, str],
    incoming: Mapping[str, object],
    origin: str,
    *,
    value_source: str,
) -> None:
    validated = _validate_values(incoming, source=value_source)
    for key, value in validated.items():
        values[key] = value
        sources[key] = origin


def _validate_values(values: Mapping[str, object], *, source: str) -> dict[str, object]:
    _only_keys(values, set(_OPERATIONAL_KEYS), f"{source} settings")
    result: dict[str, object] = {}
    for key, value in values.items():
        if key == "limit":
            parsed = _integer(value, key)
            if not 1 <= parsed <= 10_000:
                raise _invalid("limit must be an integer between 1 and 10000.")
            result[key] = parsed
        elif key == "timeout":
            result[key] = _positive_number(value, key)
        elif key == "worker_idle_seconds":
            parsed_number = _positive_number(value, key)
            if parsed_number > 86_400:
                raise _invalid("worker_idle_seconds must not exceed 86400.")
            result[key] = parsed_number
        elif key in {"target"}:
            result[key] = _string(value, key)
        elif key in {"profiles_dir", "dbt"}:
            result[key] = _path_value(value, key, source=source)
        elif key == "backend":
            parsed_string = _string(value, key)
            if parsed_string not in {"auto", "process", "worker"}:
                raise _invalid("backend must be auto, process, or worker.")
            result[key] = parsed_string
        elif key == "diagnostics":
            result[key] = _boolean(value, key)
        elif key == "theme":
            parsed_string = _string(value, key)
            if parsed_string not in {"auto", "dark", "light"}:
                raise _invalid("theme must be auto, dark, or light.")
            result[key] = parsed_string
    return result


def _integer(value: object, name: str) -> int:
    if isinstance(value, str):
        try:
            value = int(value, 10)
        except ValueError:
            raise _invalid(f"{name} must be an integer.") from None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{name} must be an integer.")
    return value


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise _invalid(f"{name} must be a number.") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(f"{name} must be a number.")
    try:
        parsed = float(value)
    except OverflowError:
        raise _invalid(f"{name} must be positive and finite.") from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise _invalid(f"{name} must be positive and finite.")
    return parsed


def _boolean(value: object, name: str) -> bool:
    if isinstance(value, str):
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    if not isinstance(value, bool):
        raise _invalid(f"{name} must be true or false.")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        raise _invalid(f"{name} must be a non-empty string no longer than 255 characters.")
    if any(character in value for character in "\0\r\n"):
        raise _invalid(f"{name} must not contain NUL or newlines.")
    return value


def _path_value(value: object, name: str, *, source: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise _invalid(f"{name} must be a path.")
    path = Path(value)
    if source == "TOML":
        if not path.is_absolute():
            raise _invalid(f"{name} in TOML must be absolute.")
        return path.resolve(strict=False)
    return path.expanduser().absolute()


def _project_root(value: Any, name: str) -> Path:
    if not isinstance(value, str):
        raise _invalid(f"{name} must be an absolute path string.")
    path = Path(value)
    if not path.is_absolute():
        raise _invalid(f"{name} must be absolute.")
    # Preferences can outlive a checkout or an unmounted volume. Validate the
    # path, not every unrelated project's availability; query discovery remains
    # responsible for requiring an actual dbt_project.yml before execution.
    return path.resolve(strict=False)


def _only_keys(values: Mapping[str, object], allowed: set[str], context: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise _invalid(f"Unknown key {unknown[0]!r} in {context}.")


def _invalid(message: str) -> EnhanceError:
    return EnhanceError(message, code="configuration_invalid", stage="preflight")
