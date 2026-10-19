from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from dbt_enhance.dbt import run_query
from dbt_enhance.models import EnhanceError, QueryRequest

pytestmark = pytest.mark.integration


def _dbt_executable() -> Path:
    adjacent = Path(sys.executable).parent / "dbt"
    if adjacent.is_file():
        return adjacent
    found = shutil.which("dbt")
    if found is None:
        pytest.skip("dbt Core is not installed")
    return Path(found)


def _duckdb_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "dbt project with spaces"
    profiles = tmp_path / "profiles with spaces"
    models = project / "models"
    macros = project / "macros"
    models.mkdir(parents=True)
    macros.mkdir()
    profiles.mkdir()

    (project / "dbt_project.yml").write_text(
        """name: isolated_preview
version: '1.0'
config-version: 2
profile: isolated_preview
model-paths: [models]
macro-paths: [macros]
"""
    )
    (profiles / "profiles.yml").write_text(
        f"""isolated_preview:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: '{tmp_path / "isolated.duckdb"}'
      threads: 1
"""
    )
    (models / "values.sql").write_text(
        """{{ config(materialized='ephemeral') }}
select 1 as id, 'alpha' as label, null as missing, true as enabled
union all
select 2 as id, 'beta' as label, 3 as missing, false as enabled
"""
    )
    (macros / "answer.sql").write_text("{% macro answer() %}42{% endmacro %}\n")
    source = project / "editor query.sql"
    source.write_text("-- source path used for project discovery\n")
    return project, profiles, source


def test_real_dbt_duckdb_preview_compiles_and_executes_inline_jinja(
    tmp_path: Path,
) -> None:
    project, profiles, source = _duckdb_project(tmp_path)

    result = run_query(
        QueryRequest(
            sql="""
{% if target.name == 'dev' %}
select *, {{ answer() }} as answer from {{ ref('values') }} order by id
{% else %}
select 0 as id
{% endif %}
""",
            file=source,
            limit=3,
            profiles_dir=profiles,
            dbt=_dbt_executable(),
            timeout=60,
            backend="process",
        )
    )

    assert result.project_dir == project
    assert result.columns == ["id", "label", "missing", "enabled", "answer"]
    assert result.rows == [
        [1, "alpha", None, True, 42],
        [2, "beta", 3, False, 42],
    ]
    assert "__dbt__cte__values" in result.compiled_sql
    assert "42 as answer" in result.compiled_sql
    assert "limit 3" in result.compiled_sql.lower()
    assert "{{" not in result.compiled_sql
    assert (project / "target" / "partial_parse.msgpack").is_file()
    assert (project / "target" / "run_results.json").is_file()
    assert (project / "logs" / "dbt.log").is_file()
    assert not (profiles / ".user.yml").exists()


def test_real_dbt_reuses_project_partial_parse_cache_for_changed_selection(tmp_path, monkeypatch):
    from dbt_enhance import dbt as backend

    project, profiles, source = _duckdb_project(tmp_path)
    # Use a 14-column / 10,000-row preview, with a different unsaved selection
    # on the second run. Only the project manifest should be reused, never rows/SQL.
    columns = ", ".join(f"range + {index} as c{index}" for index in range(14))
    events_per_run = []
    original = backend._run_process
    monkeypatch.setenv("DBT_LOG_LEVEL", "debug")

    def capture_events(*args, **kwargs):
        code, output = original(*args, **kwargs)
        events_per_run.append(backend._json_events(output))
        return code, output

    monkeypatch.setattr(backend, "_run_process", capture_events)

    def query(offset):
        return run_query(
            QueryRequest(
                sql=f"select {columns} from range({offset}, {offset + 10000})",
                file=source,
                profiles_dir=profiles,
                dbt=_dbt_executable(),
                limit=10000,
                backend="process",
            )
        )

    cold = query(0)
    cache = project / "target" / "partial_parse.msgpack"
    assert cache.is_file(), "Preview discarded the project's reusable partial-parse cache"
    warm = query(10000)
    assert len(cold.columns) == len(warm.columns) == 14
    assert len(cold.rows) == len(warm.rows) == 10000
    assert cold.rows[0][0] == 0
    assert warm.rows[0][0] == 10000
    assert cold.compiled_sql != warm.compiled_sql
    names = {event.get("info", {}).get("name") for event in events_per_run[1]}
    assert "PartialParsingSkipParsing" in names
    artifact = json.loads((project / "target" / "run_results.json").read_text())
    assert artifact["results"][0]["compiled_code"] == warm.compiled_sql
    print(f"14 columns x 10,000 rows: cold={cold.elapsed:.2f}s; warm={warm.elapsed:.2f}s")


