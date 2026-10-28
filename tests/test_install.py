import json
import subprocess

import pytest
import tomlkit

from dbt_enhance import install
from dbt_enhance.helix import HELIX_BRIDGE_FORMAT
from dbt_enhance.lifecycle import load_state
from dbt_enhance.models import EnhanceError


class SetupRunner:
    def __init__(self, binary):
        self.binary = binary
        self.commands = []
        self.bridge_format = HELIX_BRIDGE_FORMAT

    def __call__(self, command, **_kwargs):
        self.commands.append(command)
        if command[1:4] == ["tool", "dir", "--bin"]:
            return subprocess.CompletedProcess(command, 0, str(self.binary.parent), "")
        if command[1:4] == ["tool", "install", "--force"]:
            self.binary.touch()
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[0] == "uvx" and "validate" in command:
            report = {
                "valid": True,
                "schema": 1,
                "version": "0.1.2",
                "helix_bridge_format": self.bridge_format,
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(report), "")
        if command == [str(self.binary), "--version"] or (
            command[0] == "uvx" and command[-1] == "--version"
        ):
            return subprocess.CompletedProcess(command, 0, "0.1.2\n", "")
        raise AssertionError(command)


def test_setup_injected_runner_records_private_state_and_alternative_shortcut(
    tmp_path, monkeypatch
):
    binary = tmp_path / "bin" / "dbt-enhance"
    binary.parent.mkdir()
    monkeypatch.setattr(install.shutil, "which", lambda _: "uv")
    runner = SetupRunner(binary)
    config = tmp_path / "helix.toml"
    state_root = tmp_path / "state"

    launcher, backup = install.setup(
        config,
        source="dbt-enhance==0.1.2",
        shortcut="space.e",
        helix_shell_bridge=True,
        runner=runner,
        state_root=state_root,
    )
    assert launcher == binary
    assert backup is None
    parsed = tomlkit.parse(config.read_text())
    assert parsed["keys"]["normal"]["space"]["e"] == parsed["keys"]["select"]["space"]["e"]
    assert parsed["editor"]["shell"] == [str(state_root / "helix-shell-bridge-v2")]
    state = load_state(state_root=state_root)
    assert state.current.version == "0.1.2"
    assert state.current.shortcut == "space.e"
    assert state.current.helix_launcher == (str(binary),)


def test_setup_alternative_conflict_precedes_install(tmp_path, monkeypatch):
    binary = tmp_path / "bin" / "dbt-enhance"
    binary.parent.mkdir()
    monkeypatch.setattr(install.shutil, "which", lambda _: "uv")
    runner = SetupRunner(binary)
    config = tmp_path / "helix.toml"
    original = '[keys.normal.space]\ne = "keep"\n'
    config.write_text(original)

    with pytest.raises(EnhanceError, match="already has a binding"):
        install.setup(
            config,
            source=".",
            shortcut="space.e",
            helix_shell_bridge=True,
            runner=runner,
            state_root=tmp_path / "state",
        )
    assert runner.commands == [["uv", "tool", "dir", "--bin"]]
    assert config.read_text() == original


def test_setup_without_bridge_consent_fails_before_install_or_config(tmp_path, monkeypatch):
    binary = tmp_path / "bin" / "dbt-enhance"
    binary.parent.mkdir()
    monkeypatch.setattr(install.shutil, "which", lambda _: "uv")
    runner = SetupRunner(binary)
    config = tmp_path / "helix.toml"
    config.write_text('theme = "base16"\n')

    with pytest.raises(EnhanceError, match="--helix-shell-bridge"):
        install.setup(
            config,
            source=".",
            runner=runner,
            state_root=tmp_path / "state",
        )
    assert runner.commands == [["uv", "tool", "dir", "--bin"]]
    assert config.read_text() == 'theme = "base16"\n'
    assert not (tmp_path / "state").exists()


def test_setup_checks_installed_version_before_publishing_integration(tmp_path, monkeypatch):
    binary = tmp_path / "bin" / "dbt-enhance"
    binary.parent.mkdir()
    monkeypatch.setattr(install.shutil, "which", lambda _: "uv")
    candidate = SetupRunner(binary)

    def run(command, **kwargs):
        result = candidate(command, **kwargs)
        if command == [str(binary), "--version"]:
            return subprocess.CompletedProcess(command, 0, "0.0.0\n", "")
        return result

    config = tmp_path / "helix.toml"
    original = 'theme = "base16"\n'
    config.write_text(original)
    with pytest.raises(EnhanceError, match="partial installation"):
        install.setup(
            config, source=".", helix_shell_bridge=True, runner=run, state_root=tmp_path / "state"
        )
    assert binary.exists()  # Report the partial installation honestly; do not claim rollback.
    assert config.read_text() == original
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("capability", [None, True, 1])
def test_setup_rejects_incompatible_candidate_before_any_install_or_bridge_effect(
    tmp_path, monkeypatch, capability
):
    binary = tmp_path / "bin" / "dbt-enhance"
    binary.parent.mkdir()
    monkeypatch.setattr(install.shutil, "which", lambda _: "uv")
    runner = SetupRunner(binary)
    runner.bridge_format = capability
    config = tmp_path / "helix.toml"
    original = 'theme = "base16"\n'
    config.write_text(original)
    with pytest.raises(EnhanceError, match="Helix bridge format"):
        install.setup(
            config,
            source=".",
            helix_shell_bridge=True,
            runner=runner,
            state_root=tmp_path / "state",
        )
    assert not any("install" in command for command in runner.commands)
    assert not binary.exists()
    assert config.read_text() == original
    assert not (tmp_path / "state").exists()
