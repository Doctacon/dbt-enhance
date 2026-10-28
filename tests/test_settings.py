import os
import threading
from pathlib import Path

import pytest

from dbt_enhance.models import EnhanceError
from dbt_enhance.settings import (
    UserSettings,
    check_target_allowed,
    load_user_settings,
    resolve_settings,
    settings_path,
)


def test_configuration_fifo_is_rejected_without_waiting_for_a_writer(tmp_path):
    config = tmp_path / "settings.toml"
    os.mkfifo(config)
    errors = []

    def load():
        try:
            load_user_settings(config)
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=load, daemon=True)
    thread.start()
    thread.join(timeout=0.2)
    try:
        assert not thread.is_alive(), "settings loading blocked on a FIFO"
        assert isinstance(errors[0], EnhanceError)
        assert errors[0].code == "configuration_invalid"
    finally:
        if thread.is_alive():
            fd = os.open(config, os.O_WRONLY | os.O_NONBLOCK)
            os.write(fd, b"schema = 1\n")
            os.close(fd)
            thread.join(timeout=1)


def test_resolve_settings_uses_documented_builtins(tmp_path: Path):
    resolved = resolve_settings(tmp_path, environment={})

    assert resolved.values == {
        "limit": 500,
        "timeout": 120.0,
        "target": None,
        "profiles_dir": None,
        "dbt": None,
        "backend": "worker",
        "worker_idle_seconds": 600.0,
        "diagnostics": True,
        "theme": "auto",
    }
    assert set(resolved.sources.values()) == {"builtin"}
    assert resolved.allowed_targets is None


def test_load_and_resolve_precedence_and_canonical_project(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "dbt_project.yml").write_text("name: test\n")
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    profiles = tmp_path / "profiles"
    dbt = tmp_path / "dbt"
    config = tmp_path / "config.toml"
    config.write_text(
        f'''schema = 1
[global]
limit = 100
timeout = 30
backend = "process"
diagnostics = false

[[project]]
path = "{alias}"
limit = 200
target = "dev"
profiles_dir = "{profiles}"
dbt = "{dbt}"
worker_idle_seconds = 45
theme = "dark"

[[safety.project_targets]]
project = "{project}"
targets = ["dev", "ci"]
'''
    )

    user = load_user_settings(config)
    resolved = resolve_settings(
        project,
        cli={"limit": 400, "target": None},
        environment={"DBT_ENHANCE_LIMIT": "300", "DBT_ENHANCE_TIMEOUT": "60"},
        user=user,
    )

    assert user.schema == 1
    assert resolved.values == {
        "limit": 400,
        "timeout": 60.0,
        "target": "dev",
        "profiles_dir": profiles,
        "dbt": dbt,
        "backend": "process",
        "worker_idle_seconds": 45.0,
        "diagnostics": False,
        "theme": "dark",
    }
    assert resolved.sources == {
        "limit": "cli",
        "timeout": "environment",
        "target": "project",
        "profiles_dir": "project",
        "dbt": "project",
        "backend": "global",
        "worker_idle_seconds": "project",
        "diagnostics": "global",
        "theme": "project",
    }
    assert resolved.allowed_targets == ("dev", "ci")


@pytest.mark.parametrize("mode", [0o664, 0o666])
def test_policy_file_cannot_be_writable_by_other_users(tmp_path, mode):
    config = tmp_path / "settings.toml"
    config.write_text("schema = 1\n")
    config.chmod(mode)
    with pytest.raises(EnhanceError, match="writable") as error:
        load_user_settings(config)
    assert error.value.code == "configuration_invalid"


def test_unmounted_project_settings_do_not_break_other_projects(tmp_path):
    absent = tmp_path / "unmounted-project"
    present = tmp_path / "live-project"
    present.mkdir()
    (present / "dbt_project.yml").touch()
    config = tmp_path / "settings.toml"
    config.write_text(
        "[global]\nlimit = 42\n"
        f'[[project]]\npath = "{absent}"\nlimit = 10\n'
        f'[[safety.project_targets]]\nproject = "{absent}"\ntargets = ["dev"]\n'
    )
    user = load_user_settings(config)
    effective = resolve_settings(present, environment={}, user=user)
    assert effective.values["limit"] == 42
    assert user.safety[absent] == ("dev",)
    assert not absent.exists()


