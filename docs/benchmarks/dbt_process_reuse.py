"""Offline feasibility probe, NOT a persistent-runner implementation.

Run from the checkout with `uv run python docs/benchmarks/dbt_process_reuse.py`.
Only disposable DuckDB data/profiles are used. Measures the existing CLI runner
against sequential dbtRunner invocations in one process, with identical CLI args
and result/event parsing. UI rendering and a daemon/IPC transport are excluded.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import statistics
import sys
import tempfile
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

from dbt_enhance import dbt as backend
from dbt_enhance.models import QueryRequest


def main() -> None:
    # Never use the caller's profiles, artifact paths, flags, or dotenv files.
    # These changes affect only this standalone benchmark process and its children.
    for key in list(os.environ):
        if key.startswith("DBT_"):
            del os.environ[key]
    os.environ["DBT_POPULATE_CACHE"] = "false"
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    dbt_executable = Path(sys.executable).parent / "dbt"
    if not dbt_executable.is_file():
        raise RuntimeError("Run with uv sync's development environment (dbt-duckdb required).")

    report: dict[str, Any] = {
        "dbt_core": version("dbt-core"),
        "dbt_duckdb": version("dbt-duckdb"),
        "python": sys.version.split()[0],
        "samples_per_workload": 3,
        "workloads": {},
    }
    previous_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="dbt-enhance-benchmark-") as temporary:
        project = Path(temporary).resolve()
        models = project / "models"
        profiles = project / "profiles"
        models.mkdir()
        profiles.mkdir()
        (project / "dbt_project.yml").write_text(
            "name: process_benchmark\nversion: '1.0'\nconfig-version: 2\n"
            "profile: process_benchmark\nmodel-paths: [models]\n"
        )
        (profiles / "profiles.yml").write_text(
            "process_benchmark:\n  target: dev\n  outputs:\n    dev:\n"
            f"      type: duckdb\n      path: '{project / 'benchmark.duckdb'}'\n"
            "      threads: 1\n"
        )
        model = models / "base.sql"
        model.write_text("{{ config(materialized='ephemeral') }}\nselect 7 as value\n")
        source = project / "selection.sql"
        source.write_text("-- Discovery path only; selections are supplied in memory.\n")
        os.chdir(project)
        try:
            command_template: list[str] = []
            original_process = backend._run_process

            def record_command(command: list[str], **kwargs: Any) -> tuple[int, str]:
                command_template[:] = command[1:]
                return original_process(command, **kwargs)

            def cli_query(sql: str) -> list[list[object]]:
                return backend.run_query(
                    QueryRequest(
                        sql=sql,
                        file=source,
                        dbt=dbt_executable,
                        profiles_dir=profiles,
                        limit=10000,
                        timeout=60,
                    )
                ).rows

            def selection(workload: str, index: int) -> tuple[str, int, int, int]:
                if workload == "one_row":
                    return f"select {index} as value", 1, 1, index
                offset = index * 10000
                columns = ", ".join(
                    f"range + {offset + column} as c{column}" for column in range(14)
                )
                return f"select {columns} from range(10000)", 10000, 14, offset

            def sample(query, workload):
                timings = []
                for index in range(3):
                    sql, expected_rows, expected_columns, first_value = selection(workload, index)
                    started = time.perf_counter()
                    rows = query(sql)
                    timings.append(round(time.perf_counter() - started, 4))
                    assert len(rows) == expected_rows
                    assert len(rows[0]) == expected_columns
                    assert rows[0][0] == first_value, "A previous selection leaked into this run"
                return {"seconds": timings, "median_seconds": round(statistics.median(timings), 4)}

            backend._run_process = record_command
            try:
                cli_query("select 0 as warmup")  # Populate the same normal partial-parse cache.
                for workload in ("one_row", "wide_10000"):
                    report["workloads"][workload] = {"fresh_cli": sample(cli_query, workload)}
            finally:
                backend._run_process = original_process

            started = time.perf_counter()
            from dbt.cli.main import dbtRunner

            report["dbt_import_seconds"] = round(time.perf_counter() - started, 4)
            runner = dbtRunner()  # No manifest injection: dbt owns cache invalidation.

            def invoke(sql):
                arguments = command_template.copy()
                arguments[arguments.index("--inline") + 1] = sql
                capture = io.StringIO()
                with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
                    outcome = runner.invoke(arguments)
                return outcome, capture.getvalue()

            def reused_query(sql):
                outcome, output = invoke(sql)
                if not outcome.success:
                    raise RuntimeError(str(outcome.exception or backend._dbt_error_detail(output)))
                events = backend._json_events(output)
                show = backend._show_event(events)
                preview = backend._parse_preview(show["data"])
                backend._compiled_sql(project, events, show)  # Same artifact/identity checks.
                _, rows, _ = backend._rows_from_preview(preview)
                return rows

            started = time.perf_counter()
            assert reused_query("select 0 as warmup") == [[0]]
            report["first_in_process_invoke_seconds"] = round(time.perf_counter() - started, 4)
            for workload in ("one_row", "wide_10000"):
                report["workloads"][workload]["reused_process"] = sample(reused_query, workload)

            failed, _ = invoke("select * from table_that_does_not_exist")
            assert not failed.success
            assert reused_query("select 999 as recovered") == [[999]]
            assert reused_query("select * from {{ ref('base') }}") == [[7]]
            model.write_text("{{ config(materialized='ephemeral') }}\nselect 8 as value\n")
            assert reused_query("select * from {{ ref('base') }}") == [[8]]
            report["checks"] = {
                "changed_selections": True,
                "recovery_after_sql_error": True,
                "changed_project_model": True,
            }
        finally:
            os.chdir(previous_cwd)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
