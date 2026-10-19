"""Public commands. Import the terminal UI only when a screen is requested."""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
from dataclasses import asdict
from enum import StrEnum
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

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
FileOption = Annotated[
    Path, typer.Option("--file", help="Source buffer path; locates the nearest dbt project.")
]
LimitOption = Annotated[
    int,
    typer.Option(
        "--limit", min=1, max=10000, envvar="DBT_ENHANCE_LIMIT", help="Maximum preview rows."
    ),
]
TargetOption = Annotated[
    str | None,
    typer.Option("--target", envvar="DBT_ENHANCE_TARGET", help="Override dbt's default target."),
]
ProfilesOption = Annotated[
    Path | None, typer.Option("--profiles-dir", envvar="DBT_ENHANCE_PROFILES_DIR")
]
DbtOption = Annotated[
    Path | None,
    typer.Option("--dbt", envvar="DBT_ENHANCE_DBT", help="Explicit path to a dbt Core executable."),
]
TimeoutOption = Annotated[
    float,
    typer.Option(
        "--timeout",
        min=0.1,
        envvar="DBT_ENHANCE_TIMEOUT",
        help="Query timeout in seconds; the popup reports expiry independently of cleanup.",
    ),
]


class Backend(StrEnum):
    process = "process"
    worker = "worker"


BackendOption = Annotated[
    Backend,
    typer.Option(
        "--backend",
        envvar="DBT_ENHANCE_BACKEND",
        help="process: one dbt invocation per query; worker: opt-in persistent dbt/session.",
    ),
]


def _request(
    file: Path,
    sql: str | None,
    limit: int,
    target: str | None,
    profiles_dir: Path | None,
    dbt: Path | None,
    timeout: float,
    backend: str = "process",
) -> QueryRequest:
    if str(file) == "[scratch]":
        raise EnhanceError(
            "Save this buffer inside a dbt project first (scratch buffers have no project)."
        )
    if any(char in str(file) for char in "\n\r\0"):
        raise EnhanceError("Filenames containing newlines or NUL are not supported.")
    if not math.isfinite(timeout):
        raise EnhanceError("The timeout must be a finite number.")
    if sql is None:
        if sys.stdin.isatty():
            raise EnhanceError("Pipe a SQL selection on stdin, or pass --sql 'select ...'.")
        sql = sys.stdin.read(1_048_577)
    if len(sql.encode("utf-8")) > 1_048_576:
        raise EnhanceError("The selected SQL exceeds 1 MiB. Select a smaller query.")
    if not sql.strip():
        raise EnhanceError("The selection is empty. Select a complete query and press Space, x.")
    return QueryRequest(
        sql=sql,
        file=file.expanduser().absolute(),
        limit=limit,
        target=target,
        profiles_dir=profiles_dir.expanduser().absolute() if profiles_dir else None,
        dbt=dbt.expanduser().absolute() if dbt else None,
        timeout=timeout,
        backend=backend,
    )


def _fail(exc: Exception) -> None:
    # Control characters from a SQL error or path should never become terminal commands.
    message = "".join(
        c if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) <= 159) else "?" for c in str(exc)
    )
    typer.echo(f"dbt-enhance: {message}", err=True)
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
    limit: LimitOption = 500,
    target: TargetOption = None,
    profiles_dir: ProfilesOption = None,
    dbt: DbtOption = None,
    timeout: TimeoutOption = 120.0,
    backend: BackendOption = Backend.process,
) -> None:
    """Receive a selection on stdin and open a tmux popup (used by Helix)."""
    from dbt_enhance.popup import launch_popup

    try:
        launch_popup(_request(file, None, limit, target, profiles_dir, dbt, timeout, backend.value))
    except (EnhanceError, OSError) as exc:
        _fail(exc)


@app.command()
def query(
    file: FileOption,
    sql: Annotated[str | None, typer.Option(help="SQL text; otherwise read stdin.")] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print structured results instead of opening the UI.")
    ] = False,
    limit: LimitOption = 500,
    target: TargetOption = None,
    profiles_dir: ProfilesOption = None,
    dbt: DbtOption = None,
    timeout: TimeoutOption = 120.0,
    backend: BackendOption = Backend.process,
) -> None:
    """Run a selection in this terminal; no tmux required. Never saves the buffer."""
    try:
        request = _request(file, sql, limit, target, profiles_dir, dbt, timeout, backend.value)
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
        bool, typer.Option(help="Replace an existing Space-x shortcut, after backing it up.")
    ] = False,
) -> None:
    """Install a persistent uv tool and add Space-x in Helix normal/select modes."""
    from dbt_enhance.install import setup as install

    try:
        path = config or helix.config_path()
        launcher, backup = install(path, source=source, force=force)
    except (EnhanceError, OSError) as exc:
        _fail(exc)
    typer.echo(f"Installed: {launcher}\nHelix config: {path}")
    if backup:
        typer.echo(f"Backup: {backup}")
    typer.echo(
        "Reload Helix with :config-reload (or restart it).\n"
        "Run Helix/Yazi inside tmux: tmux new-session -A -s work\n"
        "Select a complete SQL query, then press Space followed by x."
    )


@app.command()
def doctor(
    file: Annotated[
        Path | None, typer.Option(help="Check project and dbt resolution for this buffer.")
    ] = None,
) -> None:
    """Check prerequisites without connecting to a database or reading profiles."""
    from dbt_enhance.dbt import discover_project, find_dbt
    from dbt_enhance.popup import tmux_binary

    checks: list[tuple[str, bool, str]] = []
    checks.append(
        (
            "Helix",
            bool(shutil.which("hx")),
            shutil.which("hx") or "hx not on PATH (requires Helix 25.07+)",
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
    if file:
        try:
            project = discover_project(file.expanduser().absolute())
            checks.append(("project", True, str(project)))
            override = os.environ.get("DBT_ENHANCE_DBT")
            executable = find_dbt(project, Path(override) if override else None)
            checks.append(("dbt", True, str(executable)))
            from dbt_enhance.worker import project_python, status

            selected = os.environ.get("DBT_ENHANCE_BACKEND", "process")
            checks.append(("backend", selected in {"process", "worker"}, selected))
            if selected == "worker":
                checks.append(("worker interpreter", True, str(project_python(executable))))
            running = status(project)
            detail = "not running"
            if running:
                detail = f"running (supervisor {running['pid']}, session {running['session_pid']})"
            checks.append(("project worker", True, detail))
        except (EnhanceError, OSError) as exc:
            checks.append(("project/dbt", False, str(exc)))
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


def main() -> None:
    app()
