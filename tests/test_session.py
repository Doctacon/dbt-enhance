from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SESSION_MODULE = Path(__file__).parents[1] / "src" / "dbt_enhance" / "_session.py"


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = (tmp_path / "project with spaces").resolve()
    profiles = (tmp_path / "explicit profiles").resolve()
    (project / "models").mkdir(parents=True)
    (project / "macros").mkdir()
    profiles.mkdir()
    (project / "dbt_project.yml").write_text(
        """name: native_session_test
version: '1.0'
config-version: 2
profile: native_session_test
model-paths: [models]
macro-paths: [macros]
"""
    )
    (profiles / "profiles.yml").write_text(
        f"""native_session_test:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: '{tmp_path / "disposable.duckdb"}'
      threads: 1
"""
    )
    (project / "models" / "values.sql").write_text(
        "{{ config(materialized='ephemeral') }}\nselect 7 as value\n"
    )
    (project / "macros" / "answer.sql").write_text("{% macro answer() %}42{% endmacro %}\n")
    (project / "models" / "sources.yml").write_text(
        """version: 2
sources:
  - name: preview_source
    schema: main
    tables:
      - name: values
        identifier: source_one
"""
    )
    return project, profiles


def _run_child(project: Path, profiles: Path, body: str) -> subprocess.CompletedProcess[str]:
    script = textwrap.dedent(
        f"""
        import importlib.util
        import json
        import os
        import sys
        import time
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "standalone_dbt_session", {str(SESSION_MODULE)!r}
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        DbtSession = module.DbtSession
        SessionError = module.SessionError
        QueryError = module.QueryError
        UnsupportedSession = module.UnsupportedSession
        project = Path({str(project)!r})
        profiles = Path({str(profiles)!r})
        context = {{"project_dir": str(project), "profiles_dir": str(profiles), "target": None}}
        {textwrap.indent(textwrap.dedent(body), "        ").lstrip()}
        """
    )
    environment = os.environ.copy()
    environment["DBT_POPULATE_CACHE"] = "false"
    environment["PYTHON_DOTENV_DISABLED"] = "1"
    environment.pop("DBT_PROFILES_DIR", None)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )


