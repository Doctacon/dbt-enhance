import json
import shlex
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import tomlkit

from dbt_enhance.helix import install_config
from dbt_enhance.lifecycle import (
    InstallationState,
    InstallProvenance,
    apply_remove,
    apply_rollback,
    apply_update,
    load_state,
    preview_remove,
    preview_rollback,
    preview_update,
    record_install,
)
from dbt_enhance.models import EnhanceError

OLD_COMMIT = "1" * 40
NEW_COMMIT = "2" * 40


def provenance(tmp_path: Path, *, kind="git") -> InstallProvenance:
    helix = tmp_path / "helix.toml"
    launcher = tmp_path / "bin" / "dbt-enhance"
    launcher.parent.mkdir(exist_ok=True)
    launcher.touch()
    source = f"git+https://example.com/dbt-enhance.git@{OLD_COMMIT}"
    identity = OLD_COMMIT
    if kind == "legacy":
        source = "dbt-enhance==0.1.0"
        identity = source
    return InstallProvenance(
        source=source,
        kind=kind,
        identity=identity,
        version="0.1.0",
        launcher=str(launcher),
        helix_config=str(helix),
        helix_launcher=(str(launcher),),
    )


class CandidateRunner:
    def __init__(self, *, valid=True, version="0.2.0", bridge_format=2):
        self.commands = []
        self.valid = valid
        self.version = version
        self.bridge_format = bridge_format
        self.schema = 1

    def __call__(self, command, **_kwargs):
        self.commands.append(command)
        if command[:2] == ["git", "ls-remote"]:
            return subprocess.CompletedProcess(command, 0, f"{NEW_COMMIT}\trefs/tags/v0.2.0\n", "")
        if "validate" in command:
            if self.valid is True:
                report = {"valid": True, "schema": self.schema, "version": self.version}
                if self.bridge_format is not None:
                    report["helix_bridge_format"] = self.bridge_format
                return subprocess.CompletedProcess(command, 0, json.dumps(report), "")
            if self.valid is None:
                return subprocess.CompletedProcess(command, 0, "{}", "")
            return subprocess.CompletedProcess(command, 2, "", "unsupported schema")
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, self.version + "\n", "")
        if command[:3] == ["uv", "tool", "install"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["uv", "tool", "uninstall"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)


def test_private_manifest_permissions_and_symlink_refusal(tmp_path):
    root = tmp_path / "state"
    state = InstallationState(current=provenance(tmp_path))
    record_install(state, state_root=root)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "manifest.json").stat().st_mode) == 0o600
    assert load_state(state_root=root) == state

    outsider = tmp_path / "outsider"
    outsider.write_text("keep")
    (root / "manifest.json").unlink()
    (root / "manifest.json").symlink_to(outsider)
    with pytest.raises((EnhanceError, OSError)):
        load_state(state_root=root)
    assert outsider.read_text() == "keep"


def test_update_resolves_tag_validates_before_replacement_and_preserves_settings(tmp_path):
    root = tmp_path / "state"
    current = provenance(tmp_path)
    record_install(InstallationState(current=current), state_root=root)
    settings = tmp_path / "settings.toml"
    settings.write_text(
        'schema = 1\n[[safety.project_targets]]\nproject = "/private/project"\n'
        'targets = ["production"]\n'
    )
    original = settings.read_bytes()
    runner = CandidateRunner()

    plan = preview_update(
        "git+https://example.com/dbt-enhance.git@v0.2.0",
        settings_config=settings,
        runner=runner,
        state_root=root,
    )
    assert plan.candidate is not None
    assert plan.candidate.source.endswith("@" + NEW_COMMIT)
    assert all(command[:3] != ["uv", "tool", "install"] for command in runner.commands)
    assert runner.commands[1] == [
        "uvx",
        "--isolated",
        "--from",
        plan.candidate.source,
        "dbt-enhance",
        "config",
        "validate",
        "--config",
        str(settings),
        "--json",
    ]

    result = apply_update(plan, runner=runner)
    assert result.ok
    assert settings.read_bytes() == original
    updated = load_state(state_root=root)
    assert updated.current == plan.candidate
    assert updated.prior == current
    assert ["uv", "tool", "install", "--force", plan.candidate.source] in runner.commands


