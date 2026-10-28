"""Viewer palettes: the same hierarchy with readable light and dark colors."""

from collections.abc import Mapping

from textual.theme import Theme

DARK = Theme(
    name="dbt-enhance-dark",
    dark=True,
    primary="#8fd3ca",
    secondary="#aabbb8",
    accent="#79c5bc",
    foreground="#d8e2e0",
    background="#101817",
    surface="#142120",
    panel="#1b302e",
    warning="#d7a65a",
    success="#79c5bc",
    error="#ec8b88",
    variables={"quiet": "#718783"},
)
LIGHT = Theme(
    name="dbt-enhance-light",
    dark=False,
    primary="#146f64",
    secondary="#49645f",
    accent="#16705f",
    foreground="#1b2927",
    background="#f5f7f6",
    surface="#ffffff",
    panel="#dcebe8",
    warning="#855600",
    success="#16705f",
    error="#a72f2f",
    variables={"quiet": "#506c66"},
)


def preferred_theme(preference: str, environment: Mapping[str, str]) -> str:
    if preference == "light":
        return LIGHT.name
    if preference == "auto":
        # A conventional terminal hint, not an assumption about the OS theme.
        # Terminals without this hint use the dark palette; --theme is definitive.
        try:
            background = int(environment.get("COLORFGBG", "").rsplit(";", 1)[-1])
        except ValueError:
            background = -1
        if 7 <= background <= 15:
            return LIGHT.name
    return DARK.name
