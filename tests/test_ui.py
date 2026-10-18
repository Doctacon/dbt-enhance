from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event

import pytest
from textual.widgets import DataTable

from dbt_enhance.models import EnhanceError, QueryRequest, QueryResult
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
