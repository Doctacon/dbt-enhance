from __future__ import annotations

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
    assert not (project / "target").exists()
    assert not (project / "logs").exists()
    assert not (profiles / ".user.yml").exists()


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
        )
    )
    assert result.rows == [[42, "literal;"], [42, "literal;"]]
    assert "limit 2" in result.compiled_sql.lower()
    assert "literal;" in result.compiled_sql


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
            )
        )
