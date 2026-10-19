"""Recovery boundaries: statement errors are not transport/internal failures."""

from types import SimpleNamespace

import pytest

from dbt_enhance._session import DbtSession


def test_duckdb_error_classification(tmp_path):
    import duckdb
    from dbt_common.exceptions import DbtRuntimeError

    session = DbtSession({"project_dir": str(tmp_path)})
    session._config = SimpleNamespace(credentials=SimpleNamespace(type="duckdb"))
    for error_type in (duckdb.ParserException, duckdb.CatalogException, duckdb.BinderException):
        error = DbtRuntimeError("query failed")
        error.__cause__ = error_type("bad SQL")
        assert session._is_query_error(error)
    for cause in (duckdb.IOException("disconnected"), RuntimeError("internal failure")):
        error = DbtRuntimeError("query failed")
        error.__cause__ = cause
        assert not session._is_query_error(error)
    assert not session._is_query_error(DbtRuntimeError("unknown dbt failure"))
    assert not session._is_query_error(ValueError("internal failure"))
    assert (
        session._query_error_reason(DbtRuntimeError("unknown dbt failure"))
        == "driver_error_missing"
    )
    assert session._query_error_reason(ValueError("internal failure")) == "non_dbt_error"


@pytest.mark.parametrize(
    "sqlstate,expected",
    [
        ("42000", True),
        ("42S02", True),
        ("22000", True),
        ("23000", True),
        ("08003", False),
        ("28000", False),
        ("58000", False),
        (None, False),
    ],
)
def test_snowflake_error_classification(tmp_path, monkeypatch, sqlstate, expected):
    import sys

    from dbt_common.exceptions import DbtDatabaseError

    class ProgrammingError(Exception):
        pass

    # Match Snowflake adapter's implicit exception context, without credentials.
    monkeypatch.setitem(
        sys.modules,
        "snowflake.connector.errors",
        SimpleNamespace(
            ProgrammingError=ProgrammingError,
        ),
    )
    session = DbtSession({"project_dir": str(tmp_path)})
    session._config = SimpleNamespace(credentials=SimpleNamespace(type="snowflake"))
    driver_error = ProgrammingError("synthetic SQL failure")
    driver_error.sqlstate = sqlstate
    error = DbtDatabaseError("scrubbed adapter message")
    error.__context__ = driver_error
    assert session._is_query_error(error) is expected
    assert session._query_error_reason(error) == (None if expected else "sqlstate_not_recoverable")


def test_closed_connection_does_not_resolve_lazy_handle(tmp_path):
    from dbt.adapters.contracts.connection import ConnectionState

    class Connection:
        state = ConnectionState.CLOSED

        @property
        def handle(self):
            raise AssertionError("must not resolve a closed/lazy connection")

    session = DbtSession({"project_dir": str(tmp_path)})
    session._adapter = SimpleNamespace(connections=SimpleNamespace(get_if_exists=Connection))
    assert not session._connection_is_open()
