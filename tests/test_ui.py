from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from threading import Event

import pytest
from textual.widgets import DataTable

from dbt_enhance import dbt as dbt_backend
from dbt_enhance.models import SSO_STATUS, EnhanceError, QueryRequest, QueryResult
from dbt_enhance.ui import QueryApp


def result(rows: list[list[object]] | None = None) -> QueryResult:
    return QueryResult(
        columns=["id", "name", "note"],
        rows=rows
        if rows is not None
        else [[1, "one", "a very long value " * 20], [2, "two", None]],
        compiled_sql="select *\nfrom example",
        elapsed=0.12,
        project_dir=Path("/tmp/project"),
        limit=50,
        target="dev",
    )


@pytest.mark.asyncio
async def test_success_renders_literal_values_and_detail() -> None:
    query = result([[1, "[not markup] \x1b[31mred", None]])
    app = QueryApp(result=query)
    async with app.run_test(size=(100, 30)) as pilot:
        table = app.query_one("#results", DataTable)
        assert table.row_count == 1
        assert "1 rows" in str(app.query_one("#result-meta").renderable)
        await pilot.press("l", "a")
        assert app.query_one("#detail-panel").display
        assert "�" in app.query_one("#detail").renderable.plain


@pytest.mark.asyncio
async def test_empty_result_has_an_in_app_message() -> None:
    app = QueryApp(result=result([]))
    async with app.run_test() as pilot:
        await pilot.pause()
        message = app.query_one("#message")
        assert message.display
        assert "No rows returned" in message.renderable.plain


@pytest.mark.asyncio
async def test_runner_error_stays_in_popup_with_readable_lines() -> None:
    request = QueryRequest("select 1", Path("model.sql"))

    def failing(*_: object) -> QueryResult:
        raise EnhanceError(
            "connection refused\nCheck credentials: \x1b[31mdenied\n" + ("driver detail\n" * 20)
        )

    app = QueryApp(request, runner=failing)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("tab", "pagedown")
        panel = app.query_one("#message-panel")
        assert "Could not run query" in str(app.query_one("#status").renderable)
        message = app.query_one("#message").renderable.plain
        assert "connection refused\nCheck credentials" in message
        assert "�[31mdenied" in message
        assert app.focused is panel
        assert panel.scroll_y > 0
        assert app._timeout_timer is None
        assert app._elapsed_timer is None


@pytest.mark.asyncio
async def test_sql_open_at_80x24_keeps_at_least_three_data_lines_visible() -> None:
    app = QueryApp(result=result())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("s")
        table = app.query_one("#results", DataTable)
        assert table.content_region.height - table.header_height >= 3


@pytest.mark.asyncio
async def test_long_sql_is_complete_and_retains_literal_layout_glyphs() -> None:
    tail = "-- SQL TAIL: do not lose this"
    sql = "select '↵' as visible_newline, '⇥' as visible_tab\n" + ("-- filler\n" * 1_000) + tail
    query = result()
    query = QueryResult(
        columns=query.columns,
        rows=query.rows,
        compiled_sql=sql,
        elapsed=query.elapsed,
        project_dir=query.project_dir,
        limit=query.limit,
        target=query.target,
    )
    app = QueryApp(result=query)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("s", "tab", "pagedown")
        panel = app.query_one("#sql-panel")
        syntax = app.query_one("#sql").renderable
        assert syntax.code.endswith(tail)
        assert "'↵'" in syntax.code
        assert "'⇥'" in syntax.code
        assert app.focused is panel
        assert panel.scroll_y > 0
        assert panel.styles.overflow_y == "auto"


@pytest.mark.asyncio
async def test_compact_context_hides_long_source_paths_but_keeps_labels() -> None:
    request = QueryRequest(
        "select 1",
        Path("/tmp/very-long-temporary-directory/" + ("source-" * 20) + ".sql"),
    )
    base = result()
    offline = QueryResult(
        columns=base.columns,
        rows=base.rows,
        compiled_sql=base.compiled_sql,
        elapsed=base.elapsed,
        project_dir=base.project_dir,
        limit=base.limit,
        target=None,
    )
    app = QueryApp(request, result=offline)
    async with app.run_test(size=(80, 24)):
        context = app.query_one("#context")
        assert "file  source-" in context.renderable.plain
        assert "/tmp/very-long" not in context.renderable.plain
        assert "target  profile default" in context.renderable.plain
        assert "file: /tmp/very-long" in context.tooltip.plain


