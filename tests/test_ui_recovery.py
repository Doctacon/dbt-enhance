import json
from pathlib import Path
from threading import Event

import pytest
from textual.widgets import DataTable, Static

from dbt_enhance.diagnostics import DiagnosticStore
from dbt_enhance.models import EnhanceError, QueryRequest, QueryResult
from dbt_enhance.ui import QueryApp


@pytest.mark.asyncio
async def test_query_can_finish_while_action_menu_is_open():
    release = Event()
    result = QueryResult(["value"], [[1]], "select 1", 0.1, Path("."), 10)

    def runner(_request, cancel, on_status):
        while not release.wait(0.01):
            if cancel.is_set():
                return result
        on_status("Query completed")
        return result

    app = QueryApp(QueryRequest("select 1", Path("model.sql")), runner=runner)
    async with app.run_test() as pilot:
        table = app.query_one("#results", DataTable)
        try:
            await pilot.press("ctrl+p")
            release.set()
            await pilot.pause()
            assert table.row_count == 1
            assert app._result is result
        finally:
            release.set()


@pytest.mark.asyncio
async def test_support_report_is_reviewed_then_copies_only_the_displayed_metadata(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("dbt_enhance.ui.platform", "linux")
    monkeypatch.delenv("TMUX", raising=False)
    (tmp_path / "dbt_project.yml").write_text("name: example")
    source = tmp_path / "models" / "model.sql"
    source.parent.mkdir()
    source.write_text("select 'SECRET SQL'")
    store = DiagnosticStore()
    store.record_failure(tmp_path, {"outcome": "timeout", "reason": "timeout", "sql": "SECRET SQL"})
    store.record_latest(
        tmp_path, {"outcome": "failure", "reason": "query_failed", "error": "SECRET ERROR"}
    )
    calls = []

    def runner(*args):
        calls.append(args)
        raise EnhanceError("SECRET ERROR from SECRET SQL")

    app = QueryApp(QueryRequest("select 'SECRET SQL'", source), runner=runner)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p", *"support report", "enter")
        report = app.screen.query_one("#review-body", Static).renderable.plain
        assert "SECRET" not in report
        assert json.loads(report)["failures"][0]["reason"] == "timeout"
        assert not app.clipboard
        await pilot.press("Y")
        await pilot.pause()
        assert app.clipboard == report
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_native_error_stays_local_and_opted_out_report_keeps_cancellation_certainty(
    monkeypatch,
):
    monkeypatch.setattr("dbt_enhance.ui.platform", "linux")
    monkeypatch.delenv("TMUX", raising=False)
    detail = "SECRET " + "native detail\n" * 800 + "ERROR END"

    def runner(*_args):
        error = EnhanceError(detail, code="cancelled", stage="execution")
        error.cancellation = "requested_unverified"
        raise error

    app = QueryApp(QueryRequest("SECRET SQL", Path("model.sql"), diagnostics=False), runner=runner)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p", *"native error", "enter")
        assert app.screen.query_one("#review-body", Static).renderable.plain == detail
        await pilot.press("Y")
        assert not app.clipboard, "native-error review must not copy hidden SQL or native errors"
        await pilot.press("escape", "ctrl+p", *"support report", "enter")
        report = app.screen.query_one("#review-body", Static).renderable.plain
        assert "SECRET" not in report
        latest = json.loads(report)["latest"]
        assert latest["outcome"] == "cancelled"
        assert latest["cancellation"] == "requested_unverified"
        assert not json.loads(report)["failures"]


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_worker_reset_requires_confirmation_and_never_replays_the_query(
    tmp_path, monkeypatch, active
):
    from dbt_enhance import worker

    calls = []
    monkeypatch.setattr(worker, "inspect_worker", lambda project: {"active": active})
    monkeypatch.setattr(
        worker,
        "reset",
        lambda project, *, allow_active=False: calls.append((project, allow_active)) or True,
    )
    result = QueryResult(
        ["value"],
        [[42]],
        "select 42",
        0.1,
        tmp_path,
        10,
        backend="worker",
        target_verified=True,
        session_state="ready",
    )

    def runner(*_args):
        pytest.fail("worker recovery must never rerun a query")

    app = QueryApp(QueryRequest("select 42", tmp_path / "model.sql"), result=result, runner=runner)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("ctrl+p", *"reset worker", "enter")
        await pilot.pause()
        assert (
            "not limited to this preview or target"
            in app.screen.query_one("#review-body", Static).renderable.plain
        )
        assert not calls
        await pilot.press("enter")  # Cancel is the default, not destructive confirmation.
        assert not calls
        await pilot.press("ctrl+p", *"reset worker", "enter")
        await pilot.pause()
        await pilot.press("tab", "enter")
        await pilot.pause()
        assert calls == [(tmp_path, active)]
        assert app.query_one("#results", DataTable).row_count == 1
        assert "disconnected" in app.query_one("#run-context", Static).renderable.plain


@pytest.mark.asyncio
async def test_watchdog_report_does_not_reuse_an_older_success_during_cleanup(tmp_path):
    (tmp_path / "dbt_project.yml").write_text("name: example")
    DiagnosticStore().record_latest(tmp_path, {"outcome": "success", "backend": "worker"})
    release = Event()

    def runner(*_args):
        release.wait(5)
        return QueryResult(["value"], [[1]], "select 1", 0.1, tmp_path, 10)

    app = QueryApp(QueryRequest("select 1", tmp_path / "model.sql", timeout=0.05), runner=runner)
    async with app.run_test() as pilot:
        try:
            await pilot.pause(0.1)
            latest = json.loads(app.review_diagnostic_report())["latest"]
            assert latest["outcome"] == "timeout"
            assert latest["cancellation"] == "requested_unverified"
        finally:
            release.set()
