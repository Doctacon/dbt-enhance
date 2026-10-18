from __future__ import annotations

import json
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from dbt_enhance.dbt import discover_project, find_dbt, run_query
from dbt_enhance.models import EnhanceError, QueryRequest

FAKE_DBT = r"""#!{python}
import json
import os
import subprocess
import sys
import time
from pathlib import Path

args = sys.argv[1:]
def option(name):
    return args[args.index(name) + 1]

record = os.environ.get("FAKE_DBT_RECORD")
if record:
    Path(record).write_text(json.dumps(args))
    Path(record + ".env").write_text(json.dumps({{
        key: os.environ.get(key)
        for key in (
            "PYTHONUNBUFFERED", "SNOWFLAKE_DISABLE_PLATFORM_DETECTION",
            "DBT_CLOUD_TEST_CONTEXT", "DBT_POPULATE_CACHE",
        )
    }}))
sql = option("--inline")
if "SSO" in sql:
    print("Initiating login request with your identity provider. "
          "Press CTRL+C to abort and try again...")
    print("Going to open: https://login.example.invalid/?token=DO-NOT-DISPLAY to authenticate...")
    if "EOF" in sql:
        print("We were unable to open a browser window for you, "
              "please open the url above manually then paste the "
              "URL you are redirected to into the terminal.")
        input("Enter the URL the SSO URL redirected you to: ")
    # No explicit flush: the backend must request unbuffered Python output.
    time.sleep(30)
if "SLEEP" in sql:
    time.sleep(30)
if "FAIL" in sql:
    event = {{
        "data": {{"exc": "database exploded"}},
        "info": {{
            "name": "MainEncounteredError",
            "level": "error",
            "msg": "Database Error: database exploded",
        }},
    }}
    print(json.dumps(event))
    sys.exit(2)

target = Path(option("--target-path"))
unique_id = "sql_operation.fake.inline_query"
if "NO_ARTIFACT" not in sql:
    target.mkdir(parents=True, exist_ok=True)
    compiled = "select 1 as id\nlimit " + option("--limit")
    result = {{
        "unique_id": unique_id,
        "status": "success",
        "compiled_code": compiled,
    }}
    (target / "run_results.json").write_text(
        json.dumps({{"results": [result]}})
    )
if "NO_SHOW" not in sql:
    if "EMPTY" in sql:
        rows = []
    elif "WIDE" in sql:
        rows = [{{"wide": "x" * 700_000}}]
    else:
        rows = [{{"first": None, "enabled": True, "label": "plain"}}]
    preview = json.dumps(rows)
    message = preview if "WIDE" in sql else "preview"
    event = {{
        "data": {{"preview": preview, "unique_id": unique_id}},
        "info": {{"name": "ShowNode", "level": "info", "msg": message}},
    }}
    print(json.dumps(event))
if "OVERFLOW" in sql:
    sys.stdout.write("z" * (17 * 1024 * 1024))
if "CHILD" in sql:
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
"""


def _make_project(tmp_path: Path, name: str = "project") -> tuple[Path, Path]:
    project = tmp_path / name
    source = project / "models" / "query.sql"
    source.parent.mkdir(parents=True)
    source.write_text("select 1")
    (project / "dbt_project.yml").write_text("name: fake\n")
    return project, source


def _make_fake_dbt(tmp_path: Path) -> Path:
    executable = tmp_path / "fake dbt"
    executable.write_text(FAKE_DBT.format(python=sys.executable))
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def test_discover_project_uses_nearest_ancestor(tmp_path: Path) -> None:
    outer, _ = _make_project(tmp_path, "outer")
    inner = outer / "packages" / "inner"
    source = inner / "models" / "nested" / "model.sql"
    source.parent.mkdir(parents=True)
    source.write_text("select 1")
    (inner / "dbt_project.yml").write_text("name: inner\n")

    assert discover_project(source) == inner


def test_discover_project_never_searches_cwd_or_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd_project, _ = _make_project(tmp_path, "cwd-project")
    unrelated = tmp_path / "unrelated" / "query.sql"
    unrelated.parent.mkdir()
    unrelated.write_text("select 1")
    monkeypatch.chdir(cwd_project)

    with pytest.raises(EnhanceError, match="No dbt_project.yml"):
        discover_project(unrelated)
    with pytest.raises(EnhanceError, match="absolute"):
        discover_project(Path("models/query.sql"))


