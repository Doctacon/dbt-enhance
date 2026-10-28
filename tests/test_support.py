import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from dbt_enhance import support
from dbt_enhance.models import EnhanceError, ResolvedContext
from dbt_enhance.support import (
    ReleasePolicy,
    classify_backend,
    load_support_matrix,
    select_backend,
    validate_default_transition,
)


def test_current_default_remains_explicitly_provisional_until_release_transition():
    choice = select_backend(None)
    assert choice.backend == "worker"
    assert choice.tier == "provisional"
    assert choice.origin == "legacy_builtin"


def test_packaged_matrix_is_auditable_and_has_no_current_certifications():
    matrix = load_support_matrix()

    assert matrix.schema == 1
    assert matrix.certifications == ()
    assert matrix.documentation_notes


def _context(
    *, core: str = "1.10.3", adapter: str = "duckdb", adapter_version: str = "1.10.0"
) -> ResolvedContext:
    return ResolvedContext(
        project_dir=Path("/project"),
        dbt_executable=Path("/project/.venv/bin/dbt"),
        project_python=Path("/project/.venv/bin/python"),
        target="dev",
        adapter=adapter,
        versions={
            "python": "3.12.8",
            "dbt_core": core,
            f"dbt_{adapter}": adapter_version,
        },
    )


def test_worker_admission_is_provisional_but_unsupported_versions_are_not():
    assert classify_backend(_context(), "worker") == "provisional"
    hyphenated = _context()
    hyphenated.versions.clear()
    hyphenated.versions.update({"python": "3.12.8", "dbt-core": "1.10.3", "dbt-duckdb": "1.10.0"})
    assert classify_backend(hyphenated, "worker") == "provisional"
    assert classify_backend(_context(core="1.12.1", adapter="snowflake"), "worker") == (
        "provisional"
    )
    assert classify_backend(_context(core="1.11.0"), "worker") == "unsupported"
    assert classify_backend(_context(adapter="postgres"), "worker") == "unsupported"
    assert classify_backend(None, "worker") == "provisional"
    assert classify_backend(_context(core="1.11.0"), "process") == "best-effort"


def _matrix_file(tmp_path: Path, certifications: list[dict[str, object]]) -> Path:
    path = tmp_path / "support.json"
    path.write_text(json.dumps({"schema": 1, "certifications": certifications}))
    return path


def _certificate(**overrides: object) -> dict[str, object]:
    certificate: dict[str, object] = {
        "platform": "linux",
        "architecture": "x86_64",
        "os_reference": "6.8.0-exact",
        "python": "3.12.8",
        "dbt_core": "1.10.3",
        "adapter": "duckdb",
        "adapter_version": "1.10.0",
        "backend": "worker",
        "release": "0.1.2",
        "automation_evidence": ["release:v0.1.2:ci:worker-duckdb"],
        "real_environment_evidence": ["release:v0.1.2:env:linux-x86_64"],
    }
    certificate.update(overrides)
    return certificate


def test_certification_requires_an_exact_evidence_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        support,
        "_runtime_identity",
        lambda: ("linux", "x86_64", "6.8.0-exact", "0.1.2"),
    )
    matrix = load_support_matrix(_matrix_file(tmp_path, [_certificate()]))

    assert classify_backend(_context(), "worker", matrix=matrix) == "certified"
    assert classify_backend(_context(core="1.10.4"), "worker", matrix=matrix) == ("provisional")
    assert classify_backend(_context(), "process", matrix=matrix) == "best-effort"


def test_auto_is_safe_and_explicit_backends_never_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        support,
        "_runtime_identity",
        lambda: ("linux", "x86_64", "6.8.0-exact", "0.1.2"),
    )
    certified = load_support_matrix(_matrix_file(tmp_path, [_certificate()]))
    empty = load_support_matrix(_matrix_file(tmp_path, []))

    assert select_backend("auto", _context(), matrix=certified) == support.BackendChoice(
        "worker", "certified", "auto"
    )
    assert select_backend("auto", _context(), matrix=empty) == support.BackendChoice(
        None,
        "provisional",
        "choice_required",
        choices=("worker", "process"),
    )
    unsupported = _context(core="1.11.0")
    assert select_backend("auto", unsupported, matrix=empty).choices == ("process",)
    assert select_backend("auto", None, matrix=empty).choices == ("process",)
    assert select_backend("worker", unsupported, matrix=empty) == support.BackendChoice(
        "worker", "unsupported", "explicit"
    )
    assert select_backend("process", _context(), matrix=certified) == support.BackendChoice(
        "process", "best-effort", "explicit"
    )
    assert select_backend(
        None,
        _context(),
        release_policy=ReleasePolicy(certified_default=True),
        matrix=certified,
    ) == support.BackendChoice("worker", "certified", "certified_default")


def test_default_transition_requires_notice_time_and_two_subsequent_tags():
    notice_date = date(2025, 1, 1)
    initial = ReleasePolicy()
    notice = ReleasePolicy(
        notice_tag="v0.2.0",
        notice_date=notice_date,
        release_date=notice_date,
    )
    validate_default_transition(initial, initial)
    validate_default_transition(initial, notice)

    promoted = ReleasePolicy(
        notice_tag="v0.2.0",
        notice_date=notice_date,
        subsequent_tags=("v0.2.1", "v0.2.2"),
        release_date=notice_date + timedelta(days=30),
        certified_default=True,
    )
    validate_default_transition(notice, promoted)

    invalid = [
        ReleasePolicy(certified_default=True),
        ReleasePolicy(
            notice_tag="v0.2.0",
            notice_date=notice_date,
            subsequent_tags=("v0.2.1",),
            release_date=notice_date + timedelta(days=30),
            certified_default=True,
        ),
        ReleasePolicy(
            notice_tag="v0.2.0",
            notice_date=notice_date,
            subsequent_tags=("v0.2.1", "v0.2.1"),
            release_date=notice_date + timedelta(days=30),
            certified_default=True,
        ),
        ReleasePolicy(
            notice_tag="v0.2.0",
            notice_date=notice_date,
            subsequent_tags=("v0.2.1", "v0.2.2"),
            release_date=notice_date + timedelta(days=29),
            certified_default=True,
        ),
        ReleasePolicy(notice_tag="v0.2.0"),
    ]
    for candidate in invalid:
        with pytest.raises(EnhanceError) as caught:
            validate_default_transition(notice, candidate)
        assert caught.value.code == "configuration_invalid"
        assert caught.value.stage == "preflight"


def test_matrix_loader_rejects_duplicate_json_keys(tmp_path: Path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema":1,"schema":1,"certifications":[]}')

    with pytest.raises(EnhanceError) as caught:
        load_support_matrix(path)

    assert caught.value.code == "configuration_invalid"
    assert caught.value.stage == "preflight"


@pytest.mark.parametrize(
    "certificate",
    [
        {key: value for key, value in _certificate().items() if key != "os_reference"},
        _certificate(automation_evidence=[]),
        _certificate(real_environment_evidence=[]),
        _certificate(platform="windows"),
        _certificate(extra="not allowed"),
        [_certificate(), _certificate()],
    ],
)
def test_matrix_loader_strictly_rejects_incomplete_or_unknown_rows(
    tmp_path: Path, certificate: dict[str, object] | list[dict[str, object]]
):
    rows = certificate if isinstance(certificate, list) else [certificate]
    with pytest.raises(EnhanceError) as caught:
        load_support_matrix(_matrix_file(tmp_path, rows))
    assert caught.value.code == "configuration_invalid"
    assert caught.value.stage == "preflight"
