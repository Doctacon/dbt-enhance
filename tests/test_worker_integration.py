"""End-to-end worker tests against disposable local dbt/DuckDB only."""

from __future__ import annotations

import math
import tempfile
import time
from dataclasses import replace
from importlib.metadata import version

import pytest
from test_integration import _dbt_executable, _duckdb_project

from dbt_enhance import worker
from dbt_enhance.dbt import run_query
from dbt_enhance.models import EnhanceError, QueryRequest

pytestmark = pytest.mark.integration


@pytest.fixture
def native_worker(tmp_path, monkeypatch):
    with tempfile.TemporaryDirectory(prefix="de-native-", dir="/tmp") as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
        monkeypatch.delenv("DBT_POPULATE_CACHE", raising=False)
        monkeypatch.delenv("DBT_ENGINE_POPULATE_CACHE", raising=False)
        project, profiles, source = _duckdb_project(tmp_path)
        request = QueryRequest(
            sql="select 1 as value",
            file=source,
            profiles_dir=profiles,
            dbt=_dbt_executable(),
            backend="worker",
            timeout=60,
        )
        try:
            yield project, request
        finally:
            try:
                worker.stop(project.resolve())
            except (EnhanceError, OSError):
                pass


def test_real_worker_keeps_connection_and_native_compilation_fresh(native_worker):
    project, request = native_worker
    first = run_query(
        replace(
            request,
            sql="""
{% if execute %}
  {% do run_query('create temporary table retained_session as select 73 as marker') %}
{% endif %}
select *, {{ answer() }} as answer from {{ ref('values') }} order by id
""",
        )
    )
    information = worker.status(project.resolve())
    assert information["versions"]["dbt_core"] == version("dbt-core")
    pid = information["session_pid"]
    assert first.rows == [[1, "alpha", None, True, 42], [2, "beta", 3, False, 42]]
    assert "__dbt__cte__values" in first.compiled_sql
    assert "limit 500" in first.compiled_sql.lower()

    # A temporary table cannot survive reopening the database connection.
    second = run_query(replace(request, sql="select * from retained_session"))
    assert second.rows == [[73]]
    assert worker.status(project.resolve())["session_pid"] == pid
    assert first.compiled_sql != second.compiled_sql

    (project / "macros/answer.sql").write_text("{% macro answer() %}43{% endmacro %}\n")
    (project / "models/values.sql").write_text(
        "{{ config(materialized='ephemeral') }}\nselect 9 as id\n"
    )
    changed = run_query(
        replace(request, sql="select *, {{ answer() }} as answer from {{ ref('values') }}")
    )
    assert changed.rows == [[9, 43]]
    assert worker.status(project.resolve())["session_pid"] == pid
    assert (project / "target/partial_parse.msgpack").is_file()
    assert not (project / "target/run_results.json").exists()
    assert (project / "logs/dbt.log").is_file()

    empty = run_query(replace(request, sql="select 1 as id where false"))
    assert empty.columns == ["id"]
    assert empty.rows == []
    assert empty.warnings == []

    numeric = run_query(
        replace(
            request,
            sql="select 'NaN'::double as n, 'Infinity'::double as i, "
            "12.50::decimal(8, 2) as amount, date '2025-01-02' as day",
        )
    )
    assert math.isnan(numeric.rows[0][0])
    assert math.isinf(numeric.rows[0][1])
    assert numeric.rows[0][2:] == [12.5, "2025-01-02"]

    capped = run_query(
        replace(request, sql="select range as id from range(10) limit 8; -- end", limit=2)
    )
    assert len(capped.rows) == 2
    assert "limit 8" in capped.compiled_sql.lower()
    assert "limit 2" in capped.compiled_sql.lower()
    assert "dbt_enhance_preview" in capped.compiled_sql
    print(f"native worker cold={first.elapsed:.3f}s warm={second.elapsed:.3f}s")


def test_real_worker_wide_previews_have_fresh_rows_and_sql(native_worker):
    project, request = native_worker
    columns = ", ".join(f"range + {index} as c{index}" for index in range(14))
    results = [
        run_query(
            replace(
                request,
                sql=f"select {columns} from range({offset}, {offset + 10000})",
                limit=10000,
            )
        )
        for offset in (0, 10000)
    ]
    assert len(results[0].columns) == len(results[1].columns) == 14
    assert len(results[0].rows) == len(results[1].rows) == 10000
    assert results[0].rows[0][0] == 0
    assert results[1].rows[0][0] == 10000
    assert results[0].compiled_sql != results[1].compiled_sql
    assert (project / "target/partial_parse.msgpack").is_file()
    print(f"worker 14 x 10000 cold={results[0].elapsed:.3f}s warm={results[1].elapsed:.3f}s")


