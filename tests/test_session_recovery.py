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


def test_compilation_errors_exclude_configuration_database_and_internal_errors():
    from dbt.exceptions import ParsingError, TargetNotFoundError
    from dbt_common.exceptions import (
        CompilationError,
        DbtConfigError,
        DbtDatabaseError,
        DbtRuntimeError,
    )

    assert issubclass(TargetNotFoundError, CompilationError)
    for error in (CompilationError("invalid Jinja"), ParsingError("invalid project model")):
        assert DbtSession._is_compilation_error(error)
    for error in (
        DbtConfigError("invalid credentials"),
        DbtDatabaseError("connection lost"),
        DbtRuntimeError("unknown failure"),
        ValueError("internal bug"),
    ):
        assert not DbtSession._is_compilation_error(error)


@pytest.mark.parametrize("link", ["__cause__", "__context__"])
def test_inline_parser_wrapper_preserves_compile_error_classification(link):
    from dbt_common.exceptions import CompilationError, DbtBaseException, DbtDatabaseError

    wrapped = DbtBaseException("Error parsing inline query")
    setattr(wrapped, link, CompilationError("missing ref"))
    assert DbtSession._is_compilation_error(wrapped)
    for fatal in (DbtDatabaseError("connection lost"), ValueError("internal failure")):
        setattr(fatal, link, wrapped)
        assert not DbtSession._is_compilation_error(fatal)
    other = DbtBaseException("unknown wrapper")
    setattr(wrapped, link, other)
    setattr(other, link, wrapped)
    assert not DbtSession._is_compilation_error(wrapped)


@pytest.mark.parametrize("state_name,allowed", [("INIT", True), ("CLOSED", False), ("FAIL", False)])
def test_compile_recovery_never_opens_a_connection(tmp_path, state_name, allowed):
    from dbt.adapters.contracts.connection import ConnectionState

    class Connection:
        state = getattr(ConnectionState, state_name)

        @property
        def handle(self):
            raise AssertionError("must not resolve a lazy/closed handle")

    session = DbtSession({"project_dir": str(tmp_path)})
    session._adapter = SimpleNamespace(connections=SimpleNamespace(get_if_exists=Connection))
    assert session._connection_can_compile() is allowed
    session._adapter.connections.get_if_exists = lambda: None
    assert session._connection_can_compile()


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