def test_find_dbt_prefers_project_then_monorepo(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    project = root / "packages" / "analytics"
    project.mkdir(parents=True)
    root_dbt = root / ".venv" / "bin" / "dbt"
    root_dbt.parent.mkdir(parents=True)
    root_dbt.write_text("#!/bin/sh\n")
    root_dbt.chmod(0o700)

    assert find_dbt(project) == root_dbt

    local_dbt = project / ".venv" / "bin" / "dbt"
    local_dbt.parent.mkdir(parents=True)
    local_dbt.write_text("#!/bin/sh\n")
    local_dbt.chmod(0o700)
    assert find_dbt(project) == local_dbt


def test_find_dbt_uses_path_last_and_reports_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path_dir = tmp_path / "path"
    path_dir.mkdir()
    path_dbt = path_dir / "dbt"
    path_dbt.write_text("#!/bin/sh\n")
    path_dbt.chmod(0o700)
    monkeypatch.setenv("PATH", str(path_dir))
    assert find_dbt(project) == path_dbt

    path_dbt.unlink()
    with pytest.raises(EnhanceError, match="Install dbt"):
        find_dbt(project)


def test_find_dbt_invalid_override_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(EnhanceError, match="configured dbt executable"):
        find_dbt(tmp_path, tmp_path / "missing")


def test_run_query_uses_isolated_cli_and_preserves_json_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, source = _make_project(tmp_path, "project with spaces")
    dbt = _make_fake_dbt(tmp_path)
    record = tmp_path / "arguments.json"
    monkeypatch.setenv("FAKE_DBT_RECORD", str(record))
    monkeypatch.delenv("DBT_POPULATE_CACHE", raising=False)
    statuses: list[str] = []

    result = run_query(
        QueryRequest(
            sql="select 1",
            file=source,
            limit=17,
            target="qa",
            profiles_dir=tmp_path / "profiles with spaces",
            dbt=dbt,
        ),
        on_status=statuses.append,
    )

    assert result.columns == ["first", "enabled", "label"]
    assert result.rows == [[None, True, "plain"]]
    assert result.compiled_sql.endswith("limit 17")
    assert result.project_dir == project
    assert result.limit == 17
    assert result.target == "qa"
    assert result.warnings == []
    assert result.elapsed >= 0
    assert statuses[0] == "Running dbt query"
    assert statuses[-1].startswith("Query completed")

    args = json.loads(record.read_text())
    assert args[:3] == ["show", "--inline", "select 1"]
    child_env = json.loads(Path(str(record) + ".env").read_text())
    assert child_env["DBT_POPULATE_CACHE"] == "false"
    assert "--no-upload-to-artifacts-ingest-api" in args
    assert "--no-send-anonymous-usage-stats" in args
    assert "--write-json" in args
    assert "--no-version-check" not in args
    assert args[args.index("--project-dir") + 1] == str(project)
    assert args[args.index("--target") + 1] == "qa"
    assert args[args.index("--profiles-dir") + 1] == str(tmp_path / "profiles with spaces")
    target_path = Path(args[args.index("--target-path") + 1])
    log_path = Path(args[args.index("--log-path") + 1])
    assert not target_path.exists()
    assert not log_path.exists()


@pytest.mark.parametrize("platform_detection", [None, "false", "true"])
def test_child_environment_preserves_profile_inputs(tmp_path, monkeypatch, platform_detection):
    _, source = _make_project(tmp_path)
    record = tmp_path / "arguments.json"
    monkeypatch.setenv("FAKE_DBT_RECORD", str(record))
    monkeypatch.setenv("DBT_CLOUD_TEST_CONTEXT", "a-profile-might-use-this")
    monkeypatch.setenv("PYTHONUNBUFFERED", "0")
    monkeypatch.setenv("DBT_POPULATE_CACHE", "true")
    if platform_detection is None:
        monkeypatch.delenv("SNOWFLAKE_DISABLE_PLATFORM_DETECTION", raising=False)
    else:
        monkeypatch.setenv("SNOWFLAKE_DISABLE_PLATFORM_DETECTION", platform_detection)

    run_query(QueryRequest(sql="select 1", file=source, dbt=_make_fake_dbt(tmp_path)))

    import os

    child = json.loads(Path(str(record) + ".env").read_text())
    assert child["PYTHONUNBUFFERED"] == "1"
    assert child["SNOWFLAKE_DISABLE_PLATFORM_DETECTION"] == (platform_detection or "true")
    assert child["DBT_CLOUD_TEST_CONTEXT"] == "a-profile-might-use-this"
    assert child["DBT_POPULATE_CACHE"] == "true"
    assert os.environ["PYTHONUNBUFFERED"] == "0"
    assert os.environ.get("SNOWFLAKE_DISABLE_PLATFORM_DETECTION") == platform_detection


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("select 1;", "select 1"),
        ("select 'semi;colon';  \n", "select 'semi;colon'  \n"),
        ("select 1; -- a comment;\n", "select 1 -- a comment;\n"),
        ("select 1; /* trailing; comment */", "select 1 /* trailing; comment */"),
        ("select ';'", "select ';'"),
        ("select {{ 6 * 7 }};", "select {{ 6 * 7 }}"),
        ("select $$semicolon;$$;", "select $$semicolon;$$"),
        ("select 'limit 10' as text", "select 'limit 10' as text"),
        ("select 1 -- limit 10", "select 1 -- limit 10"),
        ("select * from (select 1 limit 2)", "select * from (select 1 limit 2)"),
        ("{% set limit = 10 %}\nselect 1", "{% set limit = 10 %}\nselect 1"),
        (
            "select * from {{ ref('values') }} LiMiT {{ var('rows', 10) }}; -- end",
            "select * from (\nselect * from {{ ref('values') }} "
            "LiMiT {{ var('rows', 10) }} -- end\n) dbt_enhance_preview",
        ),
    ],
)
def test_preview_normalizes_delimiter_and_literal_outer_limit(tmp_path, monkeypatch, sql, expected):
    _, source = _make_project(tmp_path)
    record = tmp_path / "arguments.json"
    monkeypatch.setenv("FAKE_DBT_RECORD", str(record))
    run_query(QueryRequest(sql=sql, file=source, dbt=_make_fake_dbt(tmp_path)))
    args = json.loads(record.read_text())
    assert args[args.index("--inline") + 1] == expected


