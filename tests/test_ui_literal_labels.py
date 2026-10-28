from pathlib import Path

import pytest
from textual.widgets import Static

from dbt_enhance.models import QueryResult
from dbt_enhance.preview import ColumnFilter
from dbt_enhance.ui import QueryApp


@pytest.mark.asyncio
async def test_column_and_filter_dialogs_render_literal_safe_names_and_values():
    name = "[red]name[/red]\x1b[2J"
    value = "[bold]value[/bold]"
    result = QueryResult([name], [[value]], "select 1", 0.1, Path("."), 10, column_types=["text"])
    app = QueryApp(result=result)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("ctrl+p", *"choose columns", "enter")
        rendered = app.screen.query_one("#column-results", Static).render_line(0).text
        assert "[red]name[/red]" in rendered
        assert "\x1b" not in rendered
        await pilot.press("escape")
        assert app.apply_filter(ColumnFilter(0, "equals", value))
        await pilot.press("ctrl+p", *"build local", "enter")
        widget = app.screen.query_one("#filter-list", Static)
        rendered = "".join(widget.render_line(y).text for y in range(widget.size.height))
        assert value in rendered
        assert "\x1b" not in rendered
