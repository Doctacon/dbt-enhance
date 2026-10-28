"""Offline process-vs-worker benchmark, including client IPC and result decoding.

Run with `uv run python docs/benchmarks/dbt_worker.py`, or use
`uv run --with 'dbt-core==1.10.22' python docs/benchmarks/dbt_worker.py`.
Only disposable DuckDB profiles/databases are used. TUI startup/rendering and
remote warehouse authentication/execution are deliberately not measured.
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

from dbt_enhance import worker
from dbt_enhance.dbt import run_query
from dbt_enhance.models import QueryRequest


def main() -> None:
    for key in list(os.environ):
        if key.startswith("DBT_"):
            del os.environ[key]
    os.environ["DBT_POPULATE_CACHE"] = "false"
    os.environ["DBT_SEND_ANONYMOUS_USAGE_STATS"] = "false"
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    executable = Path(sys.executable).parent / "dbt"
    if not executable.is_file():
        raise RuntimeError("Run in uv's development environment with dbt-duckdb installed.")

    report = {
        "dbt_core": version("dbt-core"),
        "dbt_duckdb": version("dbt-duckdb"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "samples_per_workload": 3,
        "workloads": {},
    }
    with (
        tempfile.TemporaryDirectory(prefix="dbe-bench-", dir="/tmp") as runtime,
        tempfile.TemporaryDirectory(prefix="dbt-worker-benchmark-") as root,
    ):
        os.environ["XDG_RUNTIME_DIR"] = runtime
        project = Path(root).resolve()
        for variable, name in (
            ("HOME", "home"),
            ("XDG_CONFIG_HOME", "config"),
            ("XDG_STATE_HOME", "state"),
        ):
            directory = project / name
            directory.mkdir()
            os.environ[variable] = str(directory)
        profiles = project / "profiles"
        profiles.mkdir()
        (project / "dbt_project.yml").write_text(
            "name: worker_benchmark\nversion: '1.0'\nconfig-version: 2\nprofile: worker_benchmark\n"
        )
        (profiles / "profiles.yml").write_text(
            "worker_benchmark:\n  target: dev\n  outputs:\n    dev:\n"
            f"      type: duckdb\n      path: '{project / 'benchmark.duckdb'}'\n"
            "      threads: 1\n"
        )
        source = project / "selection.sql"
        source.write_text("-- Unsaved selections below, not this file, are executed.\n")
        request = QueryRequest(
            sql="select 0 as warmup",
            file=source,
            profiles_dir=profiles,
            dbt=executable,
            target="dev",
            limit=10000,
            timeout=60,
            backend="process",  # Warm disk state without retaining DuckDB's writer lock.
            diagnostics=False,  # Disposable benchmark projects are not user history.
        )

        def sample(backend: str, workload: str) -> dict:
            timings = []
            previous_sql = None
            for index in range(3):
                if workload == "one_row":
                    sql, row_count, column_count = f"select {index} as value", 1, 1
                    first_value = index
                else:
                    first_value = index * 10000
                    columns = ", ".join(
                        f"range + {first_value + column} as c{column}" for column in range(14)
                    )
                    sql = f"select {columns} from range(10000)"
                    row_count, column_count = 10000, 14
                started = time.perf_counter()
                result = run_query(replace(request, sql=sql, backend=backend))
                timings.append(round(time.perf_counter() - started, 4))
                assert len(result.rows) == row_count
                assert len(result.columns) == column_count
                assert result.rows[0][0] == first_value
                assert result.compiled_sql != previous_sql
                assert "limit 10000" in result.compiled_sql.lower()
                previous_sql = result.compiled_sql
            return {"seconds": timings, "median_seconds": round(statistics.median(timings), 4)}

        try:
            run_query(request)  # Warm the normal on-disk partial-parse cache.
            # Finish CLI runs before retaining DuckDB's database-file lock.
            for workload in ("one_row", "wide_10000"):
                report["workloads"][workload] = {"process": sample("process", workload)}
            started = time.perf_counter()
            run_query(replace(request, backend="worker"))
            report["first_worker_seconds_with_warm_disk_cache"] = round(
                time.perf_counter() - started, 4
            )
            information = worker.status(project)
            assert information["versions"]["dbt_core"] == report["dbt_core"]
            run_query(
                replace(
                    request,
                    backend="worker",
                    sql="{% do run_query('create temporary table retained "
                    "as select 73 as marker') %} select 0 as warmup",
                )
            )
            for workload in ("one_row", "wide_10000"):
                report["workloads"][workload]["worker"] = sample("worker", workload)
            assert run_query(
                replace(
                    request,
                    backend="worker",
                    sql="select * from retained",
                )
            ).rows == [[73]]
            assert worker.status(project)["session_pid"] == information["session_pid"]
            report["retained_connection_temporary_table"] = True
            report["partial_parse_cache"] = (project / "target/partial_parse.msgpack").is_file()
        finally:
            worker.stop(project)
        assert worker.status(project) is None
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
