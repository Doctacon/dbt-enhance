"""The small contract between editor transport, dbt execution, and the UI."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

# Recognized safe progress state; never carry login URLs or tokens to the UI.
SSO_STATUS = "Browser SSO requested — check your browser"
SSO_GUIDANCE = (
    "Snowflake browser SSO needs attention. The popup cannot accept a pasted redirect URL. "
    "Run dbt debug in a terminal from this project using the same dbt environment, "
    "target and profiles. Complete SSO and configure approved credential caching, "
    "then retry; see the README."
)


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
    backend: str = "process"


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
