"""The terminal result viewer for dbt-enhance.

The app deliberately keeps execution separate from rendering: a runner always runs
in a worker thread, while the UI only receives progress and final state updates.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from threading import Event
from typing import ClassVar

from rich.syntax import Syntax
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import DataTable, LoadingIndicator, Static

from .models import EnhanceError, QueryRequest, QueryResult, QueryRunner

# Keep terminal escapes and other invisible controls out of both Rich and the
# terminal. Tabs and newlines are intentionally excluded: SQL and errors need
# their original layout, while result cells make those characters visible.
_UNSAFE_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_CELL_CAP = 180
_DETAIL_CAP = 8_000


def _clean(value: object, *, cap: int | None = None) -> str:
    """Return plain, safe text for a terminal cell (never Rich markup)."""
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 - arbitrary warehouse values may have broken __str__
        text = "<unprintable value>"
    text = text.replace("\t", "⇥").replace("\n", "↵").replace("\r", "␍")
    text = _UNSAFE_CONTROLS.sub("�", text)
    if cap is not None and len(text) > cap:
        return f"{text[: max(0, cap - 1)]}…"
    return text


def _safe_multiline(value: object, *, cap: int | None = None) -> str:
    """Return literal safe text without changing real layout or visible glyphs."""
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 - errors can contain arbitrary driver values
        text = "<unprintable value>"
    # Retain lines across platforms, but make a lone carriage return visible.
    text = text.replace("\r\n", "\n").replace("\r", "␍")
    text = _UNSAFE_CONTROLS.sub("�", text)
    if cap is not None and len(text) > cap:
        return f"{text[: max(0, cap - 1)]}…"
    return text


def _cell_text(value: object, *, cap: int = _CELL_CAP) -> Text:
    """Format a value literally, retaining useful type distinctions."""
    if value is None:
        return Text("NULL", style="italic #d7a65a")
    if isinstance(value, bool):
        return Text(str(value).lower(), style="#79c5bc")
    if isinstance(value, (int, float)):
        return Text(_clean(value, cap=cap), style="#b8d69b")
    return Text(_clean(value, cap=cap), style="#d8e2e0")


def demo_result() -> QueryResult:
    """A small offline result used for a safe, instant visual demo."""
    return QueryResult(
        columns=["customer_id", "customer_name", "orders", "last_order_at", "is_active"],
        rows=[
            [1001, "Northwind Traders", 14, "2025-02-17 09:42:11", True],
            [1002, "Acme Supply Co.", 3, "2025-02-13 16:10:02", True],
            [1003, "Orbit Goods", 0, None, False],
        ],
        compiled_sql=(
            "select customer_id, customer_name, orders, last_order_at, is_active\n"
            "from analytics.customer_summary\n"
            "order by orders desc\n"
            "limit 500"
        ),
        elapsed=0.18,
        project_dir=Path("/workspace/analytics"),
        limit=500,
        target="dev",
    )


class ScrollPanel(Vertical):
    """A tab-stop scroll region for long SQL, messages, and cell details."""

    can_focus = True
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("up", "scroll_up", show=False),
        Binding("down", "scroll_down", show=False),
        Binding("left", "scroll_left", show=False),
        Binding("right", "scroll_right", show=False),
        Binding("pageup", "page_up", show=False),
        Binding("pagedown", "page_down", show=False),
    ]

    def action_page_up(self) -> None:
        self.scroll_page_up()

    def action_page_down(self) -> None:
        self.scroll_page_down()


class QueryApp(App[None]):
    """A compact dbt query viewer suited to a tmux popup."""

    CSS_PATH = "ui.tcss"
    TITLE = "dbt-enhance"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Close"),
        Binding("s", "toggle_sql", "SQL"),
        Binding("a", "toggle_detail", "Cell detail"),
        Binding("h", "table_left", show=False),
        Binding("j", "table_down", show=False),
        Binding("k", "table_up", show=False),
        Binding("l", "table_right", show=False),
        Binding("pageup", "table_page_up", show=False),
        Binding("pagedown", "table_page_down", show=False),
    ]

    def __init__(
        self,
        request: QueryRequest | None = None,
        *,
        runner: QueryRunner | None = None,
        result: QueryResult | None = None,
    ) -> None:
        super().__init__()
        if request is None and result is None:
            raise ValueError("provide a QueryRequest or a QueryResult")
        self.request = request
        self._runner = runner
        self._initial_result = result
        self._cancel_event = Event()
        self._result: QueryResult | None = None
        self._rows: list[list[object]] = []
        self._sql_open = False
        self._detail_open = False

    def compose(self) -> ComposeResult:
        yield Container(
            Horizontal(
                Static("dbt-enhance", id="brand"),
                Static("Query preview", id="subtitle"),
                id="header",
            ),
            Static("", id="context"),
            Horizontal(
                LoadingIndicator(id="spinner"),
                Static("Preparing query…", id="status"),
                Static("", id="result-meta"),
                id="execution",
            ),
            ScrollPanel(Static("", id="warnings"), id="warnings-panel"),
            ScrollPanel(Static("", id="message"), id="message-panel"),
            ScrollPanel(Static("", id="sql"), id="sql-panel"),
            DataTable(id="results", cursor_type="cell", zebra_stripes=True),
            ScrollPanel(Static("", id="detail"), id="detail-panel"),
            Static("s SQL  ·  a detail  ·  Tab panels  ·  q close", id="help"),
            id="shell",
        )

    def on_mount(self) -> None:
        table = self.query_one("#results", DataTable)
        table.show_header = True
        table.show_cursor = True
        self.query_one("#sql-panel").display = False
        self.query_one("#detail-panel").display = False
        self.query_one("#warnings-panel").display = False
        self.query_one("#message-panel").display = False
        if self._initial_result is not None:
            self._show_result(self._initial_result)
        else:
            assert self.request is not None
            self._set_context(self.request.file, self.request.target)
            self._execute_query()

    def _set_context(self, file: Path, target: str | None, project: Path | None = None) -> None:
        """Use popup-sized labels; expose full paths on the context tooltip."""
        file_label = _clean(file.name or file, cap=32)
        bits = [f"file  {file_label}"]
        if project is not None:
            bits.append(f"project  {_clean(project.name or project, cap=20)}")
        bits.append(f"target  {_clean(target or 'profile default', cap=20)}")
        context = self.query_one("#context", Static)
        context.update(Text("  ·  ".join(bits), style="#aabbb8"))
        paths = [f"file: {_safe_multiline(file)}"]
        if project is not None:
            paths.append(f"project: {_safe_multiline(project)}")
        context.tooltip = Text("\n".join(paths))

    @work(thread=True, exclusive=True)
    def _execute_query(self) -> None:
        """Run only once, away from Textual's event loop."""
        assert self.request is not None

        def deliver(callback: object, *args: object) -> None:
            """Do not keep an unwinding runner alive after its popup closes."""
            try:
                self.call_from_thread(callback, *args)  # type: ignore[arg-type]
            except RuntimeError:  # The app may have closed while dbt unwinds.
                pass

        def progress(message: str) -> None:
            deliver(self._set_progress, message)

        try:
            runner = self._runner or self._default_runner()
            query_result = runner(self.request, self._cancel_event, progress)
        except EnhanceError as error:
            deliver(self._show_error, _safe_multiline(error, cap=_DETAIL_CAP))
        except Exception as error:  # noqa: BLE001 - errors belong in the popup, not a vanished terminal
            deliver(self._show_error, _safe_multiline(error, cap=_DETAIL_CAP))
        else:
            deliver(self._show_result, query_result)

    @staticmethod
    def _default_runner() -> QueryRunner:
        """Import dbt only at execution time, keeping offline demos dependency-free."""
        module = importlib.import_module("dbt_enhance.dbt")
        return module.run_query  # type: ignore[no-any-return]

    def _set_progress(self, message: str) -> None:
        if not self._cancel_event.is_set():
            self.query_one("#status", Static).update(
                Text(_clean(message, cap=200) or "Running query…", style="#d7a65a")
            )

    def _show_result(self, result: QueryResult) -> None:
        if self._cancel_event.is_set():
            return
        self._result = result
        self._set_context(
            self.request.file if self.request else result.project_dir,
            result.target,
            result.project_dir,
        )
        self.query_one("#spinner").display = False
        self.query_one("#status", Static).update(Text("Complete", style="#79c5bc"))
        meta = (
            f"{len(result.rows):,} rows  ·  {result.elapsed:.2f}s  ·  "
            f"preview limit {result.limit:,}"
        )
        self.query_one("#result-meta", Static).update(Text(meta, style="#8fd3ca"))
        self._render_sql(result.compiled_sql)
        table = self.query_one("#results", DataTable)
        table.clear(columns=True)
        self._rows = result.rows
        for column_index, column in enumerate(result.columns):
            samples = [
                _clean(row[column_index], cap=_CELL_CAP)
                if column_index < len(row) and row[column_index] is not None
                else "NULL"
                for row in result.rows[:40]
            ]
            width = min(
                30, max(8, len(_clean(column, cap=30)), *(len(sample) for sample in samples))
            )
            table.add_column(Text(_clean(column, cap=30), style="bold #8fd3ca"), width=width)
        for row in result.rows:
            cells: list[Text] = []
            for column_index in range(len(result.columns)):
                value: object = row[column_index] if column_index < len(row) else None
                cells.append(_cell_text(value))
            table.add_row(*cells)
        self._show_warnings(result.warnings)
        if not result.rows:
            self._show_message(
                "No rows returned. Try broadening the filter or choosing another target."
            )
        else:
            self.query_one("#message-panel").display = False
            table.focus()

    def _render_sql(self, sql: str) -> None:
        # Compiled SQL is intentionally never capped: its scrollable panel is
        # the place to inspect the actual statement dbt sent to the warehouse.
        self.query_one("#sql", Static).update(
            Syntax(_safe_multiline(sql), "sql", theme="monokai", word_wrap=True)
        )

    def _show_warnings(self, warnings: list[str]) -> None:
        panel = self.query_one("#warnings-panel", ScrollPanel)
        widget = self.query_one("#warnings", Static)
        if not warnings:
            panel.display = False
            return
        lines = [f"Warning{'s' if len(warnings) != 1 else ''} ({len(warnings)}):"]
        lines.extend(f"• {_safe_multiline(warning)}" for warning in warnings)
        widget.update(Text("\n".join(lines), style="#d7a65a"))
        panel.display = True

    def _show_message(self, message: str) -> None:
        widget = self.query_one("#message", Static)
        widget.update(Text(_safe_multiline(message, cap=_DETAIL_CAP), style="#d7a65a"))
        self.query_one("#message-panel", ScrollPanel).display = True

    def _show_error(self, detail: str) -> None:
        if self._cancel_event.is_set():
            return
        self.query_one("#spinner").display = False
        self.query_one("#status", Static).update(Text("Could not run query", style="#d7a65a"))
        self.query_one("#result-meta", Static).update(Text("No preview available", style="#8fd3ca"))
        self._show_message(f"Query failed: {detail}")

    def action_toggle_sql(self) -> None:
        if self._result is None:
            return
        self._sql_open = not self._sql_open
        panel = self.query_one("#sql-panel", ScrollPanel)
        panel.display = self._sql_open
        if self._sql_open:
            panel.border_title = " Compiled SQL · s to close "

    def action_toggle_detail(self) -> None:
        table = self.query_one("#results", DataTable)
        coordinate = table.cursor_coordinate
        full = self._focused_cell_text(coordinate.row, coordinate.column)
        if full is None:
            return
        self._detail_open = not self._detail_open
        panel = self.query_one("#detail-panel", ScrollPanel)
        panel.display = self._detail_open
        if self._detail_open:
            self.query_one("#detail", Static).update(full)

    @on(DataTable.CellHighlighted)
    def update_cell_detail(self, event: DataTable.CellHighlighted) -> None:
        if self._detail_open:
            full = self._focused_cell_text(event.coordinate.row, event.coordinate.column)
            if full is not None:
                self.query_one("#detail", Static).update(full)

    def _focused_cell_text(self, row: int, column: int) -> Text | None:
        if row < 0 or column < 0 or row >= len(self._rows):
            return None
        values = self._rows[row]
        value: object = values[column] if column < len(values) else None
        return _cell_text(value, cap=_DETAIL_CAP)

    def _table_action(self, action: str) -> None:
        table = self.query_one("#results", DataTable)
        if table.row_count:
            getattr(table, action)()

    def action_table_left(self) -> None:
        self._table_action("action_cursor_left")

    def action_table_right(self) -> None:
        self._table_action("action_cursor_right")

    def action_table_up(self) -> None:
        self._table_action("action_cursor_up")

    def action_table_down(self) -> None:
        self._table_action("action_cursor_down")

    def action_table_page_up(self) -> None:
        self._table_action("scroll_page_up")

    def action_table_page_down(self) -> None:
        self._table_action("scroll_page_down")

    def action_quit(self) -> None:
        self._cancel_event.set()
        self.exit()

    def on_unmount(self) -> None:
        self._cancel_event.set()
