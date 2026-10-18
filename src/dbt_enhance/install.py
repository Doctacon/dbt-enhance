"""Turn an ephemeral uvx invocation into a persistent, reproducible shortcut."""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib.metadata import distribution
from pathlib import Path

from dbt_enhance.helix import install_config, prepare_config
from dbt_enhance.models import EnhanceError


def installation_source() -> str:
    """Retain local/Git provenance; registry releases are pinned to this version."""
    package = distribution("dbt-enhance")
    direct = package.read_text("direct_url.json")
    if direct:
        data = json.loads(direct)
        url = data["url"]
        vcs = data.get("vcs_info")
        if vcs:
            if vcs["vcs"] != "git":
                raise EnhanceError("Please supply --source for this non-Git installation.")
            url = f"git+{url}@{vcs['commit_id']}"
        if data.get("subdirectory"):
            url += f"#subdirectory={data['subdirectory']}"
        return url
    return f"dbt-enhance=={package.version}"


def setup(
    config: Path, *, source: str | None = None, force: bool = False
) -> tuple[Path, Path | None]:
    """Install a stable uv tool, then merge only space-x into the Helix config."""
    uv = shutil.which("uv")
    if not uv:
        raise EnhanceError("uv is required for setup. Install it from https://docs.astral.sh/uv/.")
    result = subprocess.run(
        [uv, "tool", "dir", "--bin"], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise EnhanceError(f"Could not locate uv's tool directory: {result.stderr.strip()}")
    launcher = Path(result.stdout.strip()).expanduser().resolve() / "dbt-enhance"
    # Do not install anything if merging would overwrite an unrelated shortcut.
    prepare_config(config.expanduser().resolve(), [str(launcher)], force=force)
    result = subprocess.run(
        [uv, "tool", "install", "--force", source or installation_source()],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise EnhanceError(f"uv tool installation failed:\n{result.stderr.strip()}")
    if not launcher.is_file():
        raise EnhanceError(f"uv did not install the expected command at {launcher}.")
    backup = install_config(config, [str(launcher)], force=force)
    return launcher, backup
