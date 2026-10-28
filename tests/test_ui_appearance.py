from pathlib import Path

import pytest
from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from dbt_enhance.models import QueryRequest, QueryResult
from dbt_enhance.ui import QueryApp


def luminance(rgb):
    values = [component / 255 for component in rgb]
    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in values]
    return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_requested_theme_is_real_and_context_text_has_readable_contrast(theme):
    request = QueryRequest("select 1", Path("model.sql"), theme=theme)
    result = QueryResult(["value"], [[1]], "select 1", 0.1, Path("."), 10)
    app = QueryApp(request=request, result=result)
    async with app.run_test(size=(80, 24)):
        assert app.current_theme.dark is (theme == "dark")
        background = app.query_one("#shell").styles.background
        background_luminance = luminance((background.r, background.g, background.b))
        assert (background_luminance > 0.5) is (theme == "light")
        for identity in ("context", "run-context", "status", "result-meta", "help"):
            widget = app.query_one(f"#{identity}", Static)
            effective = widget.rich_style
            if isinstance(widget.renderable, Text):
                overlay = widget.renderable.style
                effective += Style.parse(overlay) if isinstance(overlay, str) else overlay
            assert effective.color is not None
            foreground_luminance = luminance(effective.color.get_truecolor())
            contrast = (max(foreground_luminance, background_luminance) + 0.05) / (
                min(foreground_luminance, background_luminance) + 0.05
            )
            assert contrast >= 4.5, (identity, theme, contrast)