def test_package_update_blocks_exact_legacy_unsafe_mapping(tmp_path):
    root = tmp_path / "state"
    current = provenance(tmp_path)
    delimiter = "DBT_ENHANCE_FILE_" + "1" * 32
    legacy = (
        f":pipe-to IFS= read -r dbt_enhance_file <<'{delimiter}'\n"
        f"%{{buffer_name}}\n{delimiter}\n"
        f'{shlex.join(current.helix_launcher)} popup --file "$dbt_enhance_file"'
    )
    document = tomlkit.document()
    keys = tomlkit.table()
    normal = tomlkit.table()
    space = tomlkit.table()
    space["x"] = legacy
    normal["space"] = space
    keys["normal"] = normal
    document["keys"] = keys
    Path(current.helix_config).write_text(tomlkit.dumps(document))
    record_install(InstallationState(current=current), state_root=root)
    runner = CandidateRunner()

    with pytest.raises(EnhanceError, match="setup --helix-shell-bridge"):
        preview_update(
            "git+https://example.com/dbt-enhance.git@v0.2.0",
            settings_config=tmp_path / "settings.toml",
            runner=runner,
            state_root=root,
        )
    assert runner.commands == []


def test_update_source_rejects_embedded_url_credentials(tmp_path):
    root = tmp_path / "state"
    record_install(InstallationState(current=provenance(tmp_path)), state_root=root)
    runner = CandidateRunner()
    with pytest.raises(EnhanceError, match="embedded credentials"):
        preview_update(
            "git+https://secret@example.com/dbt-enhance.git@v0.2.0",
            settings_config=tmp_path / "settings.toml",
            runner=runner,
            state_root=root,
        )
    assert runner.commands == []


def test_local_artifact_is_digest_checked_and_privately_staged_for_apply(tmp_path):
    root = tmp_path / "state"
    record_install(InstallationState(current=provenance(tmp_path)), state_root=root)
    artifact = tmp_path / "dbt_enhance-0.2.0-py3-none-any.whl"
    artifact.write_bytes(b"synthetic wheel bytes")
    settings = tmp_path / "settings.toml"
    settings.write_text("schema = 1\n")
    runner = CandidateRunner()

    plan = preview_update(str(artifact), settings_config=settings, runner=runner, state_root=root)
    assert plan.candidate.kind == "artifact"
    assert plan.candidate.identity.startswith("sha256:")
    result = apply_update(plan, runner=runner)
    assert result.ok
    installs = [command for command in runner.commands if command[:3] == ["uv", "tool", "install"]]
    assert len(installs) == 1
    assert installs[0][-1].startswith("file://")
    assert "candidate-" in installs[0][-1]
    assert list(root.glob("candidate-*")) == []


def test_incompatible_update_never_replaces_tool(tmp_path):
    root = tmp_path / "state"
    record_install(InstallationState(current=provenance(tmp_path)), state_root=root)
    runner = CandidateRunner(valid=False)
    with pytest.raises(EnhanceError, match="no replacement"):
        preview_update(
            "git+https://example.com/dbt-enhance.git@v0.2.0",
            settings_config=tmp_path / "settings.toml",
            runner=runner,
            state_root=root,
        )
    assert all(command[:3] != ["uv", "tool", "install"] for command in runner.commands)


def test_retained_bridge_requires_compatible_candidate_before_install(tmp_path):
    root = tmp_path / "state"
    record_install(InstallationState(current=provenance(tmp_path)), state_root=root)
    runner = CandidateRunner(bridge_format=3)

    with pytest.raises(EnhanceError, match="bridge format"):
        preview_update(
            "git+https://example.com/dbt-enhance.git@v0.2.0",
            settings_config=tmp_path / "settings.toml",
            runner=runner,
            state_root=root,
        )
    assert all(command[:3] != ["uv", "tool", "install"] for command in runner.commands)


def test_candidate_without_bridge_capability_is_allowed_when_none_is_retained(tmp_path):
    root = tmp_path / "state"
    current = replace(provenance(tmp_path), helix_config=None, helix_launcher=())
    record_install(InstallationState(current=current), state_root=root)
    runner = CandidateRunner(bridge_format=None)

    plan = preview_update(
        "git+https://example.com/dbt-enhance.git@v0.2.0",
        settings_config=tmp_path / "settings.toml",
        runner=runner,
        state_root=root,
    )
    assert plan.candidate is not None


