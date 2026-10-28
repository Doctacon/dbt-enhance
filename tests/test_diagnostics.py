import json
import stat
from pathlib import Path

import pytest

from dbt_enhance.diagnostics import DiagnosticStore, build_shareable_report


def test_store_records_sanitized_latest_and_failures_privately(tmp_path: Path):
    root = tmp_path / "diagnostics"
    store = DiagnosticStore(root=root, clock=lambda: 1_000.0)
    project = tmp_path / "secret-project-name"
    status = {
        "outcome": "failure",
        "stage": "execution",
        "reason": "sql_error",
        "backend": "worker",
        "support_status": "provisional",
        "platform": "macos",
        "versions": {"tool": "0.1.2", "evil": "secret"},
        "timings": {"total": 2.5, "other": 9},
        "error": "select * from credentials; password=hunter2",
        "target": "production",
    }

    store.record_failure(project, status)

    expected = {
        "timestamp": 1_000.0,
        "outcome": "failure",
        "stage": "execution",
        "reason": "sql_error",
        "backend": "worker",
        "support_status": "provisional",
        "platform": "macos",
        "versions": {"tool": "0.1.2"},
        "timings": {"total": 2.5},
    }
    assert store.latest(project) == expected
    assert store.failures(project) == (expected,)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    files = list(root.iterdir())
    assert files
    assert all(project.name not in item.name for item in files)
    for item in files:
        if item.is_file():
            assert stat.S_IMODE(item.stat().st_mode) == 0o600
    serialized = json.dumps(build_shareable_report(store.latest(project), store.failures(project)))
    assert "secret" not in serialized
    assert "production" not in serialized
    assert "credentials" not in serialized


def test_retention_caps_failures_and_expires_the_whole_project(tmp_path: Path):
    now = [10_000.0]
    root = tmp_path / "diagnostics"
    store = DiagnosticStore(root=root, clock=lambda: now[0])
    project = tmp_path / "project"

    for index in range(25):
        now[0] += 1
        store.record_failure(
            project,
            {
                "outcome": "failure",
                "reason": "query_failed",
                "timings": {"total": index},
            },
        )

    failures = store.failures(project)
    assert len(failures) == 20
    assert failures[0]["timings"] == {"total": 5.0}
    assert failures[-1]["timings"] == {"total": 24.0}

    now[0] += 7 * 24 * 60 * 60 + 1
    assert store.latest(project) is None
    assert store.failures(project) == ()
    assert not list(root.glob("*.json"))


def test_disabled_store_is_a_noop_and_explicit_purge_still_works(tmp_path: Path):
    root = tmp_path / "diagnostics"
    project = tmp_path / "project"
    enabled = DiagnosticStore(root=root, clock=lambda: 100.0)
    enabled.record_latest(project, {"outcome": "success"})
    assert enabled.latest(project) is not None

    disabled = DiagnosticStore(root=root, clock=lambda: 100.0, enabled=False)
    disabled.record_failure(project, {"reason": "sql_error"})
    assert disabled.latest(project) is None
    assert root.exists()

    disabled.purge(project)
    assert not list(root.glob("*.json"))


def test_corrupt_files_and_symlinks_never_break_or_touch_outsiders(tmp_path: Path):
    project = tmp_path / "project"
    root = tmp_path / "diagnostics"
    store = DiagnosticStore(root=root, clock=lambda: 100.0)
    store.record_latest(project, {"outcome": "success"})
    state = next(root.glob("*.json"))
    state.write_text("not json")
    assert store.latest(project) is None

    outsider = tmp_path / "outsider"
    outsider.write_text("keep me")
    state.unlink(missing_ok=True)
    state.symlink_to(outsider)
    store.record_failure(project, {"reason": "unknown"})
    store.purge()
    assert outsider.read_text() == "keep me"
    assert state.is_symlink()

    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(tmp_path, target_is_directory=True)
    linked = DiagnosticStore(root=linked_root, clock=lambda: 100.0)
    linked.record_latest(project, {"outcome": "success"})
    linked.purge()
    assert outsider.exists()


@pytest.mark.parametrize("invalid", [{}, [], [["nested"]], 123])
def test_report_drops_malformed_enum_fields_and_unbounded_numbers(invalid):
    poisoned = {
        "outcome": "failure",
        "stage": invalid,
        "reason": invalid,
        "backend": invalid,
        "support_status": invalid,
        "platform": invalid,
        "timestamp": 10**1000,
        "timings": {"total": 10**1000},
    }
    assert build_shareable_report(poisoned, [poisoned]) == {
        "schema": 1,
        "latest": {"outcome": "failure"},
        "failures": [{"outcome": "failure"}],
    }
    assert build_shareable_report({"outcome": invalid}, []) == {
        "schema": 1,
        "latest": None,
        "failures": [],
    }


def test_report_sanitizes_untrusted_data_without_relying_on_store():
    poisoned = {
        "timestamp": float("nan"),
        "outcome": "success",
        "stage": "made-up",
        "reason": "password=secret",
        "backend": "custom-command",
        "support_tier": "provisional",
        "platform": "/Users/alice",
        "versions": {
            "tool": "1.2.3",
            "python": "3.12.7; token=secret",
            "private-package": "7.0",
        },
        "timings": {"total": float("inf"), "execution": 0.25, "sql": "select secret"},
        "nested": {"sql": "select secret"},
    }
    report = build_shareable_report(poisoned, [poisoned])

    assert report == {
        "schema": 1,
        "latest": {
            "outcome": "success",
            "support_status": "provisional",
            "versions": {"tool": "1.2.3"},
            "timings": {"execution": 0.25},
        },
        "failures": [
            {
                "outcome": "failure",
                "support_status": "provisional",
                "versions": {"tool": "1.2.3"},
                "timings": {"execution": 0.25},
            }
        ],
    }
    assert "secret" not in json.dumps(report)


def test_default_root_creates_private_application_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state_home = tmp_path / "new-state-home"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    store = DiagnosticStore(clock=lambda: 100.0)

    store.record_latest(tmp_path / "project", {"outcome": "success"})

    assert store.root == state_home / "dbt-enhance/diagnostics"
    for directory in (state_home, state_home / "dbt-enhance", store.root):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
