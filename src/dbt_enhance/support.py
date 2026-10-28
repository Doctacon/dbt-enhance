"""Evidence-backed support policy; admission is not certification."""

from __future__ import annotations

import json
import os
import platform as host_platform
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .models import EnhanceError, ResolvedContext

_MATRIX_PATH = Path(__file__).with_name("support-matrix.json")
_MAX_MATRIX_BYTES = 256 * 1024
_STABLE_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)+$")
_ADMITTED_CORE = re.compile(r"^1\.(?:10|12)\.[0-9]+$")
_VERSION_ALIASES = {
    "dbt_core": "dbt-core",
    "dbt_duckdb": "dbt-duckdb",
    "dbt_snowflake": "dbt-snowflake",
    "dbt_adapter": "dbt-adapter",
}


@dataclass(frozen=True)
class ReleasePolicy:
    notice_tag: str | None = None
    notice_date: date | None = None
    subsequent_tags: tuple[str, ...] = ()
    release_date: date | None = None
    certified_default: bool = False


_CURRENT_RELEASE_POLICY = ReleasePolicy()


@dataclass(frozen=True)
class BackendChoice:
    backend: str | None
    tier: str
    origin: str
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Certification:
    """One exact release-artifact evidence scope.

    ``os_reference`` is the exact native ``platform.release()`` value. Architecture is
    normalized to ``x86_64`` or ``arm64``; ``release`` is the installed dbt-enhance
    distribution version. Both evidence tuples must reference release-owned records.
    """

    platform: str
    architecture: str
    os_reference: str
    python: str
    dbt_core: str
    adapter: str
    adapter_version: str
    backend: str
    release: str
    automation_evidence: tuple[str, ...]
    real_environment_evidence: tuple[str, ...]


@dataclass(frozen=True)
class SupportMatrix:
    schema: int
    certifications: tuple[Certification, ...]
    goals: tuple[str, ...] = ()
    documentation_notes: tuple[str, ...] = ()


def load_support_matrix(path: Path | None = None) -> SupportMatrix:
    """Load a strict, immutable local matrix; no installed version creates a row."""

    candidate = _MATRIX_PATH if path is None else path
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise _invalid("The support matrix could not be read.") from exc
    if len(raw) > _MAX_MATRIX_BYTES:
        raise _invalid("The support matrix exceeds its size limit.")
    try:
        document = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _invalid("The support matrix is not valid strict JSON.") from exc
    if not isinstance(document, dict):
        raise _invalid("The support matrix must be a JSON object.")
    _strict_keys(
        document,
        required={"schema", "certifications"},
        optional={"goals", "documentation_notes"},
        context="support matrix",
    )
    schema = document["schema"]
    if type(schema) is not int or schema != 1:
        raise _invalid("The support matrix schema must be 1.")
    raw_certifications = document["certifications"]
    if not isinstance(raw_certifications, list):
        raise _invalid("Support matrix certifications must be an array.")
    certifications = tuple(_load_certification(value) for value in raw_certifications)
    identities = {
        (
            row.platform,
            row.architecture,
            row.os_reference,
            row.python,
            row.dbt_core,
            row.adapter,
            row.adapter_version,
            row.backend,
            row.release,
        )
        for row in certifications
    }
    if len(identities) != len(certifications):
        raise _invalid("The support matrix contains a duplicate certification.")
    goals = _string_array(document.get("goals", []), "support matrix goals")
    notes = _string_array(
        document.get("documentation_notes", []), "support matrix documentation notes"
    )
    return SupportMatrix(1, certifications, goals, notes)


def select_backend(
    explicit: str | None,
    context: ResolvedContext | None = None,
    *,
    release_policy: ReleasePolicy = _CURRENT_RELEASE_POLICY,
    matrix: SupportMatrix | None = None,
) -> BackendChoice:
    """Select without fallback; auto/default promotion requires an exact certificate."""

    if explicit is not None and not isinstance(explicit, str):
        raise _invalid("Explicit backend must be auto, worker, or process.")
    if explicit not in {None, "auto", "worker", "process"}:
        raise _invalid("Explicit backend must be auto, worker, or process.")
    if not isinstance(release_policy, ReleasePolicy):
        raise _invalid("Release policy must use the validated policy type.")
    if explicit in {"worker", "process"}:
        return BackendChoice(
            explicit,
            classify_backend(context, explicit, matrix=matrix),
            "explicit",
        )
    automatic = explicit == "auto" or release_policy.certified_default
    if not automatic:
        return BackendChoice(
            "worker",
            classify_backend(context, "worker", matrix=matrix),
            "legacy_builtin",
        )
    worker_tier = classify_backend(context, "worker", matrix=matrix)
    if worker_tier == "certified":
        origin = "auto" if explicit == "auto" else "certified_default"
        return BackendChoice("worker", "certified", origin)
    choices = (
        ("worker", "process")
        if context is not None and worker_tier == "provisional"
        else ("process",)
    )
    return BackendChoice(None, "provisional", "choice_required", choices)


