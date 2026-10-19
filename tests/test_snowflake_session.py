"""Offline native Snowflake adapter tests; never connect to a warehouse.

Run with: uv run --with dbt-snowflake python -m pytest tests/test_snowflake_session.py
"""

import pytest
from test_session import _project, _run_child

pytest.importorskip("dbt.adapters.snowflake")


@pytest.mark.parametrize(
    ("sqlstate", "gate", "reason"),
    [
        ("42S02", "healthy", None),
        ("42000", "healthy", None),
        ("22012", "healthy", None),
        ("42000", "closed", "connection_not_open"),
        ("42000", "probe", "recovery_check_failed"),
        ("42000", "cleanup", "request_cleanup_failed"),
    ],
)
def test_snowflake_native_query_error_preserves_handle(tmp_path, sqlstate, gate, reason):
    project, profiles = _project(tmp_path)
    (profiles / "profiles.yml").write_text("""native_session_test:
  target: dev
  outputs:
    dev:
      type: snowflake
      account: offline-test
      user: offline-test
      password: offline-test
      database: offline_test
      schema: offline_test
      threads: 1
""")
    compile_errors = (
        "select * from {{ ref('missing_branch_model') }}",
        "select {{ missing_branch_macro() }}",
        "select {{ broken_jinja",
    )
    completed = _run_child(
        project,
        profiles,
        f"""
import snowflake.connector
from snowflake.connector.errors import ProgrammingError

class Cursor:
    sqlstate = None
    sfqid = "offline-query"
    rowcount = 1
    description = [("VALUE",)]
    def execute(self, sql, bindings=None):
        if "bad_query" in sql:
            if {gate!r} == "closed":
                handles[0].closed = True
            raise ProgrammingError(msg="synthetic SQL error", errno=1003, sqlstate={sqlstate!r})
    def fetchall(self):
        return [(1,)]
    def fetchmany(self, limit):
        return self.fetchall()[:limit]

class Handle:
    closed = False
    def cursor(self):
        assert not self.closed
        return Cursor()
    def is_closed(self):
        return self.closed
    def close(self):
        self.closed = True
    def rollback(self):
        assert not self.closed

handles = []
def connect(**kwargs):
    handle = Handle()
    handles.append(handle)
    return handle
snowflake.connector.connect = connect
session = DbtSession(context)
def check_compile_errors():
    for sql in {compile_errors!r}:
        try:
            session.execute(sql, 2)
        except QueryError:
            pass
        else:
            raise AssertionError("expected recoverable compilation error")
try:
    if {gate!r} == "healthy":
        check_compile_errors()
        assert not handles  # Failed compilation must not authenticate/connect.
    assert session.execute("select 1 as value", 2)["rows"] == [[1]]
    if {gate!r} == "healthy":
        check_compile_errors()
        assert len(handles) == 1 and not handles[0].closed
    def failed_check():
        raise RuntimeError("synthetic failure with private detail")
    if {gate!r} == "probe":
        session._connection_is_open = failed_check
    if {gate!r} == "cleanup":
        session._rollback_open_transaction = failed_check
    try:
        session.execute("select bad_query", 2)
    except QueryError:
        assert {reason!r} is None
    except SessionError as error:
        assert {reason!r} is not None
        assert error.reason == {reason!r}
        assert session._closed
    else:
        raise AssertionError("expected query error")
    if {reason!r} is None:
        assert session.execute("select 1 as value", 2)["rows"] == [[1]]
        assert len(handles) == 1 and not handles[0].closed
finally:
    session.close()
""",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
