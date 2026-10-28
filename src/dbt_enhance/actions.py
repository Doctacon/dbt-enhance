"""Small searchable vocabulary for the viewer's fixed keyboard actions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ViewerAction:
    id: str
    label: str
    shortcut: str
    keywords: tuple[str, ...] = ()


ACTIONS: tuple[ViewerAction, ...] = (
    ViewerAction("search", "Find in focused view", "/", ("find", "next", "literal")),
    ViewerAction("reset_view", "Reset preview transforms", "r", ("filter", "sort", "columns")),
    ViewerAction(
        "column_chooser",
        "Choose columns, sizing, and pins",
        "",
        ("hide", "restore", "width", "freeze"),
    ),
    ViewerAction(
        "filter_builder", "Build local AND filters", "", ("equals", "contains", "remove", "clear")
    ),
    ViewerAction("sort_ascending", "Sort selected column ascending", "", ("sort", "table")),
    ViewerAction("sort_descending", "Sort selected column descending", "", ("sort", "table")),
    ViewerAction("clear_sort", "Clear local sort", "", ("sort", "restore")),
    ViewerAction("hide_selected", "Hide selected column", "", ("column", "restore")),
    ViewerAction("pin_selected", "Pin through selected column", "", ("freeze", "prefix")),
    ViewerAction("toggle_sql", "Show compiled SQL", "s", ("statement", "query")),
    ViewerAction("toggle_detail", "Show full cell detail", "a", ("inspect", "value")),
    ViewerAction("copy_cell", "Copy selected cell", "y", ("clipboard",)),
    ViewerAction("copy_sql", "Copy compiled SQL", "Y", ("clipboard",)),
    ViewerAction("reset_worker", "Reset worker / disconnect", "", ("session", "recovery")),
    ViewerAction("native_error", "Show local native error detail", "", ("failure", "recovery")),
    ViewerAction("support_report", "Review/copy support report", "", ("diagnostics", "privacy")),
    ViewerAction("quit", "Close preview", "q / Escape", ("close",)),
)


def search_actions(literal: str) -> tuple[ViewerAction, ...]:
    """Case-insensitive literal action matching for a compact command menu."""
    needle = literal.casefold().strip()
    if not needle:
        return ACTIONS
    return tuple(
        action
        for action in ACTIONS
        if needle
        in " ".join((action.id, action.label, action.shortcut, *action.keywords)).casefold()
    )
