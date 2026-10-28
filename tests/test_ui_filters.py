from pathlib import Path

import pytest
from textual.widgets import DataTable, Input

from dbt_enhance.models import QueryResult
from dbt_enhance.ui import QueryApp
from dbt_enhance.ui_dialogs import FilterDialog


async def open_filters(pilot):
    await pilot.press("ctrl+p", *"build local", "enter")


@pytest.mark.asyncio
async def test_boolean_and_native_serialized_date_filters_work_from_keyboard():
    result = QueryResult(
        ["active", "when"],
        [[True, "2025-02-02"], [False, "2025-01-01"], [True, None]],
        "select 1",
        0.1,
        Path("."),
        10,
        column_types=["boolean", "date"],
    )
    app = QueryApp(result=result)
    async with app.run_test(size=(80, 24)) as pilot:
        await open_filters(pilot)
        await pilot.press("1", "tab", *"equals", "tab", *"TRUE", "tab", "enter")
        assert app.screen_stack[0].query_one("#results", DataTable).row_count == 2
        await open_filters(pilot)
        await pilot.press("2", "tab", ">", "tab", *"2025-01-01", "tab", "enter")
        assert app.screen_stack[0].query_one("#results", DataTable).row_count == 1
        await open_filters(pilot)
        await pilot.press(*(["tab"] * 5), "enter")
        assert app.screen_stack[0].query_one("#results", DataTable).row_count == 3


@pytest.mark.asyncio
async def test_bad_numeric_filter_is_correctable_and_scientific_notation_is_supported():
    result = QueryResult(
        ["value"], [[1], [2]], "select 1", 0.1, Path("."), 10, column_types=["number"]
    )
    app = QueryApp(result=result)
    async with app.run_test(size=(80, 24)) as pilot:
        await open_filters(pilot)
        await pilot.press("1", "tab", ">", "tab", *"oops", "tab", "enter")
        assert isinstance(app.screen, FilterDialog)
        assert app.screen_stack[0].query_one("#results", DataTable).row_count == 2
        app.screen.query_one("#filter-value", Input).focus()
        await pilot.press("end", "ctrl+u", *"1e0", "tab", "enter")
        assert not isinstance(app.screen, FilterDialog)
        assert app.screen_stack[0].query_one("#results", DataTable).row_count == 1
