from pathlib import Path

import pytest
from textual.widgets import Static

from dbt_enhance.models import QueryResult
from dbt_enhance.ui import QueryApp


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [50, 80])
async def test_actions_can_be_browsed_to_recovery_controls_without_guessing_words(width):
    app = QueryApp(result=QueryResult(["value"], [[1]], "select 1", 0.1, Path("."), 10))
    async with app.run_test(size=(width, 24)) as pilot:
        help_text = app.query_one("#help", Static).render_line(0).text
        assert "Ctrl+P" in help_text and "q close" in help_text
        await pilot.press("ctrl+p", "tab", "end", "up")
        await pilot.pause()
        choices = app.screen.query_one("#action-results")
        assert choices.scroll_y > 0
        footer = app.screen.query_one("#choice-copy")
        dialog = app.screen.query_one("#backend-choice-dialog")
        assert footer.region.bottom <= dialog.content_region.bottom
        await pilot.press("enter")
        assert "Metadata report" in app.screen.query_one("#review-title", Static).renderable.plain
