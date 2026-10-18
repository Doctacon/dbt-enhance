"""The small contract between editor transport, dbt execution, and the UI."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event


class EnhanceError(Exception):
    """An actionable error that can be displayed without a Python traceback."""


@dataclass(frozen=True)
class QueryRequest:
    sql: str
    file: Path
    limit: int = 500
    target: str | None = None
    profiles_dir: Path | None = None
    dbt: Path | None = None
    timeout: float = 120.0


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[list[object]]
    compiled_sql: str
    elapsed: float
    project_dir: Path
    limit: int
    target: str | None = None
    warnings: list[str] = field(default_factory=list)


# A runner receives a cancellation event and a thread-safe progress callback.
QueryRunner = Callable[[QueryRequest, Event, Callable[[str], None]], QueryResult]
