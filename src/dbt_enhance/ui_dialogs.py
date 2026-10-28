"""Small keyboard-operable dialogs used by the terminal viewer."""

from __future__ import annotations

import math
from datetime import date, datetime, time
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from .actions import search_actions
from .preview import ColumnFilter, PreviewModel
from .ui_text import clean_inline
from .ui_widgets import ScrollPanel


class ActionDialog(ModalScreen[str | None]):
    """Search commands, or browse every action with the standard list keys."""

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Actions", id="choice-title"),
            Input(placeholder="Search actions", id="action-search"),
            OptionList(id="action-results"),
            Static("↓/Tab browse · Enter runs · Escape closes", id="choice-copy"),
            id="backend-choice-dialog",
        )

    def on_mount(self) -> None:
        self.query_one(Input).focus()
        self._show("")

    def _show(self, text: str) -> None:
        actions = search_actions(text)
        choices = self.query_one("#action-results", OptionList)
        choices.clear_options()
        choices.add_options(
            [Option(Text(f"{a.shortcut:8} {a.label}"), id=a.id) for a in actions]
            or [Option("No matching actions", disabled=True)]
        )
        choices.highlighted = 0 if actions else None

    def on_input_changed(self, event: Input.Changed) -> None:
        self._show(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        matches = search_actions(event.value)
        if matches:
            self.dismiss(matches[0].id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape":
            event.stop()
            self.dismiss(None)
        elif event.key in {"down", "up"} and isinstance(self.focused, Input):
            event.stop()
            choices = self.query_one("#action-results", OptionList)
            choices.focus()
            if event.key == "up":
                choices.action_last()


class ColumnDialog(ModalScreen[tuple[str, int | None] | None]):
    """Searchable ordinal column chooser; ordinal identity makes duplicates safe."""

    def __init__(self, preview: PreviewModel) -> None:
        super().__init__()
        self.preview = preview
        self.matches: list[int] = []

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Columns", id="choice-title"),
            Input(placeholder="Search column name (literal)", id="column-search"),
            Input(
                placeholder="Selected ordinal (optional; first match by default)",
                id="column-ordinal",
                max_length=8,
            ),
            Static("", id="column-results"),
            Horizontal(
                Button("Hide", id="column-hide"),
                Button("Restore", id="column-restore"),
                Button("Restore all", id="column-restore-all"),
            ),
            Horizontal(
                Button("Auto size", id="column-auto"),
                Button("Width −", id="column-minus"),
                Button("Width +", id="column-plus"),
            ),
            Horizontal(Button("Pin prefix", id="column-pin"), Button("Unpin", id="column-unpin")),
            Static(
                "Search then Tab to a control · first matching ordinal is used · Escape closes",
                id="choice-copy",
            ),
            id="backend-choice-dialog",
        )

    def on_mount(self) -> None:
        self.query_one(Input).focus()
        self._show("")

    def _show(self, text: str) -> None:
        needle = text.casefold()
        self.matches = [i for i, n in enumerate(self.preview.columns) if needle in n.casefold()]
        hidden = self.preview._hidden
        lines = [
            f"{i + 1}. {clean_inline(self.preview.columns[i])}{' (hidden)' if i in hidden else ''}"
            for i in self.matches
        ]
        self.query_one("#column-results", Static).update(
            Text("\n".join(lines) or "No matching columns")
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "column-search":
            self._show(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        name = event.button.id.removeprefix("column-") if event.button.id else ""
        if name in {"restore-all", "unpin"}:
            self.dismiss((name, None))
            return
        raw = self.query_one("#column-ordinal", Input).value.strip()
        try:
            selected = int(raw) - 1 if raw else self.matches[0]
        except (ValueError, IndexError):
            self.notify("Choose a displayed column ordinal.", severity="warning")
            return
        if selected not in self.matches:
            self.notify("Selected ordinal is not in the search results.", severity="warning")
            return
        self.dismiss((name, selected))

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape":
            event.stop()
            self.dismiss(None)


class FilterDialog(ModalScreen[tuple[str, object] | None]):
    """Explicit local AND-filter editor; values are parsed by returned metadata."""

    def __init__(self, preview: PreviewModel) -> None:
        super().__init__()
        self.preview = preview

    def compose(self) -> ComposeResult:
        columns = " · ".join(
            f"{i + 1}:{name} ({self.preview.column_types[i]})"
            for i, name in enumerate(self.preview.columns)
        )
        yield Vertical(
            Static("Filters (AND combined)", id="choice-title"),
            Static(columns, id="filter-columns"),
            Input(placeholder="Column ordinal, e.g. 1", id="filter-column", max_length=8),
            Input(
                placeholder="Operator: equals, contains, <, <=, >, >=, is null",
                id="filter-operator",
            ),
            Input(placeholder="Value (not needed for NULL predicates)", id="filter-value"),
            Horizontal(
                Button("Add filter", id="filter-add"),
                Button("Remove #", id="filter-remove"),
                Button("Clear filters", id="filter-clear"),
            ),
            Static("", id="filter-list"),
            Static(
                "Use column field as filter number when removing · Escape closes", id="choice-copy"
            ),
            id="backend-choice-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#filter-column", Input).focus()
        self._show()

    def _show(self) -> None:
        items = [
            f"{i + 1}. {clean_inline(f.column)} {clean_inline(f.operator)} {clean_inline(f.value)}"
            for i, f in enumerate(self.preview._filters)
        ]
        self.query_one("#filter-list", Static).update(Text("\n".join(items) or "No filters"))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "filter-column":
            return
        try:
            index = int(event.value) - 1
            if not 0 <= index < len(self.preview.column_types):
                return
            kind = self.preview.column_types[index]
        except (ValueError, IndexError):
            return
        choices = {
            "text": "equals, contains, is null, is not null",
            "unknown": "equals, contains (literal), is null, is not null",
            "number": "equals, <, <=, >, >=, is null, is not null",
            "date": "equals, <, <=, >, >=, is null, is not null",
            "datetime": "equals, <, <=, >, >=, is null, is not null",
            "time": "equals, <, <=, >, >=, is null, is not null",
            "boolean": "equals, is null, is not null",
        }[kind]
        self.query_one("#filter-operator", Input).placeholder = f"Operator: {choices}"

    def _payload(self) -> ColumnFilter:
        raw = self.query_one("#filter-column", Input).value.strip()
        try:
            column = int(raw) - 1
        except ValueError:
            raise ValueError("Choose a column ordinal.") from None
        if not 0 <= column < len(self.preview.columns):
            raise ValueError("Choose a listed column ordinal.")
        op = self.query_one("#filter-operator", Input).value.strip().lower()
        raw_value = self.query_one("#filter-value", Input).value
        kind = self.preview.column_types[column]
        if op in {"is null", "is not null"}:
            value = None
        elif kind == "number":
            try:
                value = int(raw_value)
            except ValueError:
                try:
                    value = float(raw_value)
                except ValueError:
                    raise ValueError("Enter a numeric value.") from None
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Enter a finite numeric value.")
        elif kind == "boolean":
            if raw_value.strip().casefold() not in {"true", "false"}:
                raise ValueError("Enter true or false, or use a NULL predicate.")
            value = raw_value.strip().casefold() == "true"
        elif kind == "date":
            try:
                value = date.fromisoformat(raw_value)
            except ValueError:
                raise ValueError("Enter an ISO date (YYYY-MM-DD).") from None
        elif kind == "datetime":
            try:
                value = datetime.fromisoformat(raw_value)
            except ValueError:
                raise ValueError("Enter an ISO date-time.") from None
        elif kind == "time":
            try:
                value = time.fromisoformat(raw_value)
            except ValueError:
                raise ValueError("Enter an ISO time.") from None
        else:
            value = raw_value
        return ColumnFilter(column, op, value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "filter-add":
            try:
                payload = self._payload()
                self.preview._validate_filter(payload)
                self.dismiss(("add", payload))
            except ValueError as error:
                self.notify(str(error), severity="warning")
        elif event.button.id == "filter-clear":
            self.dismiss(("clear", None))
        elif event.button.id == "filter-remove":
            try:
                self.dismiss(("remove", int(self.query_one("#filter-column", Input).value) - 1))
            except ValueError:
                self.notify("Enter a filter number to remove.", severity="warning")

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape":
            event.stop()
            self.dismiss(None)


class TableSearchDialog(ModalScreen[tuple[str, str, bool] | None]):
    def __init__(self, scope: str = "table") -> None:
        super().__init__()
        self.scope = scope

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(f"Find in {self.scope} (full source)", id="choice-title"),
            Input(placeholder="Literal text (case-insensitive)", id="table-search-input"),
            Static(
                "Enter next · Shift+Enter previous · Escape cancels · n/N repeats in the view",
                id="choice-copy",
            ),
            id="backend-choice-dialog",
        )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss((self.scope, event.value, False) if event.value else None)

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape":
            event.stop()
            self.dismiss(None)
        elif event.key == "shift+enter":
            self.dismiss((self.scope, self.query_one(Input).value, True))


class TextReviewDialog(ModalScreen[bool]):
    """A local, literal review surface; copying requires an explicit action."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close_review", show=False),
        Binding("y,Y", "copy_review", show=False),
    ]

    def __init__(self, title: str, body: str, *, allow_copy: bool = False) -> None:
        super().__init__()
        self.title_text, self.body = title, body
        self.allow_copy = allow_copy

    def compose(self) -> ComposeResult:
        controls = [Button("Close", id="review-close")]
        if self.allow_copy:
            controls.append(Button("Copy report", id="review-copy"))
        yield Vertical(
            Static(Text(self.title_text), id="review-title"),
            Static(
                "SQL, values, paths and native errors are excluded. y copies this report."
                if self.allow_copy
                else "Native error text stays local. Escape closes this view.",
                id="review-help",
            ),
            ScrollPanel(Static(Text(self.body), id="review-body"), id="review-scroll"),
            Horizontal(*controls, id="review-controls"),
            id="review-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#review-scroll", ScrollPanel).focus()

    def action_close_review(self) -> None:
        self.dismiss(False)

    def action_copy_review(self) -> None:
        if self.allow_copy:
            self.dismiss(True)
        else:
            self.notify("Native errors stay local. Use the metadata support report for sharing.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.allow_copy and event.button.id == "review-copy")


class WorkerResetDialog(ModalScreen[bool]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel_reset", show=False),
        Binding("y,Y", "no_copy", show=False),
    ]

    def __init__(self, project: str, *, active: bool) -> None:
        super().__init__()
        self.project, self.active = project, active

    def compose(self) -> ComposeResult:
        state = (
            "The worker is active or starting. Confirming may interrupt active and queued work. "
            "Remote cancellation cannot be confirmed."
            if self.active
            else "The worker is idle. If it becomes active, reset will refuse to interrupt it."
        )
        yield Vertical(
            Static("Reset project worker?", id="review-title"),
            Static(
                "Cancel is the default. Shift+Tab reviews scope. No query is rerun.",
                id="review-help",
            ),
            ScrollPanel(
                Static(
                    Text(
                        f"Project: {self.project}\n\n{state}\n\n"
                        "This affects the shared project worker, "
                        "not limited to this preview or target. "
                        "Settings and the fetched result remain unchanged."
                    ),
                    id="review-body",
                ),
                id="review-scroll",
            ),
            Horizontal(
                Button("Cancel", id="worker-cancel"),
                Button("Confirm reset", id="worker-confirm"),
                id="review-controls",
            ),
            id="review-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#worker-cancel", Button).focus()

    def action_cancel_reset(self) -> None:
        self.dismiss(False)

    def action_no_copy(self) -> None:
        pass  # Do not copy hidden result data from a recovery confirmation.

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "worker-confirm")


class BackendChoiceDialog(ModalScreen[str | None]):
    def __init__(self, choices: tuple[str, ...]) -> None:
        super().__init__()
        self.choices = choices

    def compose(self) -> ComposeResult:
        labels = {
            "worker": "Use worker (provisional)",
            "process": "Use process (isolated, best-effort)",
        }
        yield Vertical(
            Static("Choose an execution backend", id="choice-title"),
            Static(
                "No SQL has been compiled or executed. This choice applies only to this preview.",
                id="choice-copy",
            ),
            Horizontal(
                *(Button(labels.get(c, c), id=f"backend-{c}") for c in self.choices),
                Button("Cancel", id="backend-cancel"),
            ),
            id="backend-choice-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(
            None
            if event.button.id == "backend-cancel"
            else event.button.id.removeprefix("backend-")
            if event.button.id
            else None
        )

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape":
            event.stop()
            self.dismiss(None)
