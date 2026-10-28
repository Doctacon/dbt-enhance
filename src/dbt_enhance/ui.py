"""The terminal result viewer for dbt-enhance.

The app deliberately keeps execution separate from rendering: a runner always runs
in a worker thread, while the UI only receives progress and final state updates.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import math
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from sys import platform
from threading import Event, Thread
from typing import ClassVar

from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.message import Message
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import DataTable, LoadingIndicator, Static

from .models import (
    SSO_GUIDANCE,
    SSO_STATUS,
    BackendChoiceRequired,
    EnhanceError,
    QueryRequest,
    QueryResult,
    QueryRunner,
    ResolvedContext,
)
from .preview import ColumnFilter, PreviewModel, PreviewView
from .search import LiteralSearch
from .ui_dialogs import (
    ActionDialog,
    BackendChoiceDialog,
    ColumnDialog,
    FilterDialog,
    TableSearchDialog,
    TextReviewDialog,
    WorkerResetDialog,
)
from .ui_text import clean_inline as _clean
from .ui_text import safe_multiline as _safe_multiline
from .ui_theme import DARK, LIGHT, preferred_theme
from .ui_widgets import PreviewTable, ScrollPanel

_CELL_CAP = 180
_FIND_STYLE = Style(bold=True, reverse=True, meta={"dbt_enhance_match": True})


def _cell_text(value: object, *, cap: int = _CELL_CAP) -> Text:
    """Format a value literally, retaining useful type distinctions."""
    if value is None:
        return Text("NULL", style="italic")
    if isinstance(value, bool):
        return Text(str(value).lower(), style="bold")
    return Text(_clean(value, cap=cap))


class _QueryProgress(Message):
    def __init__(self, status: str) -> None:
        super().__init__()
        self.status = status


class _QuerySucceeded(Message):
    def __init__(self, result: QueryResult) -> None:
        super().__init__()
        self.result = result


class _QueryFailed(Message):
    def __init__(self, detail: str, metadata: dict[str, object] | None = None) -> None:
        super().__init__()
        self.detail = detail
        self.metadata = metadata


class _BackendChoice(Message):
    def __init__(self, choices: tuple[str, ...], context: ResolvedContext | None = None) -> None:
        super().__init__()
        self.choices = choices
        self.context = context


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


class QueryApp(App[None]):
    """A compact dbt query viewer suited to a tmux popup."""

    CSS_PATH = "ui.tcss"
    TITLE = "dbt-enhance"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Close"),
        Binding("s", "toggle_sql", "SQL"),
        Binding("a", "toggle_detail", "Cell detail"),
        Binding("y", "copy_cell", "Copy cell"),
        Binding("Y", "copy_sql", "Copy SQL"),
        Binding("/", "search", "Find"),
        Binding("n", "search_next", show=False),
        Binding("N", "search_previous", show=False),
        Binding("ctrl+p", "actions", "Actions", priority=True),
        Binding("r", "reset_view", "Reset view"),
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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._clock = clock
        self.register_theme(DARK)
        self.register_theme(LIGHT)
        self.theme = preferred_theme(request.theme if request else "auto", os.environ)
        if request is None and result is None:
            raise ValueError("provide a QueryRequest or a QueryResult")
        self.request = request
        self._runner = runner
        self._initial_result = result
        self._cancel_event = Event()
        self._result: QueryResult | None = None
        self._rows: list[list[object]] = []
        self._preview: PreviewModel | None = None
        self._source_row_ids: list[int] = []
        self._visible_column_ids: list[int] = []
        self._search_matches: tuple[tuple[int, int], ...] = ()
        self._search_index = -1
        self._last_search_literal = ""
        self._search_scope = "table"
        self._text_search: LiteralSearch | None = None
        self._chooser_paused_at: float | None = None
        self._choice_context: ResolvedContext | None = None
        self._backend_choices: tuple[str, ...] = ()
        self._query_timeout: float | None = None
        self._prechoice_elapsed = 0.0
        self._native_error = ""
        self._error_metadata: dict[str, object] = {}
        self._worker_action_pending = False
        self._sql_open = False
        self._detail_open = False
        self._terminal = False
        self._last_status: str | None = None
        self._query_started_at: float | None = None
        self._timeout_timer: Timer | None = None
        self._elapsed_timer: Timer | None = None
        self._query_thread: Thread | None = None

    @property
    def _view(self) -> Screen:
        """The result screen survives while an action or recovery dialog is open."""
        return self.screen_stack[0]

    def compose(self) -> ComposeResult:
        yield Container(
            Horizontal(
                Static("dbt-enhance", id="brand"),
                Static("Query preview", id="subtitle"),
                id="header",
            ),
            Static("", id="context"),
            Static("", id="run-context"),
            Horizontal(
                LoadingIndicator(id="spinner"),
                Static("Preparing query…", id="status"),
                Static("", id="result-meta"),
                id="execution",
            ),
            ScrollPanel(Static("", id="warnings"), id="warnings-panel"),
            ScrollPanel(Static("", id="message"), id="message-panel"),
            ScrollPanel(Static("", id="sql"), id="sql-panel"),
            PreviewTable(id="results", cursor_type="cell", zebra_stripes=True),
            ScrollPanel(Static("", id="detail"), id="detail-panel"),
            Static(
                "Ctrl+P actions · q close · / find · s SQL · a detail · y/Y copy · r reset",
                id="help",
            ),
            id="shell",
        )

    def _tone(self, role: str) -> str:
        theme = self.current_theme
        return (
            theme.variables.get(role) or getattr(theme, role, None) or theme.foreground or "default"
        )

    def on_mount(self) -> None:
        table = self._view.query_one("#results", DataTable)
        table.show_header = True
        table.show_cursor = True
        self._view.query_one("#sql-panel").display = False
        self._view.query_one("#detail-panel").display = False
        self._view.query_one("#warnings-panel").display = False
        self._view.query_one("#message-panel").display = False
        if self._initial_result is not None:
            self._show_result(self._initial_result)
        else:
            assert self.request is not None
            self._set_context(self.request.file, self.request.target)
            self._start_query()

    def _start_query(self) -> None:
        """Start UI-owned timing before handing the query to a worker."""
        assert self.request is not None
        timeout = self.request.timeout
        if (
            not isinstance(timeout, bool)
            and isinstance(timeout, (int, float))
            and math.isfinite(float(timeout))
            and timeout > 0
        ):
            self._query_started_at = self._clock()
            self._query_timeout = float(timeout)
            self._update_pending_meta()
            refresh_interval = min(0.1, max(float(timeout) / 10, 0.01))
            self._elapsed_timer = self.set_interval(
                refresh_interval, self._update_pending_meta, name="query elapsed"
            )
            self._timeout_timer = self.set_timer(
                float(timeout), self._show_timeout, name="query timeout"
            )
        self._execute_query()

    def _set_context(self, file: Path, target: str | None, project: Path | None = None) -> None:
        """Use popup-sized labels; expose full paths on the context tooltip."""
        file_label = _clean(file.name or file, cap=32)
        bits = [f"file  {file_label}"]
        if project is not None:
            bits.append(f"project  {_clean(project.name or project, cap=20)}")
        bits.append(f"target  {_clean(target or 'profile default', cap=20)}")
        context = self._view.query_one("#context", Static)
        context.update(Text(_clean("  ·  ".join(bits)), style=self._tone("secondary")))
        paths = [f"file: {_safe_multiline(file)}"]
        if project is not None:
            paths.append(f"project: {_safe_multiline(project)}")
        context.tooltip = Text("\n".join(paths))

    def _execute_query(self) -> None:
        """Run the single query on an app-owned daemon thread."""
        self._query_thread = Thread(
            target=self._run_query,
            name="dbt-enhance-query",
            daemon=True,
        )
        self._query_thread.start()

    def _run_query(self) -> None:
        """Run away from Textual's event loop and deliver only queued messages."""
        assert self.request is not None

        def deliver(message: Message) -> None:
            try:
                self.post_message(message)
            except RuntimeError:  # The app may have closed while dbt unwinds.
                pass

        def progress(status: str) -> None:
            deliver(_QueryProgress(status))

        try:
            runner = self._runner or self._default_runner()
            query_result = runner(self.request, self._cancel_event, progress)
        except BackendChoiceRequired as error:
            deliver(_BackendChoice(error.choices, error.context))
        except EnhanceError as error:
            # The error drawer is scrollable; preserve the native local detail.
            deliver(
                _QueryFailed(
                    _safe_multiline(error),
                    {
                        "reason": error.code,
                        "stage": error.stage,
                        "outcome": error.code
                        if error.code in {"cancelled", "timeout"}
                        else "failure",
                        "cancellation": getattr(error, "cancellation", None),
                    },
                )
            )
        except Exception as error:  # noqa: BLE001 - errors belong in the popup, not a vanished terminal
            deliver(_QueryFailed(_safe_multiline(error)))
        else:
            deliver(_QuerySucceeded(query_result))

    @on(_QueryProgress)
    def _on_query_progress(self, event: _QueryProgress) -> None:
        self._set_progress(event.status)

    @on(_QuerySucceeded)
    def _on_query_succeeded(self, event: _QuerySucceeded) -> None:
        self._show_result(event.result)

    @on(_BackendChoice)
    def _on_backend_choice(self, event: _BackendChoice) -> None:
        if self._terminal or self.request is None:
            return
        now = self._clock()
        if self._query_timeout is None or self._query_started_at is None:
            self._show_error("Invalid query timeout. No SQL was compiled or executed.")
            return
        if now - self._query_started_at >= self._query_timeout:
            self._show_timeout()
            return
        self._choice_context = event.context
        self._backend_choices = event.choices
        if event.context is not None:
            self._set_context(self.request.file, event.context.target, event.context.project_dir)
        self._chooser_paused_at = now
        self._stop_query_timers()
        self._view.query_one("#spinner").display = False
        self._set_progress("Choose backend — no SQL has run")
        self.push_screen(BackendChoiceDialog(event.choices), self._backend_chosen)

    def _backend_chosen(self, backend: str | None) -> None:
        if self._terminal or self.request is None:
            return
        now = self._clock()
        paused = (
            max(0.0, now - self._chooser_paused_at) if self._chooser_paused_at is not None else 0.0
        )
        self._chooser_paused_at = None
        if backend is None:
            self._show_error(
                "Backend selection cancelled. No SQL was compiled or executed.",
                {
                    "outcome": "cancelled",
                    "reason": "cancelled",
                    "stage": "preflight",
                    "cancellation": "not_requested",
                },
            )
            return
        if backend not in self._backend_choices:
            self._show_error("Invalid backend choice. No SQL was compiled or executed.")
            return
        assert self._query_started_at is not None and self._query_timeout is not None
        self._query_started_at += paused
        elapsed = max(0.0, now - self._query_started_at)
        remaining = self._query_timeout - elapsed
        if remaining <= 0:
            self._show_timeout()
            return
        self._prechoice_elapsed = elapsed
        self.request = replace(
            self.request,
            backend=backend,
            backend_origin="interactive_one_use",
            timeout=remaining,
            target=self._choice_context.target if self._choice_context else self.request.target,
        )
        self._view.query_one("#spinner").display = True
        self._elapsed_timer = self.set_interval(
            min(0.1, max(remaining / 10, 0.01)), self._update_pending_meta
        )
        self._timeout_timer = self.set_timer(remaining, self._show_timeout)
        self._execute_query()

    @on(_QueryFailed)
    def _on_query_failed(self, event: _QueryFailed) -> None:
        self._show_error(event.detail, event.metadata)

    @staticmethod
    def _default_runner() -> QueryRunner:
        """Import dbt only at execution time, keeping offline demos dependency-free."""
        module = importlib.import_module("dbt_enhance.dbt")
        return module.run_query  # type: ignore[no-any-return]

    def _set_progress(self, message: str) -> None:
        if self._terminal:
            return
        # QueryStatus is a str subclass, so legacy runners still work.
        target = getattr(message, "target", None)
        if target is not None and self.request is not None:
            verified = (
                "verified"
                if getattr(message, "target_verified", False)
                else "requested / unresolved"
            )
            self._view.query_one("#run-context", Static).update(
                Text(
                    f"target {verified}: {_clean(target, cap=20)} · "
                    f"backend {getattr(message, 'backend', None) or self.request.backend} · "
                    f"support {getattr(message, 'support_tier', 'provisional')}",
                    style=self._tone("quiet"),
                )
            )
        status = _clean(message, cap=200) or "Running query…"
        self._last_status = status
        self._view.query_one("#status", Static).update(
            Text(_clean(status), style=self._tone("warning"))
        )

    def _update_pending_meta(self) -> None:
        if self._terminal or self._query_started_at is None or self.request is None:
            return
        elapsed = self._clock() - self._query_started_at
        timeout = (
            self._query_timeout if self._query_timeout is not None else float(self.request.timeout)
        )
        self._view.query_one("#result-meta", Static).update(
            Text(f"{elapsed:.1f}s elapsed  ·  {timeout:g}s timeout", style=self._tone("primary"))
        )

    def _stop_query_timers(self) -> None:
        for timer in (self._timeout_timer, self._elapsed_timer):
            if timer is not None:
                timer.stop()
        self._timeout_timer = None
        self._elapsed_timer = None

    def _finish_ui(self) -> bool:
        """Claim the terminal UI state, rejecting all later worker callbacks."""
        if self._terminal:
            return False
        self._terminal = True
        self._stop_query_timers()
        return True

    def _show_timeout(self) -> None:
        if not self._finish_ui():
            return
        assert self.request is not None
        timeout = (
            self._query_timeout if self._query_timeout is not None else float(self.request.timeout)
        )
        elapsed = (
            self._clock() - self._query_started_at
            if self._query_started_at is not None
            else timeout
        )
        self._view.query_one("#spinner").display = False
        self._error_metadata = {
            "outcome": "timeout",
            "reason": "timeout",
            "cancellation": "requested_unverified",
        }
        self._view.query_one("#status", Static).update(
            Text("Query timed out", style=self._tone("warning"))
        )
        self._view.query_one("#result-meta", Static).update(
            Text(f"{elapsed:.1f}s elapsed  ·  {timeout:g}s timeout", style=self._tone("primary"))
        )
        detail = f"Query failed: dbt query timed out after {timeout:g} seconds."
        if self._last_status == SSO_STATUS:
            detail = f"{detail}\n{SSO_GUIDANCE}"
        self._show_message(detail)
        # Render the timeout before asking a potentially blocked backend to cancel.
        self._cancel_event.set()

    def _show_result(self, result: QueryResult) -> None:
        if not self._finish_ui():
            return
        if self._prechoice_elapsed:
            result = replace(result, elapsed=result.elapsed + self._prechoice_elapsed)
        self._result = result
        self._set_context(
            self.request.file if self.request else result.project_dir,
            result.target,
            result.project_dir,
        )
        self._view.query_one("#spinner").display = False
        self._view.query_one("#status", Static).update(
            Text("Complete", style=self._tone("success"))
        )
        self._preview = PreviewModel.from_result(result)
        self._render_preview()
        self._render_sql(result.compiled_sql)
        self._update_result_context(result)
        self._show_warnings(result.warnings)
        if not result.rows:
            self._show_message(
                "No rows returned. Try broadening the filter or choosing another target."
            )
        else:
            self._view.query_one("#message-panel").display = False
            self._view.query_one("#results", DataTable).focus()

    def _update_result_context(self, result: QueryResult) -> None:
        target_state = "verified" if result.target_verified else "requested / unresolved"
        run_bits = [
            f"target {target_state}",
            f"backend {result.backend or (self.request.backend if self.request else 'offline')}",
            f"support {result.support_tier}",
            f"session {result.session_state or 'local'}",
        ]
        if result.adapter:
            run_bits.append(f"adapter {result.adapter}")
        self._view.query_one("#run-context", Static).update(
            Text(_clean("  ·  ".join(run_bits)), style=self._tone("quiet"))
        )

    def _pin_fits(self, view: PreviewView) -> bool:
        table = self._view.query_one("#results", DataTable)
        available = table.scrollable_content_region.width
        pinned = [column for column in view.columns if column.pinned]
        if not pinned or available <= 0:
            return True
        width = sum(column.width + 2 * table.cell_padding for column in pinned)
        # Leave the model's minimum cell width in the horizontally scrolling area.
        reserve = 4 + 2 * table.cell_padding if len(pinned) < len(view.columns) else 0
        return width + reserve <= available

    @on(PreviewTable.ViewportChanged)
    def _fit_pins_after_resize(self) -> None:
        if self._preview is None or self._preview._pin_end is None or not self.screen_stack:
            return
        if not self._pin_fits(self._preview.view()):
            self._render_preview()

    def _render_preview(self) -> None:
        """Render the local projection; values retain original source identities."""
        if self._preview is None:
            return
        view = self._preview.view()
        if not self._pin_fits(view):
            self._preview.pin_through(None)
            view = self._preview.view()
            self.notify("Pinned prefix no longer fits; unpinned to keep columns reachable.")
        table = self._view.query_one("#results", DataTable)
        table.clear(columns=True)
        self._rows = [list(row.values) for row in view.rows]
        self._source_row_ids = [row.index for row in view.rows]
        self._visible_column_ids = [column.index for column in view.columns]
        table.fixed_columns = sum(column.pinned for column in view.columns)
        for column in view.columns:
            marker = "│ " if column.pinned else ""
            table.add_column(
                Text(marker + _clean(column.name, cap=30), style=f"bold {self._tone('primary')}"),
                width=column.width,
            )
        for row in view.rows:
            table.add_row(
                *[
                    _cell_text(row.values[c.index] if c.index < len(row.values) else None)
                    for c in view.columns
                ]
            )
        state = []
        if self._preview._filters:
            filter_count = len(self._preview._filters)
            plural = "s" if filter_count != 1 else ""
            state.append(f"{filter_count} filter{plural}")
        if self._preview._sort:
            state.append("sorted")
        if self._preview._hidden:
            state.append(f"{len(self._preview._hidden)} hidden")
        if self._preview._widths:
            state.append("manual widths")
        if self._preview._pin_end is not None:
            state.append("pinned prefix")
        suffix = f" · {', '.join(state)} · r reset" if state else ""
        if self._result:
            meta = (
                f"{view.fetched_count:,} rows fetched · {view.filtered_count:,} shown · "
                f"{self._result.elapsed:.2f}s · cap {view.cap:,}{suffix}"
            )
        else:
            meta = ""
        self._view.query_one("#result-meta", Static).update(Text(meta, style=self._tone("primary")))

    def _render_sql(self, sql: str) -> None:
        # Compiled SQL is intentionally never capped: its scrollable panel is
        # the place to inspect the actual statement dbt sent to the warehouse.
        self._view.query_one("#sql", Static).update(
            Syntax(
                _safe_multiline(sql),
                "sql",
                theme="monokai" if self.current_theme.dark else "friendly",
                word_wrap=True,
            )
        )

    def _show_warnings(self, warnings: list[str]) -> None:
        panel = self._view.query_one("#warnings-panel", ScrollPanel)
        widget = self._view.query_one("#warnings", Static)
        if not warnings:
            panel.display = False
            return
        lines = [f"Warning{'s' if len(warnings) != 1 else ''} ({len(warnings)}):"]
        lines.extend(f"• {_safe_multiline(warning)}" for warning in warnings)
        widget.update(Text("\n".join(lines), style=self._tone("warning")))
        panel.display = True

    def _show_message(self, message: str) -> None:
        widget = self._view.query_one("#message", Static)
        widget.update(Text(_safe_multiline(message), style=self._tone("warning")))
        self._view.query_one("#message-panel", ScrollPanel).display = True

    def _show_error(self, detail: str, metadata: dict[str, object] | None = None) -> None:
        if not self._finish_ui():
            return
        self._native_error = detail
        self._error_metadata = metadata or {"outcome": "failure", "reason": "query_failed"}
        self._view.query_one("#spinner").display = False
        self._view.query_one("#status", Static).update(
            Text("Could not run query", style=self._tone("warning"))
        )
        self._view.query_one("#result-meta", Static).update(
            Text("No preview available", style=self._tone("primary"))
        )
        self._view.query_one(
            "#message-panel", ScrollPanel
        ).border_title = " Error detail · no rerun "
        message = (
            f"Query failed: {detail}\n\nReview the detail above. "
            "No query was replayed; close to return to your editor."
        )
        self._show_message(message)

    def action_toggle_sql(self) -> None:
        if self._result is None:
            return
        self._sql_open = not self._sql_open
        panel = self._view.query_one("#sql-panel", ScrollPanel)
        panel.display = self._sql_open
        if self._sql_open:
            panel.border_title = " Compiled SQL · Y copy all · s close "

    async def _copy_text(self, text: str, label: str) -> None:
        """Send full source text, not the truncated/sanitized renderable."""
        if not text:
            # tmux load-buffer accepts empty stdin but leaves the old buffer intact.
            self.notify(f"{label} is empty; clipboard unchanged.")
            return
        if platform == "darwin" and not any(
            os.environ.get(key) for key in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
        ):
            # Terminal.app has no OSC 52 support. Only use the native clipboard
            # locally: over SSH the destination is the user's terminal, not this host.
            command = ["/usr/bin/pbcopy"]
            success = f"{label} copied to clipboard."
        elif os.environ.get("TMUX"):
            # Textual's OSC 52 is ignored by tmux's default set-clipboard=external.
            command = ["tmux", "load-buffer", "-w", "-"]
            success = f"{label} saved to tmux buffer; terminal clipboard requested."
        else:
            self.copy_to_clipboard(text)
            self.notify(f"{label} sent to terminal clipboard.")
            return
        # stdin keeps query data out of process arguments; a thread keeps rendering
        # responsive, and the timeout bounds a stalled clipboard process.
        try:
            await asyncio.to_thread(
                subprocess.run,
                command,
                input=text.encode("utf-8"),
                capture_output=True,
                check=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            self.notify("Could not copy to clipboard. Try again.", severity="error")
            return
        self.notify(success)

    async def action_copy_sql(self) -> None:
        if self._result is None:
            self.notify("No compiled SQL to copy yet.", severity="warning")
            return
        await self._copy_text(self._result.compiled_sql, "Compiled SQL")

    async def action_copy_cell(self) -> None:
        table = self._view.query_one("#results", DataTable)
        row, column = table.cursor_coordinate
        if self._result is None or not (
            0 <= row < len(self._rows) and 0 <= column < len(self._result.columns)
        ):
            self.notify("No result cell to copy.", severity="warning")
            return
        values = self._rows[row]
        source_column = (
            self._visible_column_ids[column] if column < len(self._visible_column_ids) else -1
        )
        value = values[source_column] if 0 <= source_column < len(values) else None
        try:
            if value is None:
                text = "NULL"
            elif isinstance(value, bool):
                text = str(value).lower()
            else:
                text = str(value)
        except Exception:  # noqa: BLE001 - warehouse values can have broken __str__
            self.notify("Could not convert this cell to text.", severity="error")
            return
        await self._copy_text(text, "Cell")

    def action_toggle_detail(self) -> None:
        table = self._view.query_one("#results", DataTable)
        coordinate = table.cursor_coordinate
        full = self._focused_cell_text(coordinate.row, coordinate.column)
        if full is None:
            return
        self._detail_open = not self._detail_open
        panel = self._view.query_one("#detail-panel", ScrollPanel)
        panel.display = self._detail_open
        if self._detail_open:
            self._view.query_one("#detail", Static).update(full)

    @on(DataTable.CellHighlighted)
    def update_cell_detail(self, event: DataTable.CellHighlighted) -> None:
        if self._detail_open:
            full = self._focused_cell_text(event.coordinate.row, event.coordinate.column)
            if full is not None:
                self._view.query_one("#detail", Static).update(full)

    def _focused_cell_text(self, row: int, column: int) -> Text | None:
        if row < 0 or column < 0 or row >= len(self._rows):
            return None
        values = self._rows[row]
        source_column = (
            self._visible_column_ids[column] if column < len(self._visible_column_ids) else -1
        )
        value: object = values[source_column] if 0 <= source_column < len(values) else None
        # Detail deliberately has no cap: rendered cells are compact, inspection is not.
        return _cell_text(value, cap=None)

    def _table_action(self, action: str) -> None:
        table = self._view.query_one("#results", DataTable)
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

    def action_search(self) -> None:
        if self._preview is None:
            return
        focused = self.focused.id if self.focused else None
        scope = {"sql-panel": "sql", "detail-panel": "detail"}.get(focused, "table")
        self.push_screen(TableSearchDialog(scope), self._search_submitted)

    def _search_submitted(self, request: tuple[str, str, bool] | None) -> None:
        if request and request[1]:
            self.search(request[0], request[1], previous=request[2])

    def action_search_next(self) -> None:
        if self._last_search_literal:
            self.search(self._search_scope, self._last_search_literal)

    def action_search_previous(self) -> None:
        if self._last_search_literal:
            self.search(self._search_scope, self._last_search_literal, previous=True)

    def search(self, scope: str, literal: str, *, previous: bool = False) -> bool:
        """Navigate literal matches in the focused view without changing source data."""
        if scope not in {"table", "sql", "detail"} or not literal:
            return False
        if scope != self._search_scope or literal != self._last_search_literal:
            self._search_matches, self._search_index = (), -1
            self._text_search = None
        self._search_scope, self._last_search_literal = scope, literal
        if scope == "table":
            return self.search_table(literal, previous=previous)
        source = self._result.compiled_sql if scope == "sql" and self._result else ""
        if scope == "detail":
            table = self._view.query_one("#results", DataTable)
            row, column = table.cursor_coordinate
            if not 0 <= row < len(self._rows) or not 0 <= column < len(self._visible_column_ids):
                return False
            values = self._rows[row]
            source_column = self._visible_column_ids[column]
            value = values[source_column] if source_column < len(values) else None
            try:
                source = (
                    "NULL"
                    if value is None
                    else str(value).lower()
                    if isinstance(value, bool)
                    else str(value)
                )
            except Exception:  # noqa: BLE001 - arbitrary returned values
                self.notify("This cell cannot be searched as text.", severity="warning")
                return False
        if self._text_search is None or self._text_search.text != source:
            self._text_search = LiteralSearch(source, literal)
        span = self._text_search.step(previous=previous)
        if span is None:
            self.notify(f"No full {scope} matches.")
            return False
        self._reveal_text_match(scope, source, span)
        return True

    def _reveal_text_match(self, scope: str, source: str, span: tuple[int, int]) -> None:
        start, end = span
        sanitize = _safe_multiline if scope == "sql" else _clean
        text = sanitize(source)
        prefix = sanitize(source[:start])
        display_start = len(prefix)
        display_end = display_start + len(sanitize(source[start:end]))
        if scope == "sql":
            if not self._sql_open:
                self.action_toggle_sql()
            syntax = Syntax(
                text,
                "sql",
                theme="monokai" if self.current_theme.dark else "friendly",
                word_wrap=True,
            )
            first_line = prefix.count("\n") + 1
            first_column = len(prefix.rsplit("\n", 1)[-1].expandtabs(syntax.tab_size))
            end_prefix = text[:display_end]
            syntax.stylize_range(
                _FIND_STYLE,
                (first_line, first_column),
                (
                    end_prefix.count("\n") + 1,
                    len(end_prefix.rsplit("\n", 1)[-1].expandtabs(syntax.tab_size)),
                ),
            )
            self._view.query_one("#sql", Static).update(syntax)
        else:
            if not self._detail_open:
                self.action_toggle_detail()
            highlighted = Text(text)
            highlighted.stylize(_FIND_STYLE, display_start, display_end)
            self._view.query_one("#detail", Static).update(highlighted)
        panel = self._view.query_one(f"#{scope}-panel", ScrollPanel)
        panel.border_title = Text(
            f" Find in {scope} · {_clean(self._last_search_literal, cap=16)} · n/N next/previous "
        )
        panel.focus()

        def reveal() -> None:
            body = self._view.query_one(f"#{scope}", Static)
            # Use the rendered range rather than guessing from a truncated
            # prefix: word boundaries, tabs and wide glyphs can change wrapping.
            for line in range(body.size.height):
                if any(
                    segment.style and segment.style.meta.get("dbt_enhance_match")
                    for segment in body.render_line(line)
                ):
                    panel.scroll_to(y=line, animate=False, immediate=True)
                    break

        self.call_after_refresh(reveal)

    def search_table(self, literal: str, *, previous: bool = False) -> bool:
        """Find the next literal match in visible, transformed table cells."""
        if self._preview is None:
            return False
        matches = self._preview.search("table", literal)
        if matches != self._search_matches:
            self._search_matches, self._search_index = matches, -1
        if not matches:
            self.notify("No visible table matches.")
            return False
        self._search_index = (self._search_index + (-1 if previous else 1)) % len(matches)
        source_row, source_column = matches[self._search_index]
        try:
            row = self._source_row_ids.index(source_row)
            column = self._visible_column_ids.index(source_column)
        except ValueError:
            return False
        self._view.query_one("#results", DataTable).move_cursor(row=row, column=column)
        self.notify(f"Match {self._search_index + 1}/{len(matches)}")
        return True

    def action_actions(self) -> None:
        self.push_screen(ActionDialog(), self._action_chosen)

    def action_column_chooser(self) -> None:
        if self._preview is not None:
            self.push_screen(ColumnDialog(self._preview), self._column_chosen)

    def _column_chosen(self, choice: tuple[str, int | None] | None) -> None:
        if not choice or self._preview is None:
            return
        action, column = choice
        if action == "restore-all":
            self._preview.restore_all_columns()
            self._render_preview()
            return
        if action == "unpin":
            self._preview.pin_through(None)
            self._render_preview()
            return
        if column is None:
            self.notify("No matching column.", severity="warning")
            return
        try:
            if action == "hide":
                self._preview.hide_column(column)
            elif action == "restore":
                self._preview.restore_column(column)
            elif action == "auto":
                self._preview.resize_column(column, None)
            elif action == "minus":
                self._preview.resize_by(column, -2)
            elif action == "plus":
                self._preview.resize_by(column, 2)
            elif action == "pin":
                self._pin_column(column)
                return
            self._render_preview()
        except ValueError as error:
            self.notify(str(error), severity="warning")

    def action_filter_builder(self) -> None:
        if self._preview is not None:
            self.push_screen(FilterDialog(self._preview), self._filter_chosen)

    def _filter_chosen(self, choice: tuple[str, object] | None) -> None:
        if not choice or self._preview is None:
            return
        action, value = choice
        if action == "clear":
            try:
                self._preview.clear_filters()
                self._render_preview()
            except ValueError as error:
                self.notify(str(error), severity="warning")
            return
        if action == "remove":
            try:
                self._preview.remove_filter(int(value))
                self._render_preview()
            except ValueError as error:
                self.notify(str(error), severity="warning")
        elif action == "add" and isinstance(value, ColumnFilter):
            self.apply_filter(value)

    def _action_chosen(self, action: str | None) -> None:
        if action:
            method = getattr(self, f"action_{action}", None)
            if method is not None:
                result = method()
                # Async copy actions are scheduled by Textual when invoked by bindings;
                # here they are intentionally left to the normal binding surface.
                if asyncio.iscoroutine(result):
                    self.call_after_refresh(lambda: asyncio.create_task(result))

    def apply_filter(self, filter: ColumnFilter) -> bool:
        """Apply a local AND filter and retain source-cell copy identities."""
        if self._preview is None:
            return False
        try:
            self._preview.add_filter(filter)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return False
        self._search_matches, self._search_index = (), -1
        self._render_preview()
        return True

    def sort_column(self, column: str | int, direction: str = "asc") -> bool:
        if self._preview is None:
            return False
        try:
            self._preview.set_sort(column, direction)
            self._render_preview()
            return True
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return False

    def hide_preview_column(self, column: str | int) -> bool:
        if self._preview is None:
            return False
        try:
            self._preview.hide_column(column)
            self._render_preview()
            return True
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return False

    def restore_preview_column(self, column: str | int) -> bool:
        if self._preview is None:
            return False
        try:
            self._preview.restore_column(column)
            self._render_preview()
            return True
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return False

    def _selected_source_column(self) -> int | None:
        table = self._view.query_one("#results", DataTable)
        column = table.cursor_coordinate.column
        return (
            self._visible_column_ids[column]
            if 0 <= column < len(self._visible_column_ids)
            else None
        )

    def action_sort_selected(self) -> None:
        self.action_sort_ascending()

    def action_sort_ascending(self) -> None:
        column = self._selected_source_column()
        if column is not None:
            self.sort_column(column, "asc")

    def action_sort_descending(self) -> None:
        column = self._selected_source_column()
        if column is not None:
            self.sort_column(column, "desc")

    def action_clear_sort(self) -> None:
        if self._preview is not None:
            self._preview.clear_sort()
            self._render_preview()

    def action_hide_selected(self) -> None:
        column = self._selected_source_column()
        if column is not None:
            self.hide_preview_column(column)

    def _pin_column(self, column: int) -> None:
        assert self._preview is not None
        previous = self._preview._pin_end
        self._preview.pin_through(column)
        if not self._pin_fits(self._preview.view()):
            self._preview.pin_through(previous)
            self.notify(
                "That prefix is too wide. Resize columns or pin fewer columns.", severity="warning"
            )
            return
        self._render_preview()

    def action_pin_selected(self) -> None:
        column = self._selected_source_column()
        if column is not None and self._preview is not None:
            self._pin_column(column)

    def action_reset_view(self) -> None:
        if self._preview is None:
            return
        self._preview.reset_transforms()
        self._search_matches, self._search_index = (), -1
        self._last_search_literal = ""
        self._text_search = None
        self._render_preview()
        if self._result is not None:
            self._render_sql(self._result.compiled_sql)
        self.notify("Preview filters, sort, sizing, hidden columns, and pins reset.")

    def _current_project(self) -> Path | None:
        if self._result is not None:
            return self._result.project_dir
        if self._choice_context is not None:
            return self._choice_context.project_dir
        if self.request is not None:
            from .dbt import discover_project

            try:
                return discover_project(self.request.file)
            except (EnhanceError, OSError):
                pass
        return None

    def action_reset_worker(self) -> None:
        if self._worker_action_pending:
            self.notify("A worker recovery action is already open.")
            return
        project = self._current_project()
        if project is None or (
            self.request is None and (self._result is None or self._result.backend != "worker")
        ):
            self.notify("No project worker is associated with this preview.")
            return
        self._worker_action_pending = True
        self.run_worker(self._prepare_worker_reset(project), group="worker-recovery")

    async def _prepare_worker_reset(self, project: Path) -> None:
        from .worker import inspect_worker

        try:
            information = await asyncio.to_thread(inspect_worker, project)
            if information is None:
                self.notify("No running worker for this project. Nothing was changed.")
                self._worker_action_pending = False
                return
            active = information.get("active")
            if not isinstance(active, bool):
                raise EnhanceError("Could not verify whether the worker is active.")
        except (EnhanceError, OSError) as error:
            self.notify(_safe_multiline(error), severity="error")
            self._worker_action_pending = False
            return

        def confirmed(approve: bool) -> None:
            if not approve:
                self._worker_action_pending = False
                return
            self.run_worker(self._reset_project_worker(project, active), group="worker-recovery")

        self.push_screen(WorkerResetDialog(_clean(project), active=active), confirmed)

    async def _reset_project_worker(self, project: Path, allow_active: bool) -> None:
        from .worker import reset

        try:
            changed = await asyncio.to_thread(reset, project, allow_active=allow_active)
            if not changed:
                self.notify(
                    "Worker not reset; it may have changed state. No query was rerun.",
                    severity="warning",
                )
                return
            if self._result is not None and self._result.backend == "worker":
                self._result = replace(self._result, session_state="disconnected")
                self._update_result_context(self._result)
            message = "Local worker reset. No query was rerun."
            if allow_active:
                message += " Remote cancellation remains unverified."
            self.notify(message)
        except (EnhanceError, OSError) as error:
            self.notify(_safe_multiline(error), severity="error")
        finally:
            self._worker_action_pending = False

    def action_native_error(self) -> None:
        if not self._native_error:
            self.notify("There is no local native error to review.")
            return
        self.push_screen(
            TextReviewDialog("Local native error — not shared", _safe_multiline(self._native_error))
        )

    def action_support_report(self) -> None:
        report = self.review_diagnostic_report()

        def reviewed(copy: bool) -> None:
            if copy:
                self.run_worker(self._copy_text(report, "Support report copied"), group="clipboard")

        self.push_screen(
            TextReviewDialog("Metadata report — review before copy", report, allow_copy=True),
            reviewed,
        )

    def review_diagnostic_report(self) -> str:
        """Return strict diagnostics JSON; never passes SQL, paths, or native errors."""
        from .diagnostics import DiagnosticStore, build_shareable_report

        project = self._current_project()
        store = DiagnosticStore(enabled=bool(self.request is not None and self.request.diagnostics))
        latest = store.latest(project) if project is not None else None
        failures = store.failures(project) if project is not None else []
        # A blocked native cleanup may not have recorded this viewer's watchdog
        # yet. Never replace its known timeout/cancellation with a previous success.
        if (latest is None or self._error_metadata) and self.request is not None and self._terminal:
            latest = {
                "outcome": "failure" if self._result is None else "success",
                "backend": (
                    self._result.backend
                    if self._result
                    else self.request.backend
                    if self.request
                    else "unknown"
                ),
                "support_status": self._result.support_tier if self._result else "unknown",
                **self._error_metadata,
            }
        return json.dumps(build_shareable_report(latest, failures), indent=2, sort_keys=True)

    def action_quit(self) -> None:
        self._finish_ui()
        self._cancel_event.set()
        self.exit()

    def on_unmount(self) -> None:
        self._finish_ui()
        self._cancel_event.set()
        # Give cooperative cleanup time to escalate TERM to KILL before this
        # short-lived popup process exits. Never wait forever for WSL/OS I/O.
        # Worker delivery is non-blocking, so joining here cannot deadlock it.
        if self._query_thread is not None:
            self._query_thread.join(timeout=2.0)
