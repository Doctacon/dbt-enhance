"""Headless local inspection timings; no dbt, terminal, clipboard, or warehouse calls.

Run: uv run --no-sync python docs/benchmarks/viewer.py
These include Textual's headless event/layout settling, not real terminal latency.
They are observations, not performance budgets or certification evidence.
"""

from __future__ import annotations

import asyncio
import json
import platform
import statistics
import time
from importlib.metadata import version
from pathlib import Path

from dbt_enhance.models import QueryResult
from dbt_enhance.preview import ColumnFilter
from dbt_enhance.ui import QueryApp


def no_query(*_args):
    raise AssertionError("Inspection must not execute SQL")


async def sample(rows: int) -> dict[str, float]:
    result = QueryResult(
        [f"c{index}" for index in range(14)],
        [[row + column for column in range(14)] for row in range(rows)],
        "-- synthetic fetched result; no statement executed",
        0.0,
        Path("."),
        rows,
        column_types=["number"] * 14,
    )
    app = QueryApp(result=result, runner=no_query)
    app.theme = "dbt-enhance-dark"
    timings = {}
    started = time.perf_counter()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        timings["initial_render"] = time.perf_counter() - started
        started = time.perf_counter()
        assert app.sort_column(0, "desc")
        await pilot.pause()
        timings["sort_and_repaint"] = time.perf_counter() - started
        started = time.perf_counter()
        assert app.apply_filter(ColumnFilter(0, ">=", rows // 2))
        await pilot.pause()
        timings["filter_and_repaint"] = time.perf_counter() - started
        started = time.perf_counter()
        assert app.search("table", str(rows - 1))
        await pilot.pause()
        timings["find_and_repaint"] = time.perf_counter() - started
        assert len(result.rows) == rows and result.rows[0][0] == 0
        assert not app.clipboard
    return {key: round(value, 4) for key, value in timings.items()}


async def main() -> None:
    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "textual": version("textual"),
        "viewport": [100, 30],
        "theme": "dbt-enhance-dark",
        "columns": 14,
        "samples_per_workload": 3,
        "workloads": {},
    }
    for rows in (500, 10000):
        samples = [await sample(rows) for _ in range(3)]
        report["workloads"][str(rows)] = {
            "seconds": samples,
            "median_seconds": {
                key: round(statistics.median(item[key] for item in samples), 4)
                for key in samples[0]
            },
        }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
