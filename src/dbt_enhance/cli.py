"""Public commands. Import the terminal UI only when a screen is requested."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict
from enum import StrEnum
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Any

import typer

from dbt_enhance import helix
from dbt_enhance.models import EnhanceError, QueryRequest

app = typer.Typer(
    no_args_is_help=True,
    invoke_without_command=True,
    add_completion=False,
    help="Run selected dbt SQL from Helix in a scrollable terminal popup.",
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(no_args_is_help=True, help="Validate or explain user-local settings.")
diagnostics_app = typer.Typer(no_args_is_help=True, help="Report or purge private diagnostics.")
worker_app = typer.Typer(no_args_is_help=True, help="Inspect and control native workers.")
app.add_typer(config_app, name="config")
app.add_typer(diagnostics_app, name="diagnostics")
app.add_typer(worker_app, name="worker")

FileOption = Annotated[
    Path, typer.Option("--file", help="Source buffer path; locates the nearest dbt project.")
]
LimitOption = Annotated[
    int | None, typer.Option("--limit", min=1, max=10000, help="Maximum preview rows.")
]
TargetOption = Annotated[
    str | None,
    typer.Option("--target", help="Override dbt's default target."),
]
ProfilesOption = Annotated[Path | None, typer.Option("--profiles-dir")]
DbtOption = Annotated[
    Path | None,
    typer.Option("--dbt", help="Explicit path to a dbt Core executable."),
]
TimeoutOption = Annotated[
    float | None,
    typer.Option(
        "--timeout",
        min=0.1,
        help="Query timeout in seconds; the popup reports expiry independently of cleanup.",
    ),
]


class Backend(StrEnum):
    process = "process"
    worker = "worker"
    auto = "auto"


class DoctorMode(StrEnum):
    popup = "popup"
    query = "query"


BackendOption = Annotated[
    Backend | None,
    typer.Option(
        "--backend",
        help="worker: legacy default; process: isolated; auto: certified-default policy (opt-in).",
    ),
]


def _request(
    file: Path,
    sql: str | None,
    limit: int | None,
    target: str | None,
    profiles_dir: Path | None,
    dbt: Path | None,
    timeout: float | None,
    backend: str | None = None,
    *,
    worker_idle_seconds: float | None = None,
    diagnostics: bool | None = None,
    theme: str | None = None,
) -> QueryRequest:
    if str(file) == "[scratch]":
        raise EnhanceError(
            "Save this buffer inside a dbt project first (scratch buffers have no project)."
        )
    if any(char in str(file) for char in "\n\r\0"):
        raise EnhanceError("Filenames containing newlines or NUL are not supported.")
    if timeout is not None and not math.isfinite(timeout):
        raise EnhanceError("The timeout must be a finite number.")
    if sql is None:
        if sys.stdin.isatty():
            raise EnhanceError("Pipe a SQL selection on stdin, or pass --sql 'select ...'.")
        sql = sys.stdin.read(1_048_577)
    if len(sql.encode("utf-8")) > 1_048_576:
        raise EnhanceError("The selected SQL exceeds 1 MiB. Select a smaller query.")
    if not sql.strip():
        raise EnhanceError("The selection is empty. Select a complete query and press Space, x.")
    from dbt_enhance.dbt import discover_project
    from dbt_enhance.settings import resolve_settings

    source = file.expanduser().absolute()
    try:
        project = discover_project(source)
    except EnhanceError:
        # Keep editor transport usable before backend discovery. Execution still
        # rejects missing projects; this does not authorize an unknown project.
        project = None
    effective = resolve_settings(
        project,
        cli={
            "limit": limit,
            "target": target,
            "profiles_dir": profiles_dir,
            "dbt": dbt,
            "timeout": timeout,
            "backend": backend,
            "worker_idle_seconds": worker_idle_seconds,
            "diagnostics": diagnostics,
            "theme": theme,
        },
    )
    origin = effective.sources["backend"]
    return QueryRequest(
        sql=sql,
        file=source,
        **effective.values,
        allowed_targets=effective.allowed_targets,
        backend_origin="legacy_builtin" if origin == "builtin" else origin,
    )


def _fail(exc: Exception) -> None:
    # Control characters from a SQL error or path should never become terminal commands.
    message = "".join(
        c if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) <= 159) else "?" for c in str(exc)
    )
    typer.echo(f"dbt-enhance: {message}", err=True)
    raise typer.Exit(1)


def _project_root(file: Path) -> Path:
    """Find a project for metadata commands without importing or executing dbt."""
    source = file.expanduser().absolute()
    candidate = source if source.is_dir() else source.parent
    for parent in (candidate, *candidate.parents):
        if (parent / "dbt_project.yml").is_file():
            return parent.resolve(strict=False)
    raise EnhanceError(f"No dbt_project.yml was found above {source}.")


def _json_value(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _resolved_settings(
    file: Path | None,
    *,
    limit: int | None = None,
    target: str | None = None,
    profiles_dir: Path | None = None,
    dbt: Path | None = None,
    timeout: float | None = None,
    backend: str | None = None,
    worker_idle_seconds: float | None = None,
    diagnostics: bool | None = None,
    theme: str | None = None,
):
    from dbt_enhance.settings import resolve_settings

    project = _project_root(file) if file is not None else None
    effective = resolve_settings(
        project,
        cli={
            "limit": limit,
            "target": target,
            "profiles_dir": profiles_dir,
            "dbt": dbt,
            "timeout": timeout,
            "backend": backend,
            "worker_idle_seconds": worker_idle_seconds,
            "diagnostics": diagnostics,
            "theme": theme,
        },
    )
    return project, effective


def _print_plan(operation: str, effects: tuple[str, ...]) -> None:
    typer.echo(f"{operation.capitalize()} preview (no changes applied):")
    for effect in effects:
        typer.echo(f"- {effect}")


def _print_lifecycle_result(result: Any) -> None:
    for effect in result.completed:
        typer.echo(f"DONE  {effect}")
    for error in result.errors:
        typer.echo(f"ERROR {error}", err=True)
    if result.errors:
        raise typer.Exit(1)


@app.callback()
def root(
    show_version: Annotated[
        bool, typer.Option("--version", is_eager=True, help="Show package version.")
    ] = False,
) -> None:
    if show_version:
        typer.echo(version("dbt-enhance"))
        raise typer.Exit()


@app.command()
def popup(
    file: FileOption,
    limit: LimitOption = None,
    target: TargetOption = None,
    profiles_dir: ProfilesOption = None,
    dbt: DbtOption = None,
    timeout: TimeoutOption = None,
    backend: BackendOption = None,
    worker_idle_seconds: Annotated[float | None, typer.Option(min=0.1, max=86400)] = None,
    diagnostics: Annotated[bool | None, typer.Option("--diagnostics/--no-diagnostics")] = None,
    theme: Annotated[str | None, typer.Option(help="auto, dark, or light")] = None,
) -> None:
    """Receive a selection on stdin and open a tmux popup (used by Helix)."""
    from dbt_enhance.popup import launch_popup

    try:
        launch_popup(
            _request(
                file,
                None,
                limit,
                target,
                profiles_dir,
                dbt,
                timeout,
                backend.value if backend else None,
                worker_idle_seconds=worker_idle_seconds,
                diagnostics=diagnostics,
                theme=theme,
            )
        )
    except (EnhanceError, OSError) as exc:
        _fail(exc)


@app.command()
def query(
    file: FileOption,
    sql: Annotated[str | None, typer.Option(help="SQL text; otherwise read stdin.")] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print structured results instead of opening the UI.")
    ] = False,
    limit: LimitOption = None,
    target: TargetOption = None,
    profiles_dir: ProfilesOption = None,
    dbt: DbtOption = None,
    timeout: TimeoutOption = None,
    backend: BackendOption = None,
    worker_idle_seconds: Annotated[float | None, typer.Option(min=0.1, max=86400)] = None,
    diagnostics: Annotated[bool | None, typer.Option("--diagnostics/--no-diagnostics")] = None,
    theme: Annotated[str | None, typer.Option(help="auto, dark, or light")] = None,
) -> None:
    """Run a selection in this terminal; no tmux required. Never saves the buffer."""
    try:
        request = _request(
            file,
            sql,
            limit,
            target,
            profiles_dir,
            dbt,
            timeout,
            backend.value if backend else None,
            worker_idle_seconds=worker_idle_seconds,
            diagnostics=diagnostics,
            theme=theme,
        )
        if json_output:
            from dbt_enhance.dbt import run_query

            typer.echo(json.dumps(asdict(run_query(request)), default=str, ensure_ascii=False))
        else:
            from dbt_enhance.ui import QueryApp

            QueryApp(request=request).run()
    except (EnhanceError, OSError) as exc:
        _fail(exc)


@app.command()
def demo() -> None:
    """Explore the results interface with sample data; no dbt or database needed."""
    from dbt_enhance.ui import QueryApp, demo_result

    QueryApp(result=demo_result()).run()


@app.command(name="_helix", hidden=True)
def helix_transport(raw_command: str) -> None:
    """Receive one opaque command argument from the owned Helix shell bridge."""
    try:
        filename = helix.parse_transport_command(raw_command)
        from dbt_enhance.popup import launch_popup

        launch_popup(_request(Path(filename), None, None, None, None, None, None))
    except (EnhanceError, OSError) as exc:
        _fail(exc)


@app.command(name="_view", hidden=True)
def view_request(path: Path, consume: bool = False) -> None:
    from dbt_enhance.popup import read_request
    from dbt_enhance.ui import QueryApp

    try:
        QueryApp(request=read_request(path, consume=consume)).run()
    except (EnhanceError, OSError) as exc:
        _fail(exc)


@app.command()
def setup(
    config: Annotated[
        Path | None,
        typer.Option(help="Helix config path (defaults to XDG_CONFIG_HOME/helix/config.toml)."),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option(help="uv install source; inferred from this uvx invocation by default."),
    ] = None,
    force: Annotated[
        bool, typer.Option(help="Replace an existing shortcut, after backing it up.")
    ] = False,
    helix_shell_bridge: Annotated[
        bool,
        typer.Option(
            "--helix-shell-bridge",
            help="Explicitly set editor.shell to the owned fail-closed bridge.",
        ),
    ] = False,
    shortcut: Annotated[str, typer.Option(help="Helix normal/select mode key path.")] = "space.x",
    live_smoke: Annotated[
        bool, typer.Option("--live-smoke", help="Explicitly run SELECT 1 after setup.")
    ] = False,
    file: Annotated[Path | None, typer.Option(help="dbt project buffer for --live-smoke.")] = None,
    target: TargetOption = None,
    profiles_dir: ProfilesOption = None,
    dbt: DbtOption = None,
    timeout: TimeoutOption = None,
    backend: BackendOption = None,
) -> None:
    """Install the stable tool and paired Helix mappings; never query by default."""
    from dbt_enhance.install import setup as install
    from dbt_enhance.lifecycle import default_state_root
    from dbt_enhance.popup import tmux_binary
    from dbt_enhance.settings import load_user_settings

    if live_smoke and file is None:
        _fail(EnhanceError("--live-smoke requires --file."))
    try:
        # Validate preferences and every prerequisite before package or config changes.
        load_user_settings()
        missing: list[str] = []
        if not shutil.which("uv"):
            missing.append("uv")
        helix_binary = shutil.which("hx")
        if not helix_binary:
            missing.append("hx (Helix 25.07+)")
        try:
            tmux_binary(require_session=False)
        except EnhanceError:
            missing.append("tmux 3.3+")
        if missing:
            raise EnhanceError(
                "Missing setup prerequisites (not installed automatically): " + ", ".join(missing)
            )
        assert helix_binary is not None
        result = subprocess.run(
            [helix_binary, "--version"], capture_output=True, text=True, check=False
        )
        match = re.search(r"(?<!\d)(\d{2,4})\.(\d{1,2})(?:\.\d+)?", result.stdout)
        if result.returncode or not match or tuple(map(int, match.groups())) < (25, 7):
            detail = result.stderr.strip() or result.stdout.strip() or "version unavailable"
            raise EnhanceError(f"Helix 25.07 or newer is required (found: {detail}).")
        path = config or helix.config_path()
        launcher, backup = install(
            path,
            source=source,
            force=force,
            helix_shell_bridge=helix_shell_bridge,
            shortcut=shortcut,
            state_root=default_state_root(),
        )
        if live_smoke:
            assert file is not None
            from dbt_enhance.dbt import run_query

            run_query(
                _request(
                    file,
                    "SELECT 1",
                    None,
                    target,
                    profiles_dir,
                    dbt,
                    timeout,
                    backend.value if backend else None,
                )
            )
    except (EnhanceError, OSError) as exc:
        _fail(exc)
    typer.echo(f"Installed package and Helix mappings: {launcher}\nHelix config: {path}")
    if backup:
        typer.echo(f"Backup: {backup}")
    if live_smoke:
        typer.echo("READY Live SELECT 1 smoke check passed.")
    else:
        typer.echo("Installed; readiness is not asserted because no database query was run.")
    typer.echo(
        "The owned bridge changes editor.shell but delegates every non-dbt-enhance command to "
        "your prior POSIX shell.\n"
        "Next: reload Helix with :config-reload (or restart it). If you change editor.shell, "
        "run setup again with --helix-shell-bridge.\n"
        "Run `dbt-enhance doctor` to inspect readiness or `dbt-enhance demo` without dbt.\n"
        "For popup use, start tmux, select SQL, then press Space followed by x."
    )


@app.command()
def doctor(
    file: Annotated[
        Path | None, typer.Option(help="Check project and dbt resolution for this buffer.")
    ] = None,
    mode: Annotated[
        DoctorMode, typer.Option(help="popup checks Helix/tmux; query is standalone.")
    ] = DoctorMode.popup,
    live_smoke: Annotated[
        bool, typer.Option("--live-smoke", help="Explicitly execute literal SELECT 1.")
    ] = False,
    target: TargetOption = None,
    profiles_dir: ProfilesOption = None,
    dbt: DbtOption = None,
    timeout: TimeoutOption = None,
    backend: BackendOption = None,
) -> None:
    """Inspect readiness without reading profiles, authenticating, or querying by default."""
    from dbt_enhance.dbt import discover_project, find_dbt
    from dbt_enhance.settings import resolve_settings
    from dbt_enhance.support import classify_backend

    if live_smoke and file is None:
        _fail(EnhanceError("--live-smoke requires --file."))

    checks: list[tuple[str, bool, str]] = []
    if mode == DoctorMode.popup:
        from dbt_enhance.popup import tmux_binary

        helix_binary = shutil.which("hx")
        checks.append(
            (
                "Helix",
                bool(helix_binary),
                helix_binary or "hx not on PATH (requires Helix 25.07+)",
            )
        )
        try:
            executable = tmux_binary(require_session=False)
            checks.append(("tmux 3.3+", True, executable))
        except EnhanceError as exc:
            checks.append(("tmux 3.3+", False, str(exc)))
        checks.append(
            (
                "popup session",
                bool(os.environ.get("TMUX")),
                "active" if os.environ.get("TMUX") else "start tmux, then launch Helix/Yazi",
            )
        )

    project: Path | None = None
    try:
        if file:
            project = discover_project(file.expanduser().absolute())
            checks.append(("project", True, str(project)))
        effective = resolve_settings(
            project,
            cli={
                "target": target,
                "profiles_dir": profiles_dir,
                "dbt": dbt,
                "timeout": timeout,
                "backend": backend.value if backend else None,
            },
        )
        selected = str(effective.values["backend"])
        tier = classify_backend(None, selected if selected in {"worker", "process"} else "worker")
        checks.append(("settings", True, "valid"))
        checks.append(
            (
                "backend/support",
                True,
                f"{selected} ({tier}; no live environment certification asserted)",
            )
        )
        if project is not None:
            executable = find_dbt(project, effective.values["dbt"])
            checks.append(("dbt", True, str(executable)))
            from dbt_enhance.worker import project_python, retirement_status, status

            if selected == "worker":
                checks.append(("worker interpreter", True, str(project_python(executable))))
            running = status(project)
            detail = "not running"
            if running:
                detail = f"running (supervisor {running['pid']}, session {running['session_pid']})"
            checks.append(("project worker", True, detail))
            retirement = retirement_status(project)
            if retirement:
                checks.append(
                    (
                        "last worker retirement",
                        True,
                        f"{retirement['reason']} (session {retirement['session_pid']})",
                    )
                )
            if bool(effective.values["diagnostics"]):
                from dbt_enhance.diagnostics import DiagnosticStore

                latest = DiagnosticStore().latest(project)
                checks.append(("last diagnostic metadata", True, json.dumps(latest or None)))
    except (EnhanceError, OSError) as exc:
        checks.append(("project/dbt/settings", False, str(exc)))

    if live_smoke and all(okay for _, okay, _ in checks):
        assert file is not None
        try:
            from dbt_enhance.dbt import run_query

            run_query(
                _request(
                    file,
                    "SELECT 1",
                    None,
                    target,
                    profiles_dir,
                    dbt,
                    timeout,
                    backend.value if backend else None,
                )
            )
            checks.append(("live SELECT 1", True, "completed"))
        except (EnhanceError, OSError) as exc:
            checks.append(("live SELECT 1", False, str(exc)))

    for name, okay, detail in checks:
        typer.echo(f"{'OK  ' if okay else 'FAIL'} {name}: {detail}")
    if not all(okay for _, okay, _ in checks):
        raise typer.Exit(1)


@app.command(name="stop")
def stop_worker(file: FileOption) -> None:
    """Stop this project's warm worker, including an active query, without starting dbt."""
    from dbt_enhance.dbt import discover_project
    from dbt_enhance.worker import stop

    try:
        stopped = stop(discover_project(file.expanduser().absolute()))
    except (EnhanceError, OSError) as exc:
        _fail(exc)
    typer.echo("Project worker stopped." if stopped else "No project worker is running.")