@pytest.mark.parametrize("schema", [True, 1.0])
def test_candidate_schema_requires_an_integer_not_boolean_or_float(tmp_path, schema):
    root = tmp_path / "state"
    record_install(InstallationState(current=provenance(tmp_path)), state_root=root)
    runner = CandidateRunner()
    runner.schema = schema
    with pytest.raises(EnhanceError, match="incompatible capability"):
        preview_update(
            "git+https://example.com/dbt-enhance.git@v0.2.0",
            settings_config=tmp_path / "settings.toml",
            runner=runner,
            state_root=root,
        )


def test_candidate_must_affirm_compatibility(tmp_path):
    root = tmp_path / "state"
    record_install(InstallationState(current=provenance(tmp_path)), state_root=root)
    runner = CandidateRunner(valid=None)
    with pytest.raises(EnhanceError, match="incompatible capability"):
        preview_update(
            "git+https://example.com/dbt-enhance.git@v0.2.0",
            settings_config=tmp_path / "settings.toml",
            runner=runner,
            state_root=root,
        )
    assert all(command[:3] != ["uv", "tool", "install"] for command in runner.commands)


def test_rollback_refuses_unverifiable_legacy_and_preserves_editor_config(tmp_path):
    root = tmp_path / "state"
    current = provenance(tmp_path)
    legacy = provenance(tmp_path, kind="legacy")
    record_install(InstallationState(current=current, prior=legacy), state_root=root)
    runner = CandidateRunner()
    with pytest.raises(EnhanceError, match="safe rollback is blocked"):
        preview_rollback(settings_config=tmp_path / "settings.toml", runner=runner, state_root=root)
    assert runner.commands == []


def test_rollback_candidate_must_support_retained_bridge_format(tmp_path):
    root = tmp_path / "state"
    current = provenance(tmp_path)
    prior = replace(
        current,
        source=f"git+https://example.com/dbt-enhance.git@{NEW_COMMIT}",
        identity=NEW_COMMIT,
        version="0.2.0",
    )
    record_install(InstallationState(current=current, prior=prior), state_root=root)
    runner = CandidateRunner(version="0.2.0", bridge_format=None)

    with pytest.raises(EnhanceError, match="bridge format"):
        preview_rollback(settings_config=tmp_path / "settings.toml", runner=runner, state_root=root)
    assert all(command[:3] != ["uv", "tool", "install"] for command in runner.commands)


def test_rollback_revalidates_and_only_swaps_version_provenance(tmp_path):
    root = tmp_path / "state"
    current = provenance(tmp_path)
    prior = InstallProvenance(
        **{
            **current.__dict__,
            "source": f"git+https://example.com/dbt-enhance.git@{NEW_COMMIT}",
            "identity": NEW_COMMIT,
            "version": "0.2.0",
        }
    )
    record_install(InstallationState(current=current, prior=prior), state_root=root)
    helix = Path(current.helix_config)
    helix.write_text("# user editor config\n")
    settings = tmp_path / "settings.toml"
    settings.write_text("schema = 1\n")
    runner = CandidateRunner(version="0.2.0")

    plan = preview_rollback(settings_config=settings, runner=runner, state_root=root)
    result = apply_rollback(plan, runner=runner)
    assert result.ok
    assert helix.read_text() == "# user editor config\n"
    assert settings.read_text() == "schema = 1\n"
    rolled_back = load_state(state_root=root)
    assert rolled_back.current == prior
    assert rolled_back.prior == current


def test_removal_rechecks_active_workers_and_preserves_later_binding_edits(tmp_path):
    root = tmp_path / "state"
    current = provenance(tmp_path)
    helix = Path(current.helix_config)
    install_config(
        helix,
        list(current.helix_launcher),
        state_root=root,
        helix_shell_bridge=True,
    )
    document = tomlkit.parse(helix.read_text())
    document["keys"]["normal"]["space"]["x"] = "later_user_edit"
    helix.write_text(tomlkit.dumps(document))
    record_install(InstallationState(current=current), state_root=root)
    project = tmp_path / "project"

    def worker_snapshot():
        return ({"project": str(project), "active": True},)

    runner = CandidateRunner()
    reset_calls = []

    plan = preview_remove(inventory=worker_snapshot, state_root=root)
    with pytest.raises(EnhanceError, match="Active workers block"):
        apply_remove(plan, runner=runner, inventory=worker_snapshot)
    assert runner.commands == []

    result = apply_remove(
        plan,
        allow_active=True,
        runner=runner,
        inventory=worker_snapshot,
        reset=lambda path, *, allow_active=False: reset_calls.append((path, allow_active)) or True,
    )
    assert result.ok
    assert reset_calls == [(project, True)]
    updated = tomlkit.parse(helix.read_text())
    assert updated["keys"]["normal"]["space"]["x"] == "later_user_edit"
    assert "x" not in updated["keys"]["select"]["space"]
    assert "shell" not in updated["editor"]
    assert not (root / "helix-shell-bridge-v2").exists()
    assert load_state(state_root=root) is None
    assert ["uv", "tool", "uninstall", "dbt-enhance"] in runner.commands


