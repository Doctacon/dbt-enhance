from pathlib import Path

import pytest
from textual.widgets import Input, Static

from dbt_enhance.models import QueryResult
from dbt_enhance.ui import QueryApp, ScrollPanel


@pytest.mark.asyncio
async def test_focused_full_sql_find_navigates_and_scrolls_in_both_directions():
    sql = "-- filler\n" * 40 + "select 'Target'\n" + "-- filler\n" * 40 + "-- target at tail"
    query = QueryResult(["value"], [[1]], sql, 0.1, Path("."), 10)
    app = QueryApp(result=query)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("s", "tab", "/")
        await pilot.press(*"target")
        assert app.screen.query_one(Input).value == "target"
        await pilot.press("enter")
        await pilot.pause()
        panel = app.query_one("#sql-panel", ScrollPanel)
        first_scroll = panel.scroll_y
        assert first_scroll > 0, "find must bring the matching SQL into view"
        assert app._text_search.span[0] == sql.index("Target")
        await pilot.press("n")
        await pilot.pause()
        assert app._text_search.span[0] == sql.index("target")
        assert panel.scroll_y > first_scroll
        await pilot.press("N")
        await pilot.pause()
        assert app._text_search.span[0] == sql.index("Target")
        assert panel.scroll_y == first_scroll


@pytest.mark.asyncio
async def test_full_cell_find_is_focused_literal_and_does_not_change_copied_value(monkeypatch):
    monkeypatch.setattr("dbt_enhance.ui.platform", "linux")
    monkeypatch.delenv("TMUX", raising=False)
    value = "a" * 9000 + "[Target]" + "b" * 500 + "[target]"
    query = QueryResult(["value"], [[value]], "select 1", 0.1, Path("."), 10)
    app = QueryApp(result=query)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("a", "tab", "/", *"[target]", "enter")
        await pilot.pause()
        panel = app.query_one("#detail-panel", ScrollPanel)
        assert app._text_search.span == (9000, 9008)
        assert panel.scroll_y > 0
        await pilot.press("n")
        assert app._text_search.span[0] == value.index("[target]")
        await pilot.press("y")
        assert app.clipboard == value


@pytest.mark.asyncio
async def test_cell_search_reveals_a_match_moved_to_the_next_line_by_word_wrapping():
    value = "a" * 65 + " LONGTARGET"
    app = QueryApp(result=QueryResult(["value"], [[value]], "select 1", 0.1, Path("."), 10))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("a", "tab", "/", *"target", "enter")
        await pilot.pause()
        panel = app.query_one("#detail-panel", ScrollPanel)
        body = app.query_one("#detail", Static)
        visible = "".join(
            body.render_line(y).text
            for y in range(
                int(panel.scroll_y),
                int(panel.scroll_y) + panel.scrollable_content_region.height,
            )
        )
        assert "TARGET" in visible


@pytest.mark.asyncio
async def test_sql_find_highlights_the_exact_match_after_expanded_tabs():
    sql = "select\t1;\t-- TARGET"
    app = QueryApp(result=QueryResult(["value"], [[1]], sql, 0.1, Path("."), 10))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("s", "tab", "/", *"target", "enter")
        await pilot.pause()
        body = app.query_one("#sql", Static)
        highlighted = "".join(
            segment.text
            for y in range(body.size.height)
            for segment in body.render_line(y)
            if segment.style and segment.style.reverse
        )
        assert highlighted == "TARGET"
