"""Install a non-destructive Helix mapping, preserving existing TOML and symlinks."""

from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
import stat
import tempfile
from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError

from dbt_enhance.models import EnhanceError


def config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "helix/config.toml"


def binding(launcher: list[str]) -> str:
    """SQL goes over stdin; the filename is data, never interpolated shell code.

    Helix does not shell-escape expansions. A quoted heredoc handles spaces,
    apostrophes, dollar signs and backticks. A fresh 128-bit delimiter avoids
    collisions; newline-containing filenames are not supported.
    """
    delimiter = "DBT_ENHANCE_FILE_" + secrets.token_hex(16)
    return (
        f":pipe-to IFS= read -r dbt_enhance_file <<'{delimiter}'\n"
        f"%{{buffer_name}}\n{delimiter}\n"
        f'{shlex.join(launcher)} popup --file "$dbt_enhance_file"'
    )


def _same_binding(value: object, launcher: list[str]) -> bool:
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


def prepare_config(path: Path, launcher: list[str], *, force: bool = False) -> tuple[str, str]:
    """Validate both mappings before making any change. Return original/new TOML."""
    try:
        original = path.read_text() if path.exists() else ""
        document = tomlkit.parse(original)
    except (OSError, TOMLKitError) as exc:
        raise EnhanceError(f"Cannot read Helix config {path}: {exc}") from exc
    command = binding(launcher)
    try:
        keys = document.setdefault("keys", tomlkit.table())
        for mode in ("normal", "select"):
            table = keys.setdefault(mode, tomlkit.table())
            space = table.setdefault("space", tomlkit.table())
            if not hasattr(space, "get"):
                raise EnhanceError(f"keys.{mode}.space is already a command, not a key map.")
            old = space.get("x")
            if _same_binding(old, launcher):
                continue
            if old is not None and not force:
                raise EnhanceError(
                    f"keys.{mode}.space.x already has a binding. "
                    "Use setup --force to replace it (a backup will be saved)."
                )
            space["x"] = command
    except (AttributeError, TypeError, KeyError, TOMLKitError) as exc:
        raise EnhanceError(f"Cannot merge Helix key maps in {path}: {exc}") from exc
    return original, tomlkit.dumps(document)


def install_config(path: Path, launcher: list[str], *, force: bool = False) -> Path | None:
    """Write atomically through symlinks; keep a unique backup on each change."""
    path = path.expanduser().resolve()
    original, updated = prepare_config(path, launcher, force=force)
    if original == updated:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.dbt-enhance-{secrets.token_hex(4)}.bak")
        shutil.copy2(path, backup)
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