def classify_backend(
    context: ResolvedContext | None,
    backend: str,
    *,
    matrix: SupportMatrix | None = None,
) -> str:
    """Classify one backend without treating technical admission as certification."""

    if not isinstance(backend, str) or backend not in {"worker", "process"}:
        raise _invalid("Backend must be worker or process.")
    selected_matrix = _PACKAGED_MATRIX if matrix is None else matrix
    if not isinstance(selected_matrix, SupportMatrix):
        raise _invalid("Support matrix must be loaded and validated.")
    if context is None:
        return "provisional" if backend == "worker" else "best-effort"
    if backend == "worker" and not _worker_admitted(context):
        return "unsupported"
    if any(_certification_matches(row, context, backend) for row in selected_matrix.certifications):
        return "certified"
    return "provisional" if backend == "worker" else "best-effort"


def validate_default_transition(previous: ReleasePolicy, candidate: ReleasePolicy) -> None:
    """Validate supplied release metadata; elapsed time alone never changes a default."""

    _validate_release_policy(previous)
    _validate_release_policy(candidate)
    if previous == candidate:
        return
    if previous.certified_default:
        raise _invalid("An established certified default policy cannot be rewritten.")

    if previous.notice_tag is None:
        if candidate.certified_default:
            raise _invalid("Certified default promotion requires a prior tagged notice.")
        if candidate.notice_tag is not None and candidate.subsequent_tags:
            raise _invalid("Subsequent releases cannot be recorded in the notice transition.")
        return

    if candidate.notice_tag != previous.notice_tag or candidate.notice_date != previous.notice_date:
        raise _invalid("The published default-change notice cannot be replaced or removed.")
    if candidate.subsequent_tags[: len(previous.subsequent_tags)] != previous.subsequent_tags:
        raise _invalid("Previously recorded subsequent release tags cannot be rewritten.")
    if not candidate.certified_default:
        return

    assert previous.notice_date is not None
    if candidate.release_date is None:
        raise _invalid("Certified default promotion requires a candidate release date.")
    if candidate.release_date < previous.notice_date + timedelta(days=30):
        raise _invalid("Certified default promotion requires at least 30 days of notice.")
    if len(candidate.subsequent_tags) < 2:
        raise _invalid("Certified default promotion requires two subsequent tagged releases.")


def _validate_release_policy(policy: ReleasePolicy) -> None:
    if not isinstance(policy, ReleasePolicy):
        raise _invalid("Release policy must use the validated policy type.")
    if not isinstance(policy.certified_default, bool):
        raise _invalid("Release policy certified_default must be boolean.")
    if (policy.notice_tag is None) != (policy.notice_date is None):
        raise _invalid("Release notice tag and date must be supplied together.")
    if policy.notice_date is not None and type(policy.notice_date) is not date:
        raise _invalid("Release notice date must be a calendar date.")
    if policy.release_date is not None and type(policy.release_date) is not date:
        raise _invalid("Candidate release date must be a calendar date.")
    if not isinstance(policy.subsequent_tags, tuple):
        raise _invalid("Subsequent release tags must be an immutable tuple.")
    tags = tuple(_release_tag(tag) for tag in policy.subsequent_tags)
    if len(set(tags)) != len(tags):
        raise _invalid("Subsequent release tags must be distinct.")
    if policy.notice_tag is not None:
        notice_tag = _release_tag(policy.notice_tag)
        if notice_tag in tags:
            raise _invalid("The notice tag is not a subsequent release tag.")
        assert policy.notice_date is not None
        if policy.release_date is not None and policy.release_date < policy.notice_date:
            raise _invalid("Candidate release date cannot precede the notice date.")
    elif tags:
        raise _invalid("Subsequent release tags require a prior notice.")
    if policy.certified_default and (
        policy.notice_tag is None or policy.release_date is None or len(tags) < 2
    ):
        raise _invalid("Certified default policy metadata is incomplete.")