@pytest.mark.asyncio
async def test_warnings_and_default_profile_context_are_visible() -> None:
    query = result([])
    query = QueryResult(
        columns=query.columns,
        rows=query.rows,
        compiled_sql=query.compiled_sql,
        elapsed=query.elapsed,
        project_dir=query.project_dir,
        limit=query.limit,
        target=None,
        warnings=["Column metadata was unavailable for this empty result."],
    )
    app = QueryApp(result=query)
    async with app.run_test():
        assert "profile default" in app.query_one("#context").renderable.plain
        warnings = app.query_one("#warnings")
        assert warnings.display
        assert "metadata was unavailable" in warnings.renderable.plain


@pytest.mark.asyncio
async def test_sql_panel_collapses_and_table_navigation_works() -> None:
    app = QueryApp(result=result())
    async with app.run_test(size=(80, 24)) as pilot:
        table = app.query_one("#results", DataTable)
        assert not app.query_one("#sql-panel").display
        await pilot.press("s")
        assert app.query_one("#sql-panel").display
        await pilot.press("s", "j", "l")
        assert table.cursor_coordinate.row == 1
        assert table.cursor_coordinate.column == 1


@pytest.mark.asyncio
async def test_table_scrolls_both_axes_from_keyboard() -> None:
    query = QueryResult(
        columns=[f"column_{column}" for column in range(8)],
        rows=[
            [f"row {row}, column {column}, long value" for column in range(8)] for row in range(40)
        ],
        compiled_sql="select * from wide_result",
        elapsed=0.1,
        project_dir=Path("/tmp/project"),
        limit=500,
    )
    app = QueryApp(result=query)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press(*(["l"] * 6 + ["j"] * 25))
        table = app.query_one("#results", DataTable)
        assert table.cursor_coordinate.column == 6
        assert table.cursor_coordinate.row == 25
        assert table.scroll_x > 0
        assert table.scroll_y > 0


@pytest.mark.asyncio
async def test_ui_timeout_survives_blocked_real_runner_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    source = project / "models" / "model.sql"
    source.parent.mkdir(parents=True)
    source.write_text("select 1", encoding="utf-8")
    (project / "dbt_project.yml").write_text("name: timeout_test\n", encoding="utf-8")
    executable = tmp_path / "fake-dbt"
    executable.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
    executable.chmod(0o700)

    cleanup_entered = Event()
    release_cleanup = Event()
    original_cleanup = dbt_backend._terminate_process_group

    def blocked_cleanup(process: object) -> None:
        cleanup_entered.set()
        release_cleanup.wait(2)
        original_cleanup(process)  # type: ignore[arg-type]

    monkeypatch.setattr(dbt_backend, "_terminate_process_group", blocked_cleanup)
    request = QueryRequest("select 1", source, dbt=executable, timeout=0.1)
    app = QueryApp(request, runner=dbt_backend.run_query)
    started = time.monotonic()

    async with app.run_test() as pilot:
        try:
            while "timed out" not in str(app.query_one("#status").renderable).lower():
                assert time.monotonic() - started < 0.6
                await pilot.pause(0.01)

            assert cleanup_entered.wait(0.2)
            assert not app.query_one("#spinner").display
            assert "timed out" in app.query_one("#message").renderable.plain.lower()
            assert app._cancel_event.is_set()
            assert app._timeout_timer is None
            assert app._elapsed_timer is None
        finally:
            release_cleanup.set()
            await pilot.pause(0.1)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_outcome", ["success", "error"])
async def test_late_worker_callbacks_cannot_replace_timeout(late_outcome: str) -> None:
    request = QueryRequest("select 1", Path("model.sql"), timeout=0.05)
    started = Event()
    release = Event()
    finished = Event()

    def late_runner(_: QueryRequest, __: Event, on_status: object) -> QueryResult:
        started.set()
        release.wait(2)
        try:
            on_status(SSO_STATUS)  # type: ignore[operator]
            if late_outcome == "error":
                raise EnhanceError("late backend error")
            return result()
        finally:
            finished.set()

    app = QueryApp(request, runner=late_runner)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        try:
            assert started.wait(0.2)
            for _ in range(50):
                if "timed out" in str(app.query_one("#status").renderable).lower():
                    break
                await pilot.pause(0.01)
            else:
                pytest.fail("UI watchdog did not display its timeout")

            status = app.query_one("#status").renderable.plain
            message = app.query_one("#message").renderable.plain
            release.set()
            for _ in range(50):
                if finished.is_set():
                    break
                await pilot.pause(0.01)
            await pilot.pause(0.05)

            assert finished.is_set()
            assert app.query_one("#status").renderable.plain == status
            assert app.query_one("#message").renderable.plain == message
            assert "timed out" in message.lower()
            assert "late backend error" not in message
            assert "browser SSO" not in status
        finally:
            release.set()


