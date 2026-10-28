from pathlib import Path

import pytest
from textual.widgets import DataTable

from dbt_enhance.models import QueryResult
from dbt_enhance.ui import QueryApp


def wide_result():
    return QueryResult(
        ["first", "second", "tail"],
        [["x" * 30, "y" * 30, "TAIL"]],
        "select 1",
        0.1,
        Path("."),
        10,
        column_types=["text"] * 3,
    )


@pytest.mark.asyncio
async def test_oversized_pin_is_refused_without_destroying_an_existing_valid_pin():
    app = QueryApp(result=wide_result())
    async with app.run_test(size=(50, 24)) as pilot:
        table = app.query_one("#results", DataTable)
        await pilot.press("ctrl+p", *"pin through", "enter")
        assert table.fixed_columns == 1
        table.move_cursor(column=1)
        await pilot.press("ctrl+p", *"pin through", "enter")
        assert table.fixed_columns == 1
        table.move_cursor(column=2)
        await pilot.pause(0.15)
        visible = "".join(table.render_line(y).text for y in range(table.size.height))
        assert "TAIL" in visible


@pytest.mark.asyncio
async def test_shrinking_terminal_unpins_an_unusable_prefix():
    app = QueryApp(result=wide_result())
    async with app.run_test(size=(80, 24)) as pilot:
        table = app.query_one("#results", DataTable)
        table.move_cursor(column=1)
        await pilot.press("ctrl+p", *"pin through", "enter")
        assert table.fixed_columns == 2
        await pilot.resize_terminal(50, 24)
        await pilot.pause()
        assert table.fixed_columns == 0
        table.move_cursor(column=2)
        await pilot.pause(0.15)
        visible = "".join(table.render_line(y).text for y in range(table.size.height))
        assert "TAIL" in visible
