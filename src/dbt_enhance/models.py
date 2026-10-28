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
    """An actionable local error; only its fixed code/stage belong in diagnostics."""

    def __init__(self, message: str, *, code: str = "query_failed", stage: str = "execution"):
        super().__init__(message)
        self.code = code
        self.stage = stage


class BackendChoiceRequired(EnhanceError):
    """Raised only before execution; choosing is not retrying a failed query."""

    def __init__(
        self,
        choices: tuple[str, ...] = ("worker", "process"),
        *,
        context: "ResolvedContext | None" = None,
    ):
        descriptions = {
            "worker": "--backend worker (provisional)",
            "process": "--backend process (isolated, best-effort)",
        }
        guidance = " or ".join(descriptions[choice] for choice in choices)
        super().__init__(
            f"No certified default is available. Explicitly choose {guidance}. "
            "No selected SQL has been compiled or executed.",
            code="backend_choice_required",
            stage="preflight",
        )
        self.choices = choices
        self.context = context


class QueryStatus(str):
    """A backwards-compatible status string with optional proven context."""

    def __new__(
        cls,
        message: str,
        *,
        stage: str = "execution",
        target: str | None = None,
        target_verified: bool = False,
        backend: str | None = None,
        support_tier: str = "provisional",
    ):
        value = super().__new__(cls, message)
        value.stage = stage
        value.target = target
        value.target_verified = target_verified
        value.backend = backend
        value.support_tier = support_tier
        return value


@dataclass(frozen=True)
class QueryRequest:
    sql: str
    file: Path
    limit: int = 500
    target: str | None = None
    profiles_dir: Path | None = None
    dbt: Path | None = None
    timeout: float = 120.0
    backend: str = "worker"
    # None disables the policy; an empty tuple explicitly denies every target.
    allowed_targets: tuple[str, ...] | None = None
    worker_idle_seconds: float = 600.0
    diagnostics: bool = True
    theme: str = "auto"
    backend_origin: str = "legacy_builtin"


@dataclass(frozen=True)
class ResolvedContext:
    """Native configuration metadata, never credentials or a guessed target."""

    project_dir: Path
    dbt_executable: Path
    project_python: Path
    target: str
    adapter: str
    versions: dict[str, str] = field(default_factory=dict)


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
    # Value kinds describe dbt's returned values, not warehouse SQL column types.
    column_types: list[str] = field(default_factory=list)
    target_verified: bool = False
    backend: str | None = None
    adapter: str | None = None
    support_tier: str = "provisional"
    session_state: str | None = None
    versions: dict[str, str] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    sql_provenance: str | None = None


# A runner receives a cancellation event and a thread-safe progress callback.
QueryRunner = Callable[[QueryRequest, Event, Callable[[str], None]], QueryResult]