def test_wide_show_event_larger_than_one_mib_is_not_truncated(tmp_path: Path) -> None:
    _, source = _make_project(tmp_path)

    result = run_query(QueryRequest(sql="WIDE", file=source, dbt=_make_fake_dbt(tmp_path)))

    assert result.columns == ["wide"]
    assert len(result.rows[0][0]) == 700_000


def test_output_overflow_is_actionable_instead_of_parsed_as_success(
    tmp_path: Path,
) -> None:
    _, source = _make_project(tmp_path)

    with pytest.raises(EnhanceError, match="16 MiB.*Lower --limit.*fewer columns"):
        run_query(QueryRequest(sql="OVERFLOW", file=source, dbt=_make_fake_dbt(tmp_path)))


@pytest.mark.parametrize("darwin", [True, False])
def test_exited_group_permission_race_is_only_ignored_on_macos(tmp_path, monkeypatch, darwin):
    import errno

    from dbt_enhance import dbt

    _, source = _make_project(tmp_path)
    monkeypatch.setattr(dbt, "_IS_DARWIN", darwin, raising=False)

    def exited_group(pid, sig):
        raise PermissionError(errno.EPERM, "exited process group")

    monkeypatch.setattr(dbt.os, "killpg", exited_group)
    request = QueryRequest(sql="select 1", file=source, dbt=_make_fake_dbt(tmp_path))
    if darwin:
        assert run_query(request).rows == [[None, True, "plain"]]
    else:
        with pytest.raises(EnhanceError, match="exited process group"):
            run_query(request)


def test_successful_leader_cleans_child_holding_stdout(tmp_path: Path) -> None:
    _, source = _make_project(tmp_path)
    started = time.monotonic()

    result = run_query(QueryRequest(sql="CHILD", file=source, dbt=_make_fake_dbt(tmp_path)))

    assert result.rows == [[None, True, "plain"]]
    assert time.monotonic() - started < 2


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sql": "  \n"}, "SQL must not be empty"),
        ({"limit": 0}, "between 1 and 10000"),
        ({"limit": 10_001}, "between 1 and 10000"),
        ({"timeout": 0}, "positive, finite"),
        ({"timeout": float("inf")}, "positive, finite"),
    ],
)
def test_run_query_validates_before_starting(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    _, source = _make_project(tmp_path)
    values = {"sql": "select 1", "file": source, "dbt": tmp_path / "missing"}
    values.update(changes)
    with pytest.raises(EnhanceError, match=message):
        run_query(QueryRequest(**values))  # type: ignore[arg-type]


def test_run_query_validates_utf8_sql_size_before_starting(tmp_path: Path) -> None:
    _, source = _make_project(tmp_path)
    oversized_multibyte_sql = "é" * (512 * 1024 + 1)

    with pytest.raises(EnhanceError, match="1 MiB.*UTF-8"):
        run_query(
            QueryRequest(
                sql=oversized_multibyte_sql,
                file=source,
                dbt=tmp_path / "missing",
            )
        )


def test_empty_preview_has_explicit_column_limitation(tmp_path: Path) -> None:
    _, source = _make_project(tmp_path)
    result = run_query(QueryRequest(sql="EMPTY", file=source, dbt=_make_fake_dbt(tmp_path)))
    assert result.columns == []
    assert result.rows == []
    assert "does not include column metadata" in result.warnings[0]


def test_dbt_failure_is_structured_and_actionable(tmp_path: Path) -> None:
    _, source = _make_project(tmp_path)
    with pytest.raises(EnhanceError, match="Database Error: database exploded"):
        run_query(QueryRequest(sql="FAIL", file=source, dbt=_make_fake_dbt(tmp_path)))


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("NO_SHOW", "did not emit a ShowNode"),
        ("NO_ARTIFACT", "did not write the compiled run_results.json"),
    ],
)
def test_success_without_required_dbt_output_is_an_error(
    tmp_path: Path, sql: str, message: str
) -> None:
    _, source = _make_project(tmp_path)
    with pytest.raises(EnhanceError, match=message):
        run_query(QueryRequest(sql=sql, file=source, dbt=_make_fake_dbt(tmp_path)))


