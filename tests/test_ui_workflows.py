from pathlib import Path

import pytest
from textual.widgets import DataTable

from dbt_enhance.models import QueryResult
from dbt_enhance.preview import ColumnFilter
from dbt_enhance.ui import QueryApp


def result() -> QueryResult:
    return QueryResult(
        ["id", "name"],
        [[2, "Beta"], [1, "alpha"]],
        "select * from example",
        0.1,
        Path("."),
        50,
        column_types=["number", "text"],
    )


@pytest.mark.asyncio
async def test_keyboard_column_chooser_hides_and_restores_with_source_copy_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dbt_enhance.ui.platform", "linux")
    query = QueryResult(
        ["dup", "dup", "name"],
        [["first", "second", "alpha"]],
        "select duplicate",
        0,
        Path("."),
        10,
        column_types=["text", "text", "text"],
    )
    app = QueryApp(result=query)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("ctrl+p")
        await pilot.press(*"choose columns", "enter")
        await pilot.pause()
        await pilot.press(*"dup", "tab", "tab", "enter")
        await pilot.pause()
        table = app.query_one("#results", DataTable)
        assert len(table.columns) == 2
        table.focus()
        await pilot.press("y")
        assert app.clipboard == "second"
        await pilot.press("ctrl+p")
        await pilot.press(*"choose columns", "enter")
        await pilot.pause()
        await pilot.press("tab", "tab", "tab", "tab", "enter")
        await pilot.pause()
        assert len(table.columns) == 3


@pytest.mark.asyncio
async def test_column_ordinal_selection_does_not_replace_name_search():
    query = QueryResult(
        ["dup", "dup", "name"],
        [["first", "second", "alpha"]],
        "select 1",
        0,
        Path("."),
        10,
        column_types=["text"] * 3,
    )
    app = QueryApp(result=query)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("ctrl+p", *"choose columns", "enter")
        await pilot.press(*"dup", "tab", "2", "tab", "enter")
        assert app._visible_column_ids == [0, 2]


@pytest.mark.asyncio
async def test_pinning_freezes_real_table_columns_and_unpin_is_keyboard_reachable():
    app = QueryApp(result=result())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("ctrl+p", *"pin through", "enter")
        table = app.query_one("#results", DataTable)
        assert table.fixed_columns == 1
        await pilot.press("ctrl+p", *"choose columns", "enter")
        await pilot.press(*(["tab"] * 9), "enter")
        assert table.fixed_columns == 0
        assert app._preview._pin_end is None


@pytest.mark.asyncio
async def test_menu_sort_and_filter_dialog_apply_local_transforms() -> None:
    app = QueryApp(result=result())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("ctrl+p")
        await pilot.press(*"descending", "enter")
        assert app._preview is not None and app._preview._sort == (0, "desc")
        await pilot.press("ctrl+p")
        await pilot.press(*"clear local sort", "enter")
        assert app._preview._sort is None
        await pilot.press("ctrl+p")
        await pilot.press(*"build local", "enter")
        await pilot.pause()
        await pilot.press("2", "tab", *"contains", "tab", *"alp", "tab", "enter")
        assert app.query_one("#results", DataTable).row_count == 1


@pytest.mark.asyncio
async def test_action_menu_search_executes_sql_and_preview_controls_keep_copy_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dbt_enhance.ui.platform", "linux")
    monkeypatch.delenv("TMUX", raising=False)
    app = QueryApp(result=result())
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await pilot.press("s", "q", "l", "enter")
        assert app.query_one("#sql-panel").display
        assert app.apply_filter(ColumnFilter("name", "contains", "alp"))
        table = app.query_one("#results", DataTable)
        assert table.row_count == 1
        table.focus()
        await pilot.press("y")
        assert app.clipboard == "1"
        app.action_reset_view()
        assert app.query_one("#results", DataTable).row_count == 2