@pytest.mark.skipif(
    not version("dbt-core").startswith("1.12."), reason="Core 1.12 loads project dotenv files"
)
@pytest.mark.parametrize("failed_first_query", [False, True])
def test_profile_resolved_by_dotenv_is_watched(native_worker, monkeypatch, failed_first_query):
    project, request = native_worker
    monkeypatch.setenv("HOME", str(project.parent / "synthetic-home"))
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED")
    monkeypatch.delenv("DBT_PROFILES_DIR", raising=False)
    monkeypatch.delenv("DBT_ENGINE_PROFILES_DIR", raising=False)
    (project / ".env").write_text(f"DBT_PROFILES_DIR='{request.profiles_dir}'\n")
    profile = request.profiles_dir / "profiles.yml"
    request = replace(request, profiles_dir=None, sql="select '{{ target.name }}' as target_name")
    if failed_first_query:
        with pytest.raises(EnhanceError):
            run_query(replace(request, sql="select from"))
    else:
        assert run_query(request).rows == [["dev"]]
    before = worker.status(project.resolve())["session_pid"]
    profile.write_text(
        profile.read_text()
        .replace("target: dev", "target: alternate")
        .replace(
            "    dev:",
            "    alternate:",
        )
    )
    assert run_query(request).rows == [["alternate"]]
    assert worker.status(project.resolve())["session_pid"] != before


@pytest.mark.parametrize(
    "bad_sql",
    [
        "select * from missing_test_table",
        "select from",
        "select * from {{ ref('missing_branch_model') }}",
        "select {{ missing_branch_macro() }}",
        "select {{ broken_jinja",
    ],
)
def test_real_worker_query_error_keeps_session_and_connection(native_worker, bad_sql):
    project, request = native_worker
    run_query(
        replace(
            request,
            sql="{% do run_query('create temp table kept_after_error as select 73 as marker') %} "
            "select 1 as value",
        )
    )
    before = worker.status(project.resolve())["session_pid"]
    with pytest.raises(EnhanceError):
        run_query(replace(request, sql=bad_sql))
    recovered = run_query(replace(request, sql="select * from kept_after_error"))
    assert recovered.rows == [[73]]
    assert worker.status(project.resolve())["session_pid"] == before


def test_real_worker_recovers_after_project_parse_error(native_worker):
    project, request = native_worker
    run_query(
        replace(
            request,
            sql="{% do run_query('create temp table kept_after_parse as select 73 as marker') %} "
            "select 1 as value",
        )
    )
    before = worker.status(project.resolve())["session_pid"]
    # Simulate switching branches to a model with a missing dependency.
    model = project / "models" / "branch_model.sql"
    model.write_text("select * from {{ ref('missing_on_branch') }}")
    with pytest.raises(EnhanceError, match="missing_on_branch"):
        run_query(request)
    assert worker.status(project.resolve())["session_pid"] == before
    model.write_text("{{ config(materialized='ephemeral') }} select 8 as value")
    recovered = run_query(
        replace(
            request,
            sql="select value, marker from {{ ref('branch_model') }} cross join kept_after_parse",
        )
    )
    assert recovered.rows == [[8, 73]]
    assert worker.status(project.resolve())["session_pid"] == before
    assert worker.retirement_status(project.resolve()) is None


def test_real_worker_timeout_kills_active_session_and_allows_restart(native_worker):
    project, request = native_worker
    run_query(request)
    before = worker.status(project.resolve())["session_pid"]
    started = time.monotonic()
    with pytest.raises(EnhanceError, match="timed out"):
        run_query(
            replace(
                request,
                timeout=0.5,
                sql="""
select sum(a.range * b.range)
from range(1000000000) a cross join range(1000000000) b
""",
            )
        )
    assert time.monotonic() - started < 4
    assert run_query(request).rows == [[1]]
    assert worker.status(project.resolve())["session_pid"] != before