def test_settings_path_uses_only_xdg_or_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert settings_path({}) == tmp_path / "home/.config/dbt-enhance/config.toml"
    assert settings_path({"XDG_CONFIG_HOME": str(tmp_path / "xdg")}) == (
        tmp_path / "xdg/dbt-enhance/config.toml"
    )


@pytest.mark.parametrize(
    "body",
    [
        "schema = 2\n",
        "[global]\nlimti = 10\n",
        "[global]\npassword = 'secret'\n",
        "[global]\ntimeout = inf\n",
        pytest.param("[global]\ntimeout = " + str(10**1000) + "\n", id="overflowing-number"),
        "[global]\nprofiles_dir = 'relative'\n",
        "[global]\nworker_idle_seconds = 999999\n",
        "[[project]]\npath = []\n",
        "[safety]\nproject_targets = {}\n",
    ],
)
def test_invalid_configuration_has_fixed_preflight_error(tmp_path: Path, body: str):
    config = tmp_path / "config.toml"
    config.write_text(body)

    with pytest.raises(EnhanceError) as caught:
        load_user_settings(config)

    assert caught.value.code == "configuration_invalid"
    assert caught.value.stage == "preflight"


def test_duplicate_canonical_projects_and_safety_entries_are_rejected(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "dbt_project.yml").write_text("name: test\n")
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)

    for entries in (
        f"[[project]]\npath='{project}'\n[[project]]\npath='{alias}'\n",
        f"[[safety.project_targets]]\nproject='{project}'\ntargets=[]\n"
        f"[[safety.project_targets]]\nproject='{alias}'\ntargets=['dev']\n",
    ):
        config = tmp_path / "config.toml"
        config.write_text(entries)
        with pytest.raises(EnhanceError) as caught:
            load_user_settings(config)
        assert caught.value.code == "configuration_invalid"


def test_safety_policy_cannot_be_overridden_and_empty_policy_denies_all(tmp_path: Path):
    user = UserSettings(safety={tmp_path.resolve(): ()})
    resolved = resolve_settings(
        tmp_path,
        cli={"target": "prod", "diagnostics": False},
        environment={"DBT_ENHANCE_TARGET": "dev"},
        user=user,
    )

    assert resolved.values["target"] == "prod"
    assert resolved.values["diagnostics"] is False
    assert resolved.allowed_targets == ()
    with pytest.raises(EnhanceError) as caught:
        check_target_allowed(resolved.allowed_targets, tmp_path, "prod")
    assert caught.value.code == "target_not_allowed"
    assert caught.value.stage == "preflight"


def test_editor_smoke_switch_is_not_treated_as_an_invalid_preference(tmp_path):
    environment = {"DBT_ENHANCE_EDITOR_SMOKE": "1", "DBT_PROFILE_INPUT": "unchanged"}
    resolved = resolve_settings(tmp_path, environment=environment, user=UserSettings())
    assert resolved.values["backend"] == "worker"
    assert environment["DBT_ENHANCE_EDITOR_SMOKE"] == "1"


def test_unknown_cli_settings_are_rejected(tmp_path: Path):
    with pytest.raises(EnhanceError, match="Unknown key"):
        resolve_settings(tmp_path, cli={"password": "secret"}, environment={}, user=UserSettings())


def test_unrecognized_environment_names_remain_available_to_native_profiles(tmp_path):
    environment = {"DBT_ENHANCE_PROFILE_INPUT": "native-value", "DBT_ENHANCE_LIMIT": "17"}
    resolved = resolve_settings(tmp_path, environment=environment, user=UserSettings())
    assert resolved.values["limit"] == 17
    assert environment["DBT_ENHANCE_PROFILE_INPUT"] == "native-value"