def test_missing_bridge_cleanup_preserves_install_and_provenance(tmp_path):
    root = tmp_path / "state"
    current = provenance(tmp_path)
    helix = Path(current.helix_config)
    install_config(
        helix,
        list(current.helix_launcher),
        state_root=root,
        helix_shell_bridge=True,
    )
    document = tomlkit.parse(helix.read_text())
    document["editor"]["shell"] = ["/bin/sh", "-c"]
    helix.write_text(tomlkit.dumps(document))
    (root / "helix-shell-bridge-v2").unlink()
    state = InstallationState(current=current)
    record_install(state, state_root=root)
    runner = CandidateRunner()
    plan = preview_remove(inventory=lambda: (), state_root=root)

    result = apply_remove(plan, runner=runner, inventory=lambda: ())
    assert not result.ok
    assert "recognizable" in result.errors[0]
    assert runner.commands == []
    assert load_state(state_root=root) == state
    assert helix.exists()


def test_tampered_bridge_cleanup_preserves_install_and_provenance(tmp_path):
    root = tmp_path / "state"
    current = provenance(tmp_path)
    install_config(
        Path(current.helix_config),
        list(current.helix_launcher),
        state_root=root,
        helix_shell_bridge=True,
    )
    bridge = root / "helix-shell-bridge-v2"
    bridge.write_text(bridge.read_text() + "# user edit\n")
    state = InstallationState(current=current)
    record_install(state, state_root=root)
    runner = CandidateRunner()
    plan = preview_remove(inventory=lambda: (), state_root=root)
    assert "restore editor.shell only if it still selects the exact owned bridge" in plan.effects
    assert "reload or restart open Helix instances after removal" in plan.effects

    result = apply_remove(plan, runner=runner, inventory=lambda: ())
    assert not result.ok
    assert runner.commands == []
    assert load_state(state_root=root) == state
    assert bridge.exists()


def test_binding_cleanup_failure_preserves_installation_and_retry_provenance(tmp_path, monkeypatch):
    import dbt_enhance.lifecycle as lifecycle
    from dbt_enhance import worker

    root = tmp_path / "state"
    current = provenance(tmp_path)
    state = InstallationState(current=current)
    record_install(state, state_root=root)
    plan = preview_remove(inventory=lambda: (), state_root=root)
    runner = CandidateRunner()

    def cannot_edit(*_args, **_kwargs):
        raise PermissionError("temporary editor configuration permission failure")

    monkeypatch.setattr(lifecycle, "remove_owned_binding", cannot_edit)
    epoch = worker._admission_snapshot(worker._Deadline(2))
    result = apply_remove(plan, runner=runner, inventory=lambda: ())
    assert not result.ok
    assert worker._admission_snapshot(worker._Deadline(2)) == epoch
    assert runner.commands == [], "must not uninstall before integration cleanup succeeds"
    assert load_state(state_root=root) == state
    assert "Helix mapping removal failed" in result.errors[0]


def test_remove_purge_is_limited_to_known_tool_files(tmp_path):
    root = tmp_path / "state"
    record_install(InstallationState(current=provenance(tmp_path)), state_root=root)
    preferences = tmp_path / "preferences.toml"
    preferences.write_text("schema = 1\n")
    diagnostics = root / "diagnostics"
    diagnostics.mkdir(mode=0o700)
    (diagnostics / "event.json").write_text("{}")
    unrelated = root / "keep.txt"
    unrelated.write_text("keep")
    runner = CandidateRunner()

    plan = preview_remove(
        purge=True,
        inventory=lambda: (),
        state_root=root,
        preferences=preferences,
    )
    result = apply_remove(plan, runner=runner, inventory=lambda: ())
    assert result.ok
    assert not preferences.exists()
    assert not diagnostics.exists()
    assert unrelated.read_text() == "keep"