def test_cancellation_returns_quickly(tmp_path: Path) -> None:
    _, source = _make_project(tmp_path)
    cancel = threading.Event()
    timer = threading.Timer(0.15, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(EnhanceError, match="cancelled"):
            run_query(
                QueryRequest(sql="SLEEP", file=source, dbt=_make_fake_dbt(tmp_path), timeout=20),
                cancel=cancel,
            )
    finally:
        timer.cancel()
    assert time.monotonic() - started < 3


def test_sso_progress_arrives_before_timeout_without_exposing_login_url(tmp_path: Path) -> None:
    _, source = _make_project(tmp_path)
    statuses: list[str] = []
    cancel = threading.Event()

    def progress(message: str) -> None:
        statuses.append(message)
        if "SSO" in message:
            cancel.set()

    with pytest.raises(EnhanceError, match="cancelled"):
        run_query(
            QueryRequest(sql="SSO", file=source, dbt=_make_fake_dbt(tmp_path), timeout=1),
            cancel=cancel,
            on_status=progress,
        )
    assert any("SSO" in message for message in statuses)
    assert all(
        "DO-NOT-DISPLAY" not in message and "https://" not in message for message in statuses
    )


def test_sso_eof_explains_terminal_login_and_does_not_show_auth_url(tmp_path: Path) -> None:
    _, source = _make_project(tmp_path)
    with pytest.raises(EnhanceError) as error:
        run_query(QueryRequest(sql="SSO EOF", file=source, dbt=_make_fake_dbt(tmp_path)))
    message = str(error.value)
    assert "dbt debug" in message
    assert "terminal" in message
    assert "SSO" in message
    assert "DO-NOT-DISPLAY" not in message
    assert "https://" not in message


def test_sso_timeout_includes_login_guidance(tmp_path: Path) -> None:
    _, source = _make_project(tmp_path)
    with pytest.raises(EnhanceError) as error:
        run_query(QueryRequest(sql="SSO", file=source, dbt=_make_fake_dbt(tmp_path), timeout=0.5))
    assert "timed out" in str(error.value)
    assert "dbt debug" in str(error.value)
    assert "DO-NOT-DISPLAY" not in str(error.value)


def test_timeout_is_not_masked_by_cleanup_permission_error(tmp_path: Path, monkeypatch) -> None:
    from dbt_enhance import dbt

    _, source = _make_project(tmp_path)
    signal_group = dbt._signal_group
    processes = []

    def denied(process, sig):
        processes.append(process)
        raise PermissionError("cleanup permission denied")

    monkeypatch.setattr(dbt, "_signal_group", denied)
    try:
        with pytest.raises(EnhanceError, match="timed out"):
            run_query(
                QueryRequest(sql="SLEEP", file=source, dbt=_make_fake_dbt(tmp_path), timeout=0.1)
            )
    finally:
        for process in processes:
            signal_group(process, dbt.signal.SIGKILL)
            process.wait(timeout=2)


def test_timeout_survives_a_process_that_cannot_be_reaped(tmp_path: Path, monkeypatch) -> None:
    """Model a stuck WSL child: no cleanup wait may be unbounded or mask timeout."""
    from dbt_enhance import dbt

    _, source = _make_project(tmp_path)
    original_wait = subprocess.Popen.wait
    processes = []

    def stuck_wait(process, timeout=None):
        processes.append(process)
        assert timeout is not None, "cleanup attempted an unbounded wait"
        raise subprocess.TimeoutExpired(process.args, timeout)

    monkeypatch.setattr(dbt.subprocess.Popen, "wait", stuck_wait)
    try:
        with pytest.raises(EnhanceError, match="timed out"):
            run_query(
                QueryRequest(sql="SLEEP", file=source, dbt=_make_fake_dbt(tmp_path), timeout=0.1)
            )
    finally:
        # The real fixture process is killable; reap it outside the simulated OS failure.
        for process in processes:
            original_wait(process, timeout=2)


def test_timeout_returns_quickly(tmp_path: Path) -> None:
    _, source = _make_project(tmp_path)
    started = time.monotonic()
    with pytest.raises(EnhanceError, match="timed out"):
        run_query(QueryRequest(sql="SLEEP", file=source, dbt=_make_fake_dbt(tmp_path), timeout=0.1))
    assert time.monotonic() - started < 3
