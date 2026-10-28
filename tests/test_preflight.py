"""Native preflight tests use only disposable local DuckDB configuration."""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_integration import _dbt_executable, _duckdb_project

from dbt_enhance.models import EnhanceError, QueryRequest
from dbt_enhance.preflight import resolve_context

pytestmark = pytest.mark.integration


@pytest.fixture
def native_request(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.delenv("DBT_TARGET", raising=False)
    monkeypatch.delenv("DBT_PROFILES_DIR", raising=False)
    project, profiles, source = _duckdb_project(tmp_path)
    profile = profiles / "profiles.yml"
    profile.write_text(
        profile.read_text()
        + f"    alternate:\n      type: duckdb\n      path: '{tmp_path / 'alternate.duckdb'}'\n"
        + "      threads: 1\n"
    )
    return project, QueryRequest(
        sql="select synthetic_selected_sql_that_must_not_reach_preflight",
        file=source,
        profiles_dir=profiles,
        dbt=_dbt_executable(),
        backend="worker",
    )


def test_native_preflight_resolves_default_without_dbt_execution(native_request):
    project, request = native_request
    resolved = resolve_context(request)
    assert resolved.project_dir == project
    assert resolved.target == "dev"
    assert resolved.adapter == "duckdb"
    assert resolved.versions["dbt_core"]
    assert not (project / "target").exists()
    assert not (project.parent / "isolated.duckdb").exists()


def test_native_preflight_honors_environment_and_explicit_target(native_request, monkeypatch):
    _, request = native_request
    monkeypatch.setenv("DBT_TARGET", "alternate")
    assert resolve_context(request).target == "alternate"
    assert resolve_context(replace(request, target="dev")).target == "dev"


def test_target_allowlist_rejects_before_macro_parse_or_connection(native_request):
    project, request = native_request
    # This file would fail project parsing/compilation if preflight crossed its
    # lifecycle boundary. The target policy must reject before either can occur.
    (project / "macros" / "side_effect.sql").write_text(
        "{% do run_query('create table forbidden_side_effect as select 1') %}\n"
    )
    with pytest.raises(EnhanceError) as captured:
        resolve_context(replace(request, allowed_targets=()))
    assert captured.value.code == "target_not_allowed"
    assert "not allowed" in str(captured.value)
    assert not (project / "target").exists()
    assert not (project.parent / "isolated.duckdb").exists()