@pytest.mark.parametrize("configuration", ["project", "environment"])
def test_real_dbt_honors_custom_artifact_and_log_paths(tmp_path, monkeypatch, configuration):
    project, profiles, source = _duckdb_project(tmp_path)
    if configuration == "project":
        target = project / "custom target"
        logs = project / "custom logs"
        config = project / "dbt_project.yml"
        config.write_text(
            config.read_text() + 'target-path: "custom target"\nlog-path: "custom logs"\n'
        )
    else:
        target = tmp_path / "absolute target"
        logs = tmp_path / "absolute logs"
        # An explicit environment setting must win over a project-level setting.
        config = project / "dbt_project.yml"
        config.write_text(config.read_text() + 'target-path: "unused target"\n')
        monkeypatch.setenv("DBT_TARGET_PATH", str(target))
        monkeypatch.setenv("DBT_LOG_PATH", str(logs))
    result = run_query(
        QueryRequest(
            sql="select {{ answer() }} as answer",
            file=source,
            profiles_dir=profiles,
            dbt=_dbt_executable(),
            backend="process",
        )
    )
    assert result.rows == [[42]]
    assert "42 as answer" in result.compiled_sql
    assert (target / "partial_parse.msgpack").is_file()
    assert (target / "run_results.json").is_file()
    assert (logs / "dbt.log").is_file()
    assert not (project / "target").exists()
    assert not (project / "unused target").exists()


def test_real_dbt_terminal_semicolon_with_comment_preserves_literals_and_limit(tmp_path):
    _, profiles, source = _duckdb_project(tmp_path)
    result = run_query(
        QueryRequest(
            sql=(
                "select {{ answer() }} as answer, 'literal;' as label from range(5); -- trailing;\n"
            ),
            file=source,
            profiles_dir=profiles,
            dbt=_dbt_executable(),
            limit=2,
            backend="process",
        )
    )
    assert result.rows == [[42, "literal;"], [42, "literal;"]]
    assert "limit 2" in result.compiled_sql.lower()
    assert "literal;" in result.compiled_sql


@pytest.mark.parametrize("inner_limit", [1, 5])
def test_real_dbt_existing_limit_keeps_both_selection_and_preview_caps(tmp_path, inner_limit):
    _, profiles, source = _duckdb_project(tmp_path)
    result = run_query(
        QueryRequest(
            sql=f"select range as id from range(10) order by id desc limit {inner_limit}; -- end",
            file=source,
            profiles_dir=profiles,
            dbt=_dbt_executable(),
            limit=2,
            backend="process",
        )
    )
    assert len(result.rows) == min(inner_limit, 2)
    assert all(row[0] in range(10 - inner_limit, 10) for row in result.rows)
    assert f"limit {inner_limit}" in result.compiled_sql.lower()
    assert "limit 2" in result.compiled_sql.lower()
    assert "dbt_enhance_preview" in result.compiled_sql


def test_real_dbt_introspection_works_without_eager_cache_population(tmp_path, monkeypatch):
    import duckdb

    _, profiles, source = _duckdb_project(tmp_path)
    monkeypatch.delenv("DBT_POPULATE_CACHE", raising=False)
    with duckdb.connect(str(tmp_path / "isolated.duckdb")) as connection:
        connection.execute("create table introspection_values as select 7 as id, 'seven' as label")

    result = run_query(
        QueryRequest(
            sql="""
{% if execute %}
  {% set relation = adapter.get_relation(database=target.database, schema='main',
                                         identifier='introspection_values') %}
  {% if relation is none %}
    {{ exceptions.raise_compiler_error('Introspection failed on a cold relation cache') }}
  {% endif %}
  {% set columns = adapter.get_columns_in_relation(relation) %}
  select {% for column in columns %}
    {{ adapter.quote(column.name) }}{% if not loop.last %}, {% endif %}
  {% endfor %}
  from {{ relation }}
{% else %}
  select 0 as id
{% endif %}
""",
            file=source,
            profiles_dir=profiles,
            dbt=_dbt_executable(),
            limit=2,
            backend="process",
        )
    )
    assert result.columns == ["id", "label"]
    assert result.rows == [[7, "seven"]]


def test_real_dbt_empty_preview_and_database_error_are_structured(
    tmp_path: Path,
) -> None:
    _, profiles, source = _duckdb_project(tmp_path)
    dbt = _dbt_executable()

    empty = run_query(
        QueryRequest(
            sql="select 1 as id where false",
            file=source,
            profiles_dir=profiles,
            dbt=dbt,
            timeout=60,
            backend="process",
        )
    )
    assert empty.columns == []
    assert empty.rows == []
    assert empty.warnings

    with pytest.raises(EnhanceError, match="(?i)(database|catalog|does not exist)"):
        run_query(
            QueryRequest(
                sql="select * from relation_that_does_not_exist",
                file=source,
                profiles_dir=profiles,
                dbt=dbt,
                timeout=60,
                backend="process",
            )
        )