@pytest.mark.asyncio
async def test_sso_progress_adds_actionable_guidance_to_timeout() -> None:
    request = QueryRequest("select 1", Path("model.sql"), timeout=0.05)
    release = Event()

    def awaiting_sso(_: QueryRequest, __: Event, on_status: object) -> QueryResult:
        on_status(SSO_STATUS)  # type: ignore[operator]
        release.wait(2)
        return result()

    app = QueryApp(request, runner=awaiting_sso)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        try:
            await pilot.pause(0.1)
            message = app.query_one("#message").renderable.plain
            assert "timed out" in message
            assert "Run dbt debug in a terminal" in message
            assert "approved credential caching" in message
        finally:
            release.set()
            await pilot.pause(0.05)


@pytest.mark.asyncio
async def test_fast_success_stops_watchdog_without_spurious_timeout() -> None:
    request = QueryRequest("select 1", Path("model.sql"), timeout=0.05)

    def immediate(*_: object) -> QueryResult:
        return result()

    app = QueryApp(request, runner=immediate)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.pause(0.15)
        assert app.query_one("#status").renderable.plain == "Complete"
        assert not app._cancel_event.is_set()
        assert app._timeout_timer is None
        assert app._elapsed_timer is None


@pytest.mark.asyncio
async def test_pending_query_shows_elapsed_and_configured_timeout() -> None:
    request = QueryRequest("select 1", Path("model.sql"), timeout=0.5)
    release = Event()

    def pending(*_: object) -> QueryResult:
        release.wait(2)
        return result()

    app = QueryApp(request, runner=pending)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        try:
            await pilot.pause(0.15)
            meta = app.query_one("#result-meta").renderable.plain
            assert "elapsed" in meta
            assert "0.5s timeout" in meta
        finally:
            release.set()
            await pilot.pause(0.05)


def test_quit_gives_real_dbt_time_to_kill_a_child_ignoring_sigterm(tmp_path) -> None:
    import os
    import signal

    project = tmp_path / "project"
    project.mkdir()
    (project / "dbt_project.yml").write_text("name: quit_test\n")
    pid_file = tmp_path / "dbt.pid"
    executable = tmp_path / "dbt"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    executable.chmod(0o700)
    program = textwrap.dedent(
        f"""
        import asyncio
        from pathlib import Path
        from dbt_enhance.models import QueryRequest
        from dbt_enhance.ui import QueryApp

        async def main():
            request = QueryRequest('select 1', Path({str(project / "model.sql")!r}),
                                   dbt=Path({str(executable)!r}), timeout=10)
            app = QueryApp(request)
            async with app.run_test() as pilot:
                for _ in range(200):
                    if Path({str(pid_file)!r}).exists():
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError('dbt did not start')
                await pilot.press('q')

        asyncio.run(main())
        """
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, timeout=5
        )
        assert completed.returncode == 0, completed.stderr
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_quit_allows_process_exit_with_noncooperative_runner() -> None:
    program = textwrap.dedent(
        """
        import asyncio
        from pathlib import Path
        from threading import Event

        from dbt_enhance.models import QueryRequest
        from dbt_enhance.ui import QueryApp

        started = Event()
        held_forever = Event()

        def noncooperative(*args):
            started.set()
            held_forever.wait()

        async def main():
            app = QueryApp(
                QueryRequest("select 1", Path("model.sql"), timeout=10),
                runner=noncooperative,
            )
            async with app.run_test() as pilot:
                assert started.wait(0.5)
                await pilot.press("q")

        asyncio.run(main())
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=4,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
async def test_quit_signals_a_pending_runner() -> None:
    request = QueryRequest("select 1", Path("model.sql"))
    started = Event()
    cancelled = Event()

    def pending(_: QueryRequest, cancel_event: Event, __: object) -> QueryResult:
        started.set()
        cancel_event.wait(2)
        if cancel_event.is_set():
            cancelled.set()
        return result()

    app = QueryApp(request, runner=pending)  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        for _ in range(20):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        await pilot.press("q")
    assert cancelled.wait(0.5)
    assert app._timeout_timer is None
    assert app._elapsed_timer is None