@pytest.mark.integration
def test_native_session_retains_connection_and_refreshes_project(tmp_path: Path) -> None:
    project, profiles = _project(tmp_path)
    completed = _run_child(
        project,
        profiles,
        r"""
        callback_calls = []
        def configured(paths):
            callback_calls.append({
                "paths": paths,
                "partial_parse_exists": (project / "target/partial_parse.msgpack").exists(),
            })

        session = DbtSession(context, configured)
        first = session.execute(
            "{% do run_query('create temp table kept as select 42 as marker') %} "
            "select * from {{ ref('values') }}",
            20,
        )
        # Create source tables on the retained connection through dbt's native
        # run_query macro, not through a separate DuckDB client.
        source_setup = session.execute(
            "{% do run_query('create table source_one as select 10 as source_value') %} "
            "{% do run_query('create table source_two as select 11 as source_value') %} "
            "select {{ answer() }} as answer",
            20,
        )
        handle_one = id(session._adapter.connections.get_thread_connection().handle)
        retained = session.execute("select * from kept", 20)
        handle_two = id(session._adapter.connections.get_thread_connection().handle)
        source_one = session.execute("select * from {{ source('preview_source', 'values') }}", 20)

        (project / "models/values.sql").write_text(
            "{{ config(materialized='ephemeral') }}\nselect 8 as value\n"
        )
        (project / "macros/answer.sql").write_text("{% macro answer() %}43{% endmacro %}\n")
        (project / "models/sources.yml").write_text(
            "version: 2\nsources:\n  - name: preview_source\n    schema: main\n"
            "    tables:\n      - name: values\n        identifier: source_two\n"
        )
        edited = session.execute(
            "select v.value, {{ answer() }} as answer, s.source_value "
            "from {{ ref('values') }} v cross join {{ source('preview_source', 'values') }} s",
            20,
        )
        empty = session.execute("select 1 as id, 'x' as label where false", 20)
        warning = session.execute(
            "{% do exceptions.warn('native session warning') %} select 1 as value", 20
        )
        capped_sql = session.execute(
            "select * from (select range as id from range(10) order by id desc limit 5) "
            "as dbt_enhance_preview",
            2,
        )
        freshness_one = session.execute(
            "select '{{ invocation_id }}' as invocation, '{{ run_started_at }}' as started, "
            "'{{ selected_resources | join(',') }}' as selected",
            20,
        )
        time.sleep(0.01)
        freshness_two = session.execute(
            "select '{{ invocation_id }}' as invocation, '{{ run_started_at }}' as started, "
            "'{{ selected_resources | join(',') }}' as selected",
            20,
        )
        connection = session._adapter.connections.get_thread_connection()
        session._adapter.connections.begin()
        assert connection.transaction_open
        session.execute("select 1 as transaction_check", 2)
        assert not connection.transaction_open
        # Exercise many request boundaries without growing thread connections or
        # retaining stale inline nodes/results.
        for marker in range(20):
            assert session.execute(f"select {marker} as marker", 2)["rows"] == [[marker]]
        assert id(connection.handle) == handle_one
        session.close()
        session.close()
        from dbt.adapters.contracts.connection import ConnectionState
        from dbt.adapters.factory import FACTORY
        assert connection.state == ConnectionState.CLOSED
        # Prove physical teardown, not just registry deletion: another process
        # must now be able to open the same writable DuckDB database.
        import subprocess
        unlocked = subprocess.run(
            [sys.executable, "-c", "import duckdb; "
             "db = duckdb.connect(" + repr(str(project.parent / "disposable.duckdb")) + "); "
             "db.execute('select 1'); db.close()"],
            capture_output=True, text=True, timeout=15,
        )
        assert unlocked.returncode == 0, unlocked.stderr
        print(json.dumps({
            "compatibility": module.compatibility(),
            "callback_calls": callback_calls,
            "first": first,
            "source_setup": source_setup,
            "retained": retained,
            "source_one": source_one,
            "edited": edited,
            "empty": empty,
            "warning": warning,
            "capped_sql": capped_sql,
            "freshness_one": freshness_one,
            "freshness_two": freshness_two,
            "same_handle": handle_one == handle_two,
            "partial_parse": (project / "target/partial_parse.msgpack").is_file(),
            "run_results": (project / "target/run_results.json").exists(),
            "factory_empty_after_close": not FACTORY.adapters,
        }, default=str))
        """,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert "Exception ignored" not in completed.stderr
    assert "unraisable" not in completed.stderr.lower()
    report = json.loads(completed.stdout)
    assert report["compatibility"]["dbt_core"]
    assert report["callback_calls"] == [
        {"paths": [str(profiles / "profiles.yml")], "partial_parse_exists": False},
        {"paths": [], "partial_parse_exists": False},
    ]
    assert report["first"]["rows"] == [[7]]
    assert report["source_setup"]["rows"] == [[42]]
    assert report["retained"]["rows"] == [[42]]
    assert report["source_one"]["rows"] == [[10]]
    assert report["edited"]["rows"] == [[8, 43, 11]]
    assert "__dbt__cte__values" in report["edited"]["compiled_sql"]
    assert report["empty"]["columns"] == ["id", "label"]
    assert report["empty"]["rows"] == []
    assert any("native session warning" in item for item in report["warning"]["warnings"])
    assert report["capped_sql"]["rows"] == [[9], [8]]
    assert report["same_handle"] is True
    assert report["partial_parse"] is True
    assert report["run_results"] is False
    assert report["factory_empty_after_close"] is True
    first_freshness = report["freshness_one"]["rows"][0]
    second_freshness = report["freshness_two"]["rows"][0]
    assert first_freshness[0] != second_freshness[0]
    assert first_freshness[1] != second_freshness[1]
    assert "inline_query" in first_freshness[2]
    assert "inline_query" in second_freshness[2]


@pytest.mark.integration
def test_query_errors_recover_but_output_cap_retires(tmp_path: Path) -> None:
    project, profiles = _project(tmp_path)
    completed = _run_child(
        project,
        profiles,
        r"""
        outcomes = {}
        failed = DbtSession(context)
        failed.execute(
            "{% do run_query('create temporary table failed_session_marker as select 1') %} "
            "select 1 as value", 2,
        )
        connection = failed._adapter.connections.get_thread_connection()
        handle = connection.handle
        # An error in an explicit transaction must roll back without closing it.
        failed._adapter.connections.begin()
        assert connection.transaction_open
        try:
            failed.execute("select * from relation_that_does_not_exist", 2)
        except QueryError as error:
            outcomes["database_error"] = str(error)
        assert not connection.transaction_open
        assert connection.handle is handle
        outcomes["retained"] = failed.execute("select * from failed_session_marker", 2)["rows"]
        # Also cover errors from SQL invoked during Jinja compilation.
        try:
            failed.execute("{% do run_query('select from') %} select 1", 2)
        except QueryError:
            pass
        else:
            raise AssertionError("invalid SQL unexpectedly succeeded")
        assert failed.execute("select 1", 2)["rows"] == [[1]]
        assert not any("inline_query" in key for key in failed._manifest.nodes)
        failed.close()
        failed.close()

        recovered = DbtSession(context)
        outcomes["recovered"] = recovered.execute("select 7 as value", 2)["rows"]
        assert recovered.execute(
            "select count(*) as n from information_schema.tables "
            "where table_name = 'failed_session_marker'", 2,
        )["rows"] == [[0]]
        recovered.close()

        oversized = DbtSession(context)
        try:
            oversized.execute("select repeat('x', 17 * 1024 * 1024) as value", 2)
        except SessionError as error:
            outcomes["cap_error"] = str(error)
        try:
            oversized.execute("select 1", 2)
        except SessionError as error:
            outcomes["cap_closed"] = str(error)
        oversized.close()

        after_cap = DbtSession(context)
        outcomes["after_cap"] = after_cap.execute("select 8 as value", 2)["rows"]
        after_cap.close()
        after_cap.close()
        from dbt.adapters.factory import FACTORY
        outcomes["factory_empty_after_close"] = not FACTORY.adapters
        print(json.dumps(outcomes))
        """,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert "Exception ignored" not in completed.stderr
    assert "unraisable" not in completed.stderr.lower()
    report = json.loads(completed.stdout)
    assert "relation_that_does_not_exist" in report["database_error"]
    assert report["retained"] == [[1]]
    assert report["recovered"] == [[7]]
    assert "16 MiB" in report["cap_error"]
    assert "closed" in report["cap_closed"]
    assert report["after_cap"] == [[8]]
    assert report["factory_empty_after_close"] is True


@pytest.mark.integration
def test_first_query_error_recovers_but_cleanup_failure_retires(tmp_path: Path) -> None:
    project, profiles = _project(tmp_path)
    completed = _run_child(
        project,
        profiles,
        r"""
        session = DbtSession(context)
        try:
            session.execute("select from", 2)
        except QueryError:
            pass
        else:
            raise AssertionError("invalid query succeeded")
        assert session.execute("select 9 as value", 2)["rows"] == [[9]]
        def broken_rollback():
            raise RuntimeError("synthetic cleanup failure")
        session._rollback_open_transaction = broken_rollback
        try:
            session.execute("select * from missing_table", 2)
        except SessionError as error:
            assert "missing_table" in str(error)
            assert error.reason == "request_cleanup_failed"
        else:
            raise AssertionError("failed cleanup must retire")
        assert session._closed
        try:
            session.execute("select 1", 2)
        except SessionError as error:
            assert "closed" in str(error)
        else:
            raise AssertionError("retired session reused")
        session.close()
        """,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout


@pytest.mark.integration
def test_parse_recovery_before_connection_and_manifest_cleanup_failure(tmp_path: Path) -> None:
    project, profiles = _project(tmp_path)
    completed = _run_child(
        project,
        profiles,
        r"""
        session = DbtSession(context)
        broken_model = project / "models/branch_model.sql"
        for contents in (
            "select * from {{ ref('missing_branch_model') }}",
            "select {{ broken_jinja",
        ):
            broken_model.write_text(contents)
            try:
                session.execute("select 1", 2)
            except QueryError:
                pass
            else:
                raise AssertionError("invalid project unexpectedly parsed")
            assert not session._closed
            assert session._manifest is None
            assert "manifest" not in session._ctx.obj
            connection = session._adapter.connections.get_if_exists()
            from dbt.adapters.contracts.connection import ConnectionState
            assert connection is None or connection.state == ConnectionState.INIT
        adapter = session._adapter
        broken_model.write_text("{{ config(materialized='ephemeral') }} select 8 as value")
        for bad_sql in (
            "select * from {{ ref('missing_branch_model') }}",
            "select {{ missing_branch_macro() }}",
            "select {{ broken_jinja",
        ):
            try:
                session.execute(bad_sql, 2)
            except QueryError:
                pass
            else:
                raise AssertionError("invalid inline SQL unexpectedly compiled")
            assert session._adapter is adapter
            connection = adapter.connections.get_if_exists()
            assert connection is None or connection.state == ConnectionState.INIT
        assert session.execute("select * from {{ ref('branch_model') }}", 2)["rows"] == [[8]]
        assert session._adapter is adapter
        connection = adapter.connections.get_thread_connection()
        handle = connection.handle
        adapter.connections.begin()
        try:
            session.execute("select * from {{ ref('missing_model') }}", 2)
        except QueryError:
            pass
        else:
            raise AssertionError("invalid ref unexpectedly compiled")
        assert not connection.transaction_open
        assert connection.handle is handle
        assert session.execute("select 9", 2)["rows"] == [[9]]
        assert connection.handle is handle
        def broken_discard():
            raise RuntimeError("synthetic manifest cleanup failure")
        session._discard_manifest = broken_discard
        try:
            session.execute("select * from {{ ref('missing_model') }}", 2)
        except SessionError as error:
            assert error.reason == "request_cleanup_failed"
        else:
            raise AssertionError("manifest cleanup failure must retire")
        assert session._closed
        session.close()
        """,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_compatibility_does_not_read_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DBT_PROFILES_DIR", str(tmp_path / "missing profiles"))
    monkeypatch.setenv("DBT_PROJECT_DIR", str(tmp_path / "missing project"))
    spec = importlib.util.spec_from_file_location("compatibility_only_session", SESSION_MODULE)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.compatibility()["dbt_core"]


def test_credential_path_keeps_the_alias_for_rotation(tmp_path: Path):
    from types import SimpleNamespace

    from dbt_enhance._session import DbtSession

    original = tmp_path / "original.pem"
    original.write_text("synthetic-only")
    alias = tmp_path / "key.pem"
    alias.symlink_to(original)
    session = DbtSession({"project_dir": str(tmp_path)})
    session._config = SimpleNamespace(credentials=SimpleNamespace(private_key_path=str(alias)))
    assert session._credential_paths() == [str(alias)]
    session.close()


def test_context_validation_is_lazy_and_does_not_import_dbt() -> None:
    source = SESSION_MODULE.read_text()
    assert "from dbt" not in source
    assert "import dbt" not in source

    namespace: dict[str, object] = {"__name__": "standalone_test"}
    exec(compile(source, str(SESSION_MODULE), "exec"), namespace)
    session_type = namespace["DbtSession"]
    with pytest.raises(ValueError, match="absolute"):
        session_type({"project_dir": "relative", "profiles_dir": None, "target": None})

    unopened = session_type({"project_dir": str(Path.cwd()), "profiles_dir": None, "target": None})
    unopened.close()
    unopened.close()

    scrub_error = namespace["_scrub_error"]
    scrubbed = scrub_error(
        "connect at snowflake://literal-user:literal-password@example.test/account"
    )
    assert scrubbed == "connect at <redacted URL>"
    assert "literal-user" not in scrubbed
    assert "literal-password" not in scrubbed

    supported_version = namespace["_supported_version"]
    assert supported_version("1.10.22") is True
    assert supported_version("1.10.21") is True
    assert supported_version("1.11.0") is False
    assert supported_version("1.12.6") is True
    assert supported_version("1.10.23") is True
    assert supported_version("1.12.5") is True
    assert supported_version("1.12.7") is True
    assert supported_version("1.12.0") is True
    assert supported_version("1.12.7rc1") is False
    assert supported_version("1.12.7.dev1") is False
    assert supported_version("1.12.7+local") is False
    assert supported_version("1.12") is False
    assert supported_version("garbage") is False
    assert supported_version("1.12.6.post1") is False
    assert supported_version("1.13.0") is False
