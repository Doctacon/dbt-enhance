"""Install a fail-closed Helix transport without interpreting buffer names as shell."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError

from dbt_enhance.models import EnhanceError

_SHORTCUT_PART = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
_MODES = ("normal", "select")
HELIX_BRIDGE_FORMAT = 2
_BRIDGE_FORMAT = HELIX_BRIDGE_FORMAT
_BRIDGE_NAME = "helix-shell-bridge-v2"
_HEADER = "# DBT_ENHANCE_HELIX_BRIDGE_V2:"
_MARKER = "DBT_ENHANCE_BUFFER_V3:"
_MARKER_NAMESPACE = "DBT_ENHANCE_BUFFER_"
_LEGACY_TRANSPORT_PREFIX = "IFS= read -r dbt_enhance_file <<'DBT_ENHANCE_FILE_"
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_METADATA_LIMIT = 16 * 1024
_POSIX_SHELLS = {"ash", "bash", "dash", "ksh", "mksh", "sh", "zsh"}
_DEFAULT_SHELL = ("sh", "-c")


@dataclass(frozen=True)
class BridgeMetadata:
    launcher: tuple[str, ...]
    original_shell: tuple[str, ...]
    original_shell_absent: bool
    fingerprint: str


@dataclass(frozen=True)
class IntegrationPlan:
    config_path: Path
    original_config: str
    updated_config: str
    bridge_path: Path
    bridge_content: str
    existing_bridge_content: str | None
    fingerprint: str


def config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "helix/config.toml"


def bridge_path(state_root: Path) -> Path:
    return state_root.expanduser().absolute() / _BRIDGE_NAME


def _shortcut_path(shortcut: str) -> tuple[str, ...]:
    parts = tuple(shortcut.split("."))
    if not 1 <= len(parts) <= 4 or any(not _SHORTCUT_PART.fullmatch(part) for part in parts):
        raise EnhanceError(
            "The Helix shortcut must contain one to four dot-separated letters, digits, '_' or '-'."
        )
    return parts


def _clean_argv(value: object, *, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or not all(isinstance(item, str) and item for item in value)
        or any(any(character in item for character in "\x00\r\n") for item in value)
        or sum(len(item.encode()) for item in value) > 2048
    ):
        raise EnhanceError(f"Helix {label} must be a bounded non-empty string array.")
    return tuple(value)


def _validate_original_shell(shell: tuple[str, ...]) -> None:
    executable = Path(shell[0]).name
    explicit = "/" in shell[0]
    allowed = shell == (shell[0], "-c")
    if executable == "bash":
        allowed = shell in {
            (shell[0], "-c"),
            (shell[0], "--noprofile", "-c"),
            (shell[0], "--norc", "-c"),
            (shell[0], "--posix", "-c"),
            (shell[0], "--noprofile", "--norc", "-c"),
        }
    elif executable == "zsh":
        allowed = shell in {(shell[0], "-c"), (shell[0], "-f", "-c")}
    if (
        executable not in _POSIX_SHELLS
        or not allowed
        or (explicit and (not Path(shell[0]).is_file() or not os.access(shell[0], os.X_OK)))
    ):
        raise EnhanceError(
            "The configured Helix editor.shell is not a supported noninteractive shell command. "
            "Use exactly ['sh', '-c'], a recognized shell with final '-c', or one of the "
            "individually supported Bash/zsh startup-suppression forms; then reload Helix and "
            "retry."
        )


def _command_prefix() -> str:
    return f") # {_MARKER}"


def _fingerprint(
    launcher: tuple[str, ...],
    original_shell: tuple[str, ...],
    original_shell_absent: bool,
) -> str:
    payload = json.dumps(
        {
            "format": _BRIDGE_FORMAT,
            "launcher": list(launcher),
            "original_shell": list(original_shell),
            "original_shell_absent": original_shell_absent,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def binding(fingerprint: str, *, shortcut: str = "space.x") -> str:
    """Return the fixed parse-failing command consumed by the private shell bridge."""
    _shortcut_path(shortcut)
    if not _FINGERPRINT.fullmatch(fingerprint):
        raise EnhanceError("The Helix bridge fingerprint is invalid.")
    return f":pipe-to {_command_prefix()}{fingerprint} %{{buffer_name}}"


def parse_transport_command(command: str) -> str:
    """Extract a buffer name from the one bridge command argument, without shell parsing."""
    match = re.fullmatch(
        rf"\) # {re.escape(_MARKER)}[0-9a-f]{{64}} (.*)",
        command,
        flags=re.DOTALL,
    )
    if not match:
        raise EnhanceError("Rejected an unknown Helix shell-bridge command.")
    filename = match.group(1)
    if not filename or any(character in filename for character in "\x00\r\n"):
        raise EnhanceError(
            "Helix buffer names must be non-empty and cannot contain newlines or NUL."
        )
    return filename


def _metadata_payload(metadata: BridgeMetadata) -> str:
    raw = json.dumps(
        {
            "fingerprint": metadata.fingerprint,
            "format": _BRIDGE_FORMAT,
            "launcher": list(metadata.launcher),
            "original_shell": list(metadata.original_shell),
            "original_shell_absent": metadata.original_shell_absent,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _bridge_script(metadata: BridgeMetadata) -> str:
    prefix = f"{_command_prefix()}{metadata.fingerprint} "
    launcher = shlex.join([*metadata.launcher, "_helix"])
    original = shlex.join(metadata.original_shell)
    return (
        "#!/bin/sh\n"
        f"{_HEADER}{_metadata_payload(metadata)}\n"
        'if [ "$#" -ne 1 ]; then\n'
        "  printf '%s\\n' 'dbt-enhance Helix bridge expects exactly one command argument' >&2\n"
        "  exit 64\n"
        "fi\n"
        'case "$1" in\n'
        f'  {shlex.quote(prefix)}*) exec {launcher} "$1" ;;\n'
        f"  *{_MARKER_NAMESPACE}*|{shlex.quote(_LEGACY_TRANSPORT_PREFIX)}*)\n"
        "    printf '%s\\n' 'dbt-enhance rejected a reserved Helix transport command' >&2\n"
        "    exit 64 ;;\n"
        f'  *) exec {original} "$1" ;;\n'
        "esac\n"
    )


def _decode_metadata(line: str) -> BridgeMetadata:
    encoded = line.removeprefix(_HEADER)
    if not encoded or len(encoded) > _METADATA_LIMIT:
        raise ValueError
    padded = encoded + "=" * (-len(encoded) % 4)
    raw = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    if not isinstance(raw, dict) or set(raw) != {
        "fingerprint",
        "format",
        "launcher",
        "original_shell",
        "original_shell_absent",
    }:
        raise ValueError
    if raw["format"] != _BRIDGE_FORMAT or not isinstance(raw["original_shell_absent"], bool):
        raise ValueError
    launcher = _clean_argv(raw["launcher"], label="bridge launcher")
    original_shell = _clean_argv(raw["original_shell"], label="original shell")
    _validate_original_shell(original_shell)
    fingerprint = raw["fingerprint"]
    if not isinstance(fingerprint, str) or fingerprint != _fingerprint(
        launcher, original_shell, raw["original_shell_absent"]
    ):
        raise ValueError
    return BridgeMetadata(
        launcher=launcher,
        original_shell=original_shell,
        original_shell_absent=raw["original_shell_absent"],
        fingerprint=fingerprint,
    )


def _owned_bridge(path: Path) -> tuple[str, BridgeMetadata] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_nlink != 1
    ):
        raise EnhanceError(f"Refusing a non-owned or insecure Helix bridge file: {path}")
    try:
        content = path.read_text()
        lines = content.splitlines()
        if (
            len(content.encode()) > _METADATA_LIMIT
            or len(lines) < 2
            or not lines[1].startswith(_HEADER)
        ):
            raise ValueError
        metadata = _decode_metadata(lines[1])
        if content != _bridge_script(metadata):
            raise ValueError
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, EnhanceError) as exc:
        raise EnhanceError(
            f"The Helix bridge at {path} is modified or not owned; it was not overwritten."
        ) from exc
    return content, metadata


def _state_directory(path: Path, *, create: bool) -> None:
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise EnhanceError(f"The private tool state directory does not exist: {path}") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise EnhanceError(
            f"Tool state must be an owned, non-symlink directory with mode 0700: {path}"
        )


def _read_document(path: Path) -> tuple[str, object]:
    try:
        original = path.read_text() if path.exists() else ""
        return original, tomlkit.parse(original)
    except (OSError, TOMLKitError) as exc:
        raise EnhanceError(f"Cannot read Helix config {path}: {exc}") from exc


def _mode_table(document: object, mode: str, parts: tuple[str, ...]) -> object:
    try:
        keys = document.setdefault("keys", tomlkit.table())
        table = keys.setdefault(mode, tomlkit.table())
        if not hasattr(table, "get"):
            raise EnhanceError(f"keys.{mode} is already a command, not a key map.")
        for index, part in enumerate(parts[:-1]):
            table = table.setdefault(part, tomlkit.table())
            if not hasattr(table, "get"):
                prefix = ".".join(("keys", mode, *parts[: index + 1]))
                raise EnhanceError(f"{prefix} is already a command, not a key map.")
        return table
    except (AttributeError, TypeError, KeyError, TOMLKitError) as exc:
        raise EnhanceError(f"Cannot merge Helix key maps: {exc}") from exc


def _legacy_binding(value: object, launcher: tuple[str, ...]) -> bool:
    if not isinstance(value, str):
        return False
    prefix = ":pipe-to IFS= read -r dbt_enhance_file <<'"
    suffix = f'{shlex.join(launcher)} popup --file "$dbt_enhance_file"'
    return bool(
        re.fullmatch(
            re.escape(prefix)
            + r"(DBT_ENHANCE_FILE_[a-f0-9]{32})'\n%\{buffer_name\}\n\1\n"
            + re.escape(suffix),
            value,
        )
    )


def _secure_binding(value: object, fingerprint: str) -> bool:
    return value == binding(fingerprint)


def _recognizable_secure_binding(value: object) -> bool:
    return isinstance(value, str) and value.startswith(":pipe-to ") and _MARKER_NAMESPACE in value


def _configured_shell(document: object) -> tuple[bool, tuple[str, ...]]:
    try:
        editor = document.get("editor")
        if not hasattr(editor, "get") or "shell" not in editor:
            return True, _DEFAULT_SHELL
        return False, _clean_argv(editor.get("shell"), label="editor.shell")
    except (AttributeError, TypeError, KeyError, TOMLKitError) as exc:
        raise EnhanceError(f"Cannot read Helix editor.shell: {exc}") from exc


def prepare_integration(
    path: Path,
    launcher: list[str],
    *,
    state_root: Path,
    helix_shell_bridge: bool = False,
    force: bool = False,
    shortcut: str = "space.x",
) -> IntegrationPlan:
    """Validate a complete bridge/config update without changing either file."""
    parts = _shortcut_path(shortcut)
    launcher_argv = _clean_argv(launcher, label="launcher")
    target = path.expanduser().resolve()
    original, document = _read_document(target)
    root = state_root.expanduser().absolute()
    bridge = bridge_path(root)
    existing = _owned_bridge(bridge)
    if existing is None and not helix_shell_bridge:
        raise EnhanceError(
            "Secure Helix setup requires explicit --helix-shell-bridge consent because it changes "
            "editor.shell. No package or config changes were made."
        )

    shell_absent, configured_shell = _configured_shell(document)
    selected_owned_bridge = configured_shell == (str(bridge),) and existing is not None
    if configured_shell == (str(bridge),) and existing is None:
        raise EnhanceError(
            f"Helix editor.shell names a missing or unowned bridge at {bridge}; restore your shell "
            "setting before setup."
        )
    if selected_owned_bridge:
        assert existing is not None
        original_shell = existing[1].original_shell
        original_shell_absent = existing[1].original_shell_absent
    else:
        if not helix_shell_bridge:
            raise EnhanceError(
                "The owned Helix shell bridge is no longer the configured editor.shell. Re-run "
                "setup --helix-shell-bridge to explicitly adopt the current shell, or restore it."
            )
        original_shell = configured_shell
        original_shell_absent = shell_absent
    if original_shell == (str(bridge),):
        raise EnhanceError("Refusing to create a recursive Helix shell bridge.")
    _validate_original_shell(original_shell)

    fingerprint = _fingerprint(launcher_argv, original_shell, original_shell_absent)
    metadata = BridgeMetadata(launcher_argv, original_shell, original_shell_absent, fingerprint)
    command = binding(fingerprint, shortcut=shortcut)
    previous_fingerprint = existing[1].fingerprint if selected_owned_bridge and existing else None
    try:
        for mode in _MODES:
            table = _mode_table(document, mode, parts)
            old = table.get(parts[-1])
            owned = _secure_binding(old, fingerprint) or (
                previous_fingerprint is not None and _secure_binding(old, previous_fingerprint)
            )
            legacy = _legacy_binding(old, launcher_argv)
            if owned:
                table[parts[-1]] = command
                continue
            if legacy and not helix_shell_bridge:
                raise EnhanceError(
                    "The legacy Helix mapping is unsafe for newline-containing filenames. Re-run "
                    "setup --helix-shell-bridge to migrate it with a backup."
                )
            if old is not None and not legacy and not force:
                location = ".".join(("keys", mode, *parts))
                raise EnhanceError(
                    f"{location} already has a binding. "
                    "Use setup --force to replace it (a backup will be saved)."
                )
            table[parts[-1]] = command
        editor = document.setdefault("editor", tomlkit.table())
        if not hasattr(editor, "get"):
            raise EnhanceError("editor is already a scalar, not a settings table.")
        editor["shell"] = [str(bridge)]
    except (AttributeError, TypeError, KeyError, TOMLKitError) as exc:
        raise EnhanceError(f"Cannot merge Helix settings in {target}: {exc}") from exc

    return IntegrationPlan(
        config_path=target,
        original_config=original,
        updated_config=tomlkit.dumps(document),
        bridge_path=bridge,
        bridge_content=_bridge_script(metadata),
        existing_bridge_content=existing[0] if existing else None,
        fingerprint=fingerprint,
    )


def prepare_config(
    path: Path,
    launcher: list[str],
    *,
    state_root: Path | None = None,
    helix_shell_bridge: bool = False,
    force: bool = False,
    shortcut: str = "space.x",
) -> tuple[str, str]:
    """Compatibility wrapper returning the validated original/new TOML pair."""
    if state_root is None:
        raise EnhanceError("Secure Helix setup requires a private state_root for its shell bridge.")
    plan = prepare_integration(
        path,
        launcher,
        state_root=state_root,
        helix_shell_bridge=helix_shell_bridge,
        force=force,
        shortcut=shortcut,
    )
    return plan.original_config, plan.updated_config


def _atomic_backup(path: Path) -> Path:
    while True:
        backup = path.with_name(f"{path.name}.dbt-enhance-{secrets.token_hex(4)}.bak")
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.backup.", dir=path.parent)
        os.close(fd)
        temporary_path = Path(temporary)
        try:
            shutil.copy2(path, temporary_path)
            with temporary_path.open("rb") as handle:
                os.fsync(handle.fileno())
            try:
                os.link(temporary_path, backup)
            except FileExistsError:
                continue
            return backup
        finally:
            temporary_path.unlink(missing_ok=True)


def _write_config(path: Path, original: str, updated: str) -> Path | None:
    if original == updated:
        return None
    if (path.read_text() if path.exists() else "") != original:
        raise EnhanceError("Helix config changed after validation; retry setup.")
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = _atomic_backup(path) if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if path.exists():
            os.fchmod(fd, stat.S_IMODE(path.stat().st_mode))
        with os.fdopen(fd, "w") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return backup


def _write_bridge(plan: IntegrationPlan) -> None:
    root = plan.bridge_path.parent
    _state_directory(root, create=True)
    current = _owned_bridge(plan.bridge_path)
    current_content = current[0] if current else None
    if current_content != plan.existing_bridge_content:
        raise EnhanceError("Helix bridge changed after validation; it was not overwritten.")
    if current_content == plan.bridge_content:
        return
    fd, temporary = tempfile.mkstemp(prefix=".helix-bridge-", dir=root)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o700)
        with os.fdopen(fd, "w") as handle:
            handle.write(plan.bridge_content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, plan.bridge_path)
        directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary_path.unlink(missing_ok=True)


def apply_integration(plan: IntegrationPlan) -> Path | None:
    """Publish the standalone bridge first, then atomically update Helix TOML."""
    _write_bridge(plan)
    return _write_config(plan.config_path, plan.original_config, plan.updated_config)


def install_config(
    path: Path,
    launcher: list[str],
    *,
    state_root: Path | None = None,
    helix_shell_bridge: bool = False,
    force: bool = False,
    shortcut: str = "space.x",
) -> Path | None:
    if state_root is None:
        raise EnhanceError("Secure Helix setup requires a private state_root for its shell bridge.")
    plan = prepare_integration(
        path,
        launcher,
        state_root=state_root,
        helix_shell_bridge=helix_shell_bridge,
        force=force,
        shortcut=shortcut,
    )
    return apply_integration(plan)


def integration_status(path: Path, launcher: list[str], *, state_root: Path, shortcut: str) -> str:
    """Classify recorded integration for lifecycle security gates."""
    parts = _shortcut_path(shortcut)
    target = path.expanduser().resolve()
    _original, document = _read_document(target)
    values: list[object] = []
    try:
        keys = document.get("keys")
        for mode in _MODES:
            table = keys.get(mode) if hasattr(keys, "get") else None
            for part in parts[:-1]:
                table = table.get(part) if hasattr(table, "get") else None
            values.append(table.get(parts[-1]) if hasattr(table, "get") else None)
    except (AttributeError, TypeError, KeyError):
        return "other"
    launcher_argv = _clean_argv(launcher, label="launcher")
    if any(_legacy_binding(value, launcher_argv) for value in values):
        return "legacy"
    bridge = bridge_path(state_root)
    try:
        existing = _owned_bridge(bridge)
        absent, shell = _configured_shell(document)
    except EnhanceError:
        return "modified"
    if (
        existing
        and not absent
        and shell == (str(bridge),)
        and all(
            value is None or _secure_binding(value, existing[1].fingerprint) for value in values
        )
    ):
        return "secure"
    return "other"


def remove_owned_binding(
    path: Path,
    launcher: list[str],
    *,
    shortcut: str = "space.x",
    state_root: Path | None = None,
) -> Path | None:
    """Remove exact owned mappings and safely restore/remove an exact owned bridge."""
    parts = _shortcut_path(shortcut)
    target = path.expanduser().resolve()
    original, document = _read_document(target)
    launcher_argv = _clean_argv(launcher, label="launcher")
    existing: tuple[str, BridgeMetadata] | None = None
    bridge: Path | None = None
    if state_root is not None:
        bridge = bridge_path(state_root.expanduser().absolute())
        try:
            existing = _owned_bridge(bridge)
        except EnhanceError:
            raise
    fingerprint = existing[1].fingerprint if existing else None
    try:
        keys = document.get("keys")
        if hasattr(keys, "get") and existing is None:
            for mode in _MODES:
                table = keys.get(mode)
                if not hasattr(table, "get"):
                    continue
                for part in parts[:-1]:
                    table = table.get(part)
                    if not hasattr(table, "get"):
                        break
                else:
                    if _recognizable_secure_binding(table.get(parts[-1])):
                        raise EnhanceError(
                            "A recognizable secure Helix bridge mapping remains, but its bridge "
                            "metadata is unavailable; cleanup is blocked."
                        )
        if hasattr(keys, "get"):
            for mode in _MODES:
                table = keys.get(mode)
                if not hasattr(table, "get"):
                    continue
                for part in parts[:-1]:
                    table = table.get(part)
                    if not hasattr(table, "get"):
                        break
                else:
                    value = table.get(parts[-1])
                    if _legacy_binding(value, launcher_argv) or (
                        fingerprint is not None
                        and existing is not None
                        and _secure_binding(value, fingerprint)
                    ):
                        del table[parts[-1]]
        if bridge is not None:
            absent, shell = _configured_shell(document)
            if not absent and shell == (str(bridge),):
                if existing is None:
                    raise EnhanceError(
                        "Helix editor.shell still references a missing owned bridge; "
                        "cleanup is blocked."
                    )
                editor = document.get("editor")
                if existing[1].original_shell_absent:
                    del editor["shell"]
                else:
                    editor["shell"] = list(existing[1].original_shell)
    except (AttributeError, TypeError, KeyError, TOMLKitError) as exc:
        raise EnhanceError(f"Cannot remove Helix integration in {target}: {exc}") from exc

    backup = _write_config(target, original, tomlkit.dumps(document))
    if bridge is not None and existing is not None:
        # Recheck exact ownership immediately before deletion. Later user edits are preserved.
        current = _owned_bridge(bridge)
        if current is None or current[0] != existing[0]:
            raise EnhanceError("Helix bridge changed during cleanup; it was not deleted.")
        bridge.unlink()
    return backup
