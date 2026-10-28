from pathlib import Path

import pytest

from dbt_enhance.models import BackendChoiceRequired, QueryRequest, QueryResult, ResolvedContext
from dbt_enhance.ui import QueryApp


@pytest.mark.asyncio
async def test_backend_choice_pauses_only_human_time_and_pins_native_target():
    now = [100.0]
    seen = []
    context = ResolvedContext(Path("."), Path("dbt"), Path("python"), "dev", "duckdb")

    def runner(request, _cancel, _status):
        seen.append(request)
        if len(seen) == 1:
            now[0] += 0.3
            raise BackendChoiceRequired(context=context)
        return QueryResult(["value"], [[1]], "select 1", 0.1, Path("."), 10, target=request.target)

    app = QueryApp(
        QueryRequest("select 1", Path("model.sql"), backend="auto", timeout=1),
        runner=runner,
        clock=lambda: now[0],
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(1.1)
        assert len(seen) == 1
        assert not app._terminal, "human deliberation must not consume the query timeout"
        now[0] += 1000
        await pilot.press("enter")
        await pilot.pause()
        assert len(seen) == 2
        assert seen[1].backend == "worker"
        assert seen[1].target == "dev"
        assert seen[1].timeout == pytest.approx(0.7)
        assert app._result is not None


@pytest.mark.asyncio
async def test_exhausted_preflight_budget_does_not_dispatch_after_choice():
    now = [100.0]
    seen = []

    def runner(request, _cancel, _status):
        seen.append(request)
        now[0] += 2
        raise BackendChoiceRequired()

    app = QueryApp(
        QueryRequest("select 1", Path("model.sql"), backend="auto", timeout=1),
        runner=runner,
        clock=lambda: now[0],
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._terminal
        assert len(seen) == 1
        assert "timed out" in app.query_one("#status").renderable.plain