@config_app.command("explain")
def config_explain(
    file: FileOption,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    limit: LimitOption = None,
    target: TargetOption = None,
    profiles_dir: ProfilesOption = None,
    dbt: DbtOption = None,
    timeout: TimeoutOption = None,
    backend: BackendOption = None,
    worker_idle_seconds: Annotated[float | None, typer.Option(min=0.1, max=86400)] = None,
    diagnostics: Annotated[bool | None, typer.Option("--diagnostics/--no-diagnostics")] = None,
    theme: Annotated[str | None, typer.Option(help="auto, dark, or light")] = None,
) -> None:
    """Explain effective settings and provenance without importing or running dbt."""
    try:
        _project, effective = _resolved_settings(
            file,
            limit=limit,
            target=target,
            profiles_dir=profiles_dir,
            dbt=dbt,
            timeout=timeout,
            backend=backend.value if backend else None,
            worker_idle_seconds=worker_idle_seconds,
            diagnostics=diagnostics,
            theme=theme,
        )
    except (EnhanceError, OSError) as exc:
        _fail(exc)
    payload = {
        "values": _json_value(effective.values),
        "sources": effective.sources,
        "allowed_targets": _json_value(effective.allowed_targets),
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    for name, value in payload["values"].items():
        typer.echo(f"{name}: {value!s} ({effective.sources[name]})")
    typer.echo(f"allowed_targets: {payload['allowed_targets']!s} (safety policy)")


@config_app.command("validate")
def config_validate(
    config: Annotated[Path | None, typer.Option(help="User-local TOML file to validate.")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate settings without importing project dbt or connecting anywhere."""
    from dbt_enhance.settings import load_user_settings, settings_path

    try:
        path = config.expanduser().absolute() if config else settings_path()
        settings = load_user_settings(path)
    except (EnhanceError, OSError) as exc:
        _fail(exc)
    payload = {
        "valid": True,
        "schema": settings.schema,
        "version": version("dbt-enhance"),
        "helix_bridge_format": helix.HELIX_BRIDGE_FORMAT,
    }
    if json_output:
        typer.echo(json.dumps(payload))
    else:
        typer.echo(f"Valid user settings (schema {settings.schema}): {path}")


@config_app.command("path")
def config_location() -> None:
    """Print the user-local settings path; do not create it."""
    from dbt_enhance.settings import settings_path

    typer.echo(str(settings_path()))


@diagnostics_app.command("report")
def diagnostics_report(
    file: FileOption,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Build a shareable metadata-only report for one project."""
    from dbt_enhance.diagnostics import DiagnosticStore, build_shareable_report

    try:
        project, effective = _resolved_settings(file)
        assert project is not None
        if not bool(effective.values["diagnostics"]):
            raise EnhanceError("Diagnostics are disabled by effective settings.")
        store = DiagnosticStore(enabled=True)
        report = build_shareable_report(store.latest(project), store.failures(project))
    except (EnhanceError, OSError) as exc:
        _fail(exc)
    if json_output:
        typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        typer.echo("Shareable diagnostics (metadata only; review before sharing):")
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


@diagnostics_app.command("purge")
def diagnostics_purge(
    file: Annotated[
        Path | None, typer.Option("--file", help="Purge one project's diagnostics.")
    ] = None,
    all_projects: Annotated[
        bool, typer.Option("--all", help="Purge all dbt-enhance diagnostics.")
    ] = False,
    apply: Annotated[bool, typer.Option("--apply", help="Confirm and perform the purge.")] = False,
) -> None:
    """Preview or explicitly purge diagnostics only."""
    from dbt_enhance.diagnostics import DiagnosticStore

    if (file is None) == (not all_projects):
        _fail(EnhanceError("Choose exactly one of --file or --all."))
    try:
        project = _project_root(file) if file is not None else None
        scope = "all dbt-enhance diagnostics" if project is None else "this project's diagnostics"
        typer.echo(f"Purge preview: {scope}; preferences and project data are untouched.")
        if not apply:
            typer.echo("Dry run only. Re-run with --apply to confirm deletion.")
            return
        DiagnosticStore().purge(project)
    except (EnhanceError, OSError) as exc:
        _fail(exc)
    typer.echo("Diagnostics purged.")


@worker_app.command("list")
def worker_list() -> None:
    """List authenticated native workers without starting one."""
    from dbt_enhance.worker import inventory

    try:
        workers = inventory()
    except (EnhanceError, OSError) as exc:
        _fail(exc)
    if not workers:
        typer.echo("No workers are running.")
        return
    for item in workers:
        identity = (
            f"supervisor {item.get('pid', 'unknown')}, session {item.get('session_pid', 'unknown')}"
        )
        typer.echo(
            f"{item['project']}: {item.get('status', item.get('state', 'unknown'))} ({identity})"
        )


@worker_app.command("status")
def worker_status(file: FileOption) -> None:
    """Inspect one project's authenticated worker."""
    from dbt_enhance.worker import inspect_worker

    try:
        information = inspect_worker(_project_root(file))
    except (EnhanceError, OSError) as exc:
        _fail(exc)
    if information is None:
        typer.echo("No project worker is running.")
        return
    typer.echo(json.dumps(_json_value(information), ensure_ascii=False, sort_keys=True))


@worker_app.command("disconnect")
def worker_disconnect(file: FileOption) -> None:
    """Disconnect one idle worker; active work is never interrupted."""
    from dbt_enhance.worker import disconnect

    try:
        stopped = disconnect(_project_root(file))
    except (EnhanceError, OSError) as exc:
        _fail(exc)
    if not stopped:
        _fail(EnhanceError("No idle project worker was disconnected (it may be active)."))
    typer.echo("Project worker disconnected.")


@worker_app.command("reset")
def worker_reset(
    file: FileOption,
    allow_active: Annotated[
        bool, typer.Option("--allow-active", help="Confirm interruption of active work.")
    ] = False,
) -> None:
    """Reset one worker, requiring explicit confirmation for active work."""
    from dbt_enhance.worker import inspect_worker, reset

    try:
        project = _project_root(file)
        information = inspect_worker(project)
        active = bool(information and information.get("active"))
        if active and not allow_active:
            raise EnhanceError("Worker is active; re-run with --allow-active to interrupt it.")
        if active:
            typer.echo("WARNING: interrupting an active worker query.", err=True)
        stopped = reset(project, allow_active=allow_active)
    except (EnhanceError, OSError) as exc:
        _fail(exc)
    if not stopped:
        _fail(EnhanceError("No project worker was reset."))
    typer.echo("Project worker reset.")


@app.command("provenance")
def provenance() -> None:
    """Show recorded installation provenance without checking for updates."""
    from dbt_enhance.lifecycle import load_state

    try:
        state = load_state()
    except (EnhanceError, OSError) as exc:
        _fail(exc)
    if state is None:
        _fail(EnhanceError("No lifecycle provenance is recorded."))
    typer.echo(json.dumps(_json_value(asdict(state)), ensure_ascii=False, sort_keys=True))


@app.command("update")
def update_installation(
    source: Annotated[str, typer.Option("--source", help="Explicit Git ref or local artifact.")],
    config: Annotated[Path | None, typer.Option("--config", help="User settings TOML.")] = None,
    apply: Annotated[bool, typer.Option("--apply", help="Apply the previewed update.")] = False,
) -> None:
    """Validate and preview an immutable update; apply only when requested."""
    from dbt_enhance.lifecycle import apply_update, preview_update
    from dbt_enhance.settings import settings_path

    try:
        plan = preview_update(source, settings_config=config or settings_path())
        _print_plan("update", plan.effects)
        if not apply:
            typer.echo("Dry run only. Re-run with --apply to make these changes.")
            return
        result = apply_update(plan)
    except (EnhanceError, OSError) as exc:
        _fail(exc)
    _print_lifecycle_result(result)


@app.command("rollback")
def rollback_installation(
    config: Annotated[Path | None, typer.Option("--config", help="User settings TOML.")] = None,
    apply: Annotated[bool, typer.Option("--apply", help="Apply the previewed rollback.")] = False,
) -> None:
    """Validate and preview a version-only rollback; apply only when requested."""
    from dbt_enhance.lifecycle import apply_rollback, preview_rollback
    from dbt_enhance.settings import settings_path

    try:
        plan = preview_rollback(settings_config=config or settings_path())
        _print_plan("rollback", plan.effects)
        if not apply:
            typer.echo("Dry run only. Re-run with --apply to make these changes.")
            return
        result = apply_rollback(plan)
    except (EnhanceError, OSError) as exc:
        _fail(exc)
    _print_lifecycle_result(result)


@app.command("remove")
def remove_installation(
    apply: Annotated[bool, typer.Option("--apply", help="Apply the previewed removal.")] = False,
    purge: Annotated[
        bool, typer.Option("--purge", help="Also remove preferences and diagnostics.")
    ] = False,
    allow_active: Annotated[
        bool, typer.Option("--allow-active", help="Confirm interruption of active workers.")
    ] = False,
) -> None:
    """Preview owned removal effects; preferences are retained unless --purge is explicit."""
    from dbt_enhance.lifecycle import apply_remove, preview_remove

    try:
        plan = preview_remove(purge=purge)
        _print_plan("remove", plan.effects)
        if not apply:
            typer.echo("Dry run only. Re-run with --apply to make these changes.")
            return
        if any(bool(item.get("active", True)) for item in plan.workers) and allow_active:
            typer.echo("WARNING: removal will interrupt active worker queries.", err=True)
        result = apply_remove(plan, allow_active=allow_active)
    except (EnhanceError, OSError) as exc:
        _fail(exc)
    _print_lifecycle_result(result)


def main() -> None:
    app()
