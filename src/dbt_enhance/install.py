"""Turn an ephemeral uvx invocation into a persistent, reproducible shortcut."""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib.metadata import distribution
from pathlib import Path
from typing import TYPE_CHECKING

from dbt_enhance.helix import apply_integration, prepare_integration
from dbt_enhance.models import EnhanceError

if TYPE_CHECKING:
    from dbt_enhance.lifecycle import CommandRunner


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


def _run(runner: CommandRunner | None, command: list[str]) -> subprocess.CompletedProcess[str]:
    command_runner = subprocess.run if runner is None else runner
    try:
        return command_runner(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise EnhanceError(f"Could not run {command[0]}: {exc}") from exc


def setup(
    config: Path,
    *,
    source: str | None = None,
    force: bool = False,
    shortcut: str = "space.x",
    helix_shell_bridge: bool = False,
    runner: CommandRunner | None = None,
    state_root: Path | None = None,
) -> tuple[Path, Path | None]:
    """Install a stable uv tool and the explicitly consented fail-closed Helix bridge."""
    uv = shutil.which("uv")
    if not uv:
        raise EnhanceError("uv is required for setup. Install it from https://docs.astral.sh/uv/.")
    result = _run(runner, [uv, "tool", "dir", "--bin"])
    if result.returncode:
        raise EnhanceError(f"Could not locate uv's tool directory: {result.stderr.strip()}")
    launcher = Path(result.stdout.strip()).expanduser().resolve() / "dbt-enhance"
    config_target = config.expanduser().resolve()
    if state_root is None:
        raise EnhanceError(
            "Secure Helix setup requires private lifecycle state; no changes were made."
        )
    # Do not install anything if bridge consent or config ownership validation fails.
    integration = prepare_integration(
        config_target,
        [str(launcher)],
        state_root=state_root,
        helix_shell_bridge=helix_shell_bridge,
        force=force,
        shortcut=shortcut,
    )

    selected_source = source or installation_source()
    provenance: tuple[str, str, str] | None = None
    if state_root is not None:
        from dbt_enhance.lifecycle import resolve_setup_source

        selected_source, kind, identity = resolve_setup_source(
            selected_source, runner=subprocess.run if runner is None else runner
        )
        provenance = (kind, identity, selected_source)

    from dbt_enhance.lifecycle import validate_setup_candidate
    from dbt_enhance.settings import settings_path

    validated_version = validate_setup_candidate(
        selected_source,
        settings_config=settings_path(),
        runner=subprocess.run if runner is None else runner,
    )
    result = _run(runner, [uv, "tool", "install", "--force", selected_source])
    if result.returncode:
        raise EnhanceError(f"uv tool installation failed:\n{result.stderr.strip()}")
    if not launcher.is_file():
        raise EnhanceError(f"uv did not install the expected command at {launcher}.")
    version_result = _run(runner, [str(launcher), "--version"])
    version = version_result.stdout.strip()
    if version_result.returncode or version != validated_version:
        raise EnhanceError(
            "The installed tool does not match the validated candidate. "
            "Helix config was not changed; inspect this partial installation before retrying."
        )
    backup = apply_integration(integration)

    if provenance is not None:
        from dbt_enhance.lifecycle import (
            InstallationState,
            InstallProvenance,
            record_install,
        )

        kind, identity, pinned_source = provenance
        record_install(
            InstallationState(
                current=InstallProvenance(
                    source=pinned_source,
                    kind=kind,  # type: ignore[arg-type]
                    identity=identity,
                    version=version,
                    launcher=str(launcher),
                    helix_config=str(config_target),
                    shortcut=shortcut,
                    helix_launcher=(str(launcher),),
                )
            ),
            state_root=state_root,
        )
    return launcher, backup