def _release_tag(value: object) -> str:
    tag = _string(value, "release tag")
    if any(character.isspace() for character in tag):
        raise _invalid("Release tags must not contain whitespace.")
    return tag


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _load_certification(value: object) -> Certification:
    if not isinstance(value, dict):
        raise _invalid("Each support certification must be an object.")
    keys = {
        "platform",
        "architecture",
        "os_reference",
        "python",
        "dbt_core",
        "adapter",
        "adapter_version",
        "backend",
        "release",
        "automation_evidence",
        "real_environment_evidence",
    }
    _strict_keys(value, required=keys, optional=set(), context="support certification")
    strings = {
        key: _string(value[key], f"certification {key}")
        for key in keys
        if key not in {"automation_evidence", "real_environment_evidence"}
    }
    if strings["platform"] not in {"macos", "linux", "wsl2"}:
        raise _invalid("Certification platform must be macos, linux, or wsl2.")
    if strings["architecture"] not in {"x86_64", "arm64"}:
        raise _invalid("Certification architecture must be x86_64 or arm64.")
    if strings["backend"] not in {"worker", "process"}:
        raise _invalid("Certification backend must be worker or process.")
    if strings["adapter"] not in {"duckdb", "snowflake"}:
        raise _invalid("Certification adapter must be duckdb or snowflake.")
    for key in ("python", "dbt_core", "adapter_version", "release"):
        if _STABLE_VERSION.fullmatch(strings[key]) is None:
            raise _invalid(f"Certification {key} must be an exact stable version.")
    return Certification(
        **strings,
        automation_evidence=_nonempty_string_array(
            value["automation_evidence"], "automation evidence"
        ),
        real_environment_evidence=_nonempty_string_array(
            value["real_environment_evidence"], "real-environment evidence"
        ),
    )


def _worker_admitted(context: ResolvedContext) -> bool:
    core = _context_version(context, "dbt-core")
    adapter = context.adapter.lower() if isinstance(context.adapter, str) else ""
    adapter_version = _context_version(context, f"dbt-{adapter}")
    if adapter_version is None:
        adapter_version = _context_version(context, "dbt-adapter")
    return (
        core is not None
        and _ADMITTED_CORE.fullmatch(core) is not None
        and adapter in {"duckdb", "snowflake"}
        and adapter_version is not None
        and _STABLE_VERSION.fullmatch(adapter_version) is not None
    )


def _certification_matches(row: Certification, context: ResolvedContext, backend: str) -> bool:
    identity = _runtime_identity()
    if identity is None:
        return False
    platform_category, architecture, os_reference, release = identity
    adapter = context.adapter.lower() if isinstance(context.adapter, str) else ""
    return (
        row.platform == platform_category
        and row.architecture == architecture
        and row.os_reference == os_reference
        and row.python == _context_version(context, "python")
        and row.dbt_core == _context_version(context, "dbt-core")
        and row.adapter == adapter
        and row.adapter_version
        == (_context_version(context, f"dbt-{adapter}") or _context_version(context, "dbt-adapter"))
        and row.backend == backend
        and row.release == release
    )


def _context_version(context: ResolvedContext, canonical: str) -> str | None:
    value = context.versions.get(canonical)
    if value is None:
        for alias, equivalent in _VERSION_ALIASES.items():
            if equivalent == canonical:
                value = context.versions.get(alias)
                if value is not None:
                    break
    return value if isinstance(value, str) else None


def _runtime_identity() -> tuple[str, str, str, str] | None:
    release = host_platform.release()
    if sys.platform == "darwin":
        category = "macos"
    elif sys.platform.startswith("linux"):
        lowered = release.lower()
        category = "wsl2" if "microsoft" in lowered and "wsl2" in lowered else "linux"
    else:
        return None
    machine = host_platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        architecture = "x86_64"
    elif machine in {"arm64", "aarch64"}:
        architecture = "arm64"
    else:
        return None
    try:
        tool_release = version("dbt-enhance")
    except PackageNotFoundError:
        return None
    # WSL_INTEROP is corroborating only; the kernel release remains the exact scope.
    if category == "wsl2" and not os.environ.get("WSL_INTEROP"):
        return None
    return category, architecture, release, tool_release


def _strict_keys(
    value: dict[str, object],
    *,
    required: set[str],
    optional: set[str],
    context: str,
) -> None:
    keys = set(value)
    missing = sorted(required - keys)
    unknown = sorted(keys - required - optional)
    if missing:
        raise _invalid(f"Missing required field {missing[0]!r} in {context}.")
    if unknown:
        raise _invalid(f"Unknown field {unknown[0]!r} in {context}.")


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise _invalid(f"{context} must be a nonempty bounded string.")
    if any(character in value for character in "\0\r\n"):
        raise _invalid(f"{context} must not contain control line breaks.")
    return value


def _string_array(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _invalid(f"{context} must be an array of strings.")
    return tuple(_string(item, context) for item in value)


def _nonempty_string_array(value: object, context: str) -> tuple[str, ...]:
    result = _string_array(value, context)
    if not result:
        raise _invalid(f"{context} must contain at least one reference.")
    if len(set(result)) != len(result):
        raise _invalid(f"{context} must not contain duplicate references.")
    return result


def _invalid(message: str) -> EnhanceError:
    return EnhanceError(message, code="configuration_invalid", stage="preflight")


_PACKAGED_MATRIX = load_support_matrix()
