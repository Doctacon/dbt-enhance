import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import tomlkit

from dbt_enhance.helix import (
    binding,
    bridge_path,
    install_config,
    parse_transport_command,
    prepare_config,
    remove_owned_binding,
)
from dbt_enhance.models import EnhanceError


def install(tmp_path: Path, config: Path, launcher: list[str], **kwargs):
    return install_config(
        config,
        launcher,
        state_root=tmp_path / "state",
        helix_shell_bridge=True,
        **kwargs,
    )


def test_install_preserves_comments_bindings_symlink_and_private_bridge(tmp_path):
    actual = tmp_path / "dotfiles" / "config.toml"
    actual.parent.mkdir()
    original = '# My theme\ntheme = "night"\n[keys.normal]\nspace.d = "delete_selection"\n'
    actual.write_text(original)
    link = tmp_path / "config.toml"
    link.symlink_to(actual)

    backup = install(tmp_path, link, ["/some path/dbt-enhance"])

    assert link.is_symlink()
    assert backup.read_text() == original
    parsed = tomlkit.parse(actual.read_text())
    assert parsed["keys"]["normal"]["space"]["d"] == "delete_selection"
    assert parsed["keys"]["normal"]["space"]["x"] == parsed["keys"]["select"]["space"]["x"]
    bridge = bridge_path(tmp_path / "state")
    assert parsed["editor"]["shell"] == [str(bridge)]
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700
    assert stat.S_IMODE(bridge.stat().st_mode) == 0o700
    assert install_config(link, ["/some path/dbt-enhance"], state_root=tmp_path / "state") is None


def test_no_explicit_bridge_consent_fails_without_changes(tmp_path):
    config = tmp_path / "config.toml"
    original = 'theme = "base16"\n'
    config.write_text(original)
    with pytest.raises(EnhanceError, match="--helix-shell-bridge"):
        prepare_config(config, ["/bin/dbt-enhance"], state_root=tmp_path / "state")
    assert config.read_text() == original
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("mode", ["normal", "select"])
def test_conflict_is_all_or_nothing(tmp_path, mode):
    config = tmp_path / "config.toml"
    original = f'[keys.{mode}.space]\nx = "quit"\n'
    config.write_text(original)
    with pytest.raises(EnhanceError, match="already has a binding"):
        install(tmp_path, config, ["/bin/dbt-enhance"])
    assert config.read_text() == original
    assert not (tmp_path / "state").exists()
    backup = install(tmp_path, config, ["/bin/dbt-enhance"], force=True)
    assert backup.read_text() == original


def test_custom_posix_shell_is_preserved_and_restored(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[editor]\nshell = ["/bin/bash", "--noprofile", "-c"]\n')
    install(tmp_path, config, ["/bin/dbt-enhance"])
    bridge = bridge_path(tmp_path / "state")
    assert tomlkit.parse(config.read_text())["editor"]["shell"] == [str(bridge)]

    remove_owned_binding(
        config,
        ["/bin/dbt-enhance"],
        state_root=tmp_path / "state",
    )
    assert tomlkit.parse(config.read_text())["editor"]["shell"] == [
        "/bin/bash",
        "--noprofile",
        "-c",
    ]
    assert not bridge.exists()


@pytest.mark.parametrize(
    "shell",
    [
        ["/bin/bash", "--norc", "-c"],
        ["/bin/bash", "--posix", "-c"],
        ["/bin/bash", "--noprofile", "--norc", "-c"],
        ["/bin/zsh", "-f", "-c"],
    ],
)
def test_only_probed_optional_shell_flags_are_accepted(tmp_path, shell):
    if not Path(shell[0]).is_file():
        pytest.skip(f"requires {shell[0]}")
    config = tmp_path / "config.toml"
    config.write_text(tomlkit.dumps({"editor": {"shell": shell}}))

    install(tmp_path, config, ["/bin/dbt-enhance"])
    remove_owned_binding(config, ["/bin/dbt-enhance"], state_root=tmp_path / "state")

    assert tomlkit.parse(config.read_text())["editor"]["shell"] == shell


@pytest.mark.parametrize(
    "shell",
    [
        ["env", "sh", "-c"],
        ["/bin/sh", "-i", "-c"],
        ["/bin/sh", "-ic", "-c"],
        ["/bin/sh", "--", "-c"],
        ["/bin/sh", "script.sh", "-c"],
        ["/bin/sh", "-s", "-c"],
        ["/bin/sh", "-c", "-c"],
        ["/bin/sh", "-c", "command"],
        ["/bin/bash", "-O", "extglob", "-c"],
        ["/bin/bash", "--rcfile", "startup", "-c"],
        ["/bin/bash", "--login", "-c"],
        ["/bin/bash", "--noprofile", "--posix", "-c"],
        ["/bin/bash", "--norc", "--noprofile", "-c"],
        ["/bin/zsh", "-df", "-c"],
    ],
)
def test_unsupported_shell_modes_are_rejected_before_state_change(tmp_path, shell):
    config = tmp_path / "config.toml"
    original = tomlkit.dumps({"editor": {"shell": shell}})
    config.write_text(original)
    with pytest.raises(EnhanceError, match="supported noninteractive"):
        install(tmp_path, config, ["/bin/dbt-enhance"])
    assert config.read_text() == original
    assert not (tmp_path / "state").exists()


def test_bridge_subprocess_roundtrips_metacharacters_and_delegates(tmp_path):
    receiver = tmp_path / "receiver.py"
    receiver.write_text(
        "import json,sys; print(json.dumps([sys.argv[1:], sys.stdin.read()], ensure_ascii=False))"
    )
    config = tmp_path / "config.toml"
    install(tmp_path, config, [sys.executable, str(receiver)])
    parsed = tomlkit.parse(config.read_text())
    command = parsed["keys"]["normal"]["space"]["x"].removeprefix(":pipe-to ")
    filename = "models/a 'quote' $cash `tick` %{buffer_name} 百分比.sql"
    raw = command.replace("%{buffer_name}", filename)
    sql = "select '{{ ref(\"orders\") }}', '$HOME', '`id`'\n"

    result = subprocess.run(
        [str(bridge_path(tmp_path / "state")), raw],
        input=sql,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == [["_helix", raw], sql]

    delegated = subprocess.run(
        [str(bridge_path(tmp_path / "state")), "cat"],
        input="stdin-data",
        capture_output=True,
        text=True,
        check=True,
    )
    assert delegated.stdout == "stdin-data"


def test_helper_parser_preserves_percent_names_and_rejects_unknown_or_controls():
    command = binding("a" * 64).removeprefix(":pipe-to ")
    filename = "models/%{buffer_name} $x `y` ü.sql"
    assert parse_transport_command(command.replace("%{buffer_name}", filename)) == filename
    invalid = ["unknown", command.replace("%{buffer_name}", "")]
    invalid.extend(command.replace("%{buffer_name}", f"bad{control}name") for control in "\r\n\0")
    for raw in invalid:
        with pytest.raises(EnhanceError):
            parse_transport_command(raw)


def test_parse_guard_fallback_blocks_newline_injection_with_exec_and_exit_overrides(tmp_path):
    marker = tmp_path / "OWNED"
    shell_command = (
        binding("b" * 64)
        .removeprefix(":pipe-to ")
        .replace("%{buffer_name}", f"old.sql\ntouch {shlex.quote(str(marker))}")
    )
    result = subprocess.run(
        ["/bin/bash", "-c", f"exec() {{ return 0; }}; exit() {{ return 0; }}; {shell_command}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not marker.exists()


def test_bridge_rejects_reserved_stale_malformed_and_legacy_transport(tmp_path):
    config = tmp_path / "config.toml"
    install(tmp_path, config, ["/bin/dbt-enhance"])
    installed = tomlkit.parse(config.read_text())["keys"]["normal"]["space"]["x"]
    command = installed.removeprefix(":pipe-to ")
    bridge = bridge_path(tmp_path / "state")
    reserved = (
        command.replace(command.split("DBT_ENHANCE_BUFFER_V3:", 1)[1][:64], "f" * 64),
        "exec /usr/bin/false # DBT_ENHANCE_BUFFER_V2:" + "a" * 64 + " old.sql",
        "printf ignored # DBT_ENHANCE_BUFFER_UNKNOWN:not-a-fingerprint",
        "IFS= read -r dbt_enhance_file <<'DBT_ENHANCE_FILE_" + "1" * 32 + "'",
    )

    for raw in reserved:
        result = subprocess.run([str(bridge), raw], capture_output=True, text=True, check=False)
        assert result.returncode == 64
        assert "reserved Helix transport" in result.stderr


def test_stale_bridge_command_never_reaches_bash_env_exec_override(tmp_path):
    bash = Path("/bin/bash")
    if not bash.is_file():
        pytest.skip("requires /bin/bash")
    config = tmp_path / "config.toml"
    config.write_text(f'[editor]\nshell = ["{bash}", "-c"]\n')
    install(tmp_path, config, ["/bin/dbt-enhance"])
    command = tomlkit.parse(config.read_text())["keys"]["normal"]["space"]["x"].removeprefix(
        ":pipe-to "
    )
    fingerprint = command.split("DBT_ENHANCE_BUFFER_V3:", 1)[1][:64]
    marker = tmp_path / "OWNED"
    stale = command.replace(fingerprint, "e" * 64).replace(
        "%{buffer_name}", f"old.sql\ntouch {shlex.quote(str(marker))}"
    )
    bash_env = tmp_path / "bash-env"
    bash_env.write_text("exec() { :; }\nexit() { :; }\n")

    result = subprocess.run(
        [str(bridge_path(tmp_path / "state")), stale],
        env={**os.environ, "BASH_ENV": str(bash_env)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 64
    assert not marker.exists()


def test_legacy_exact_binding_migrates_only_with_consent_and_backup(tmp_path):
    config = tmp_path / "config.toml"
    launcher = ["/bin/dbt-enhance"]
    delimiter = "DBT_ENHANCE_FILE_" + "1" * 32
    legacy = (
        f":pipe-to IFS= read -r dbt_enhance_file <<'{delimiter}'\n"
        f"%{{buffer_name}}\n{delimiter}\n"
        f'{shlex.join(launcher)} popup --file "$dbt_enhance_file"'
    )
    original = tomlkit.dumps(
        tomlkit.parse(
            '[keys.normal.space]\nx = "placeholder"\n[keys.select.space]\nx = "placeholder"\n'
        )
    ).replace("placeholder", legacy.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n"))
    config.write_text(original)

    with pytest.raises(EnhanceError, match="--helix-shell-bridge"):
        install_config(config, launcher, state_root=tmp_path / "state")
    backup = install(tmp_path, config, launcher)
    assert backup is not None and backup.read_text() == original
    migrated = tomlkit.parse(config.read_text())["keys"]["normal"]["space"]["x"]
    assert ") # DBT_ENHANCE_BUFFER_V3:" in migrated


def test_missing_bridge_with_recognizable_binding_blocks_cleanup_and_preserves_config(tmp_path):
    config = tmp_path / "config.toml"
    launcher = ["/bin/dbt-enhance"]
    install(tmp_path, config, launcher)
    document = tomlkit.parse(config.read_text())
    document["editor"]["shell"] = ["/bin/sh", "-c"]
    config.write_text(tomlkit.dumps(document))
    original = config.read_bytes()
    bridge_path(tmp_path / "state").unlink()

    with pytest.raises(EnhanceError, match="recognizable.*bridge mapping"):
        remove_owned_binding(config, launcher, state_root=tmp_path / "state")
    assert config.read_bytes() == original


def test_missing_config_does_not_orphan_an_exact_owned_bridge(tmp_path):
    config = tmp_path / "config.toml"
    launcher = ["/bin/dbt-enhance"]
    install(tmp_path, config, launcher)
    bridge = bridge_path(tmp_path / "state")
    config.unlink()
    remove_owned_binding(config, launcher, state_root=tmp_path / "state")
    assert not bridge.exists()
    assert not config.exists()


def test_tampered_bridge_and_later_user_edits_are_preserved(tmp_path):
    config = tmp_path / "config.toml"
    launcher = ["/bin/dbt-enhance"]
    install(tmp_path, config, launcher, shortcut="space.e")
    document = tomlkit.parse(config.read_text())
    document["keys"]["normal"]["space"]["e"] = "later_mapping"
    document["editor"]["shell"] = ["/bin/bash", "-c"]
    config.write_text(tomlkit.dumps(document))

    remove_owned_binding(config, launcher, shortcut="space.e", state_root=tmp_path / "state")
    updated = tomlkit.parse(config.read_text())
    assert updated["keys"]["normal"]["space"]["e"] == "later_mapping"
    assert updated["editor"]["shell"] == ["/bin/bash", "-c"]

    # A modified owned-path file is never overwritten or deleted.
    install(tmp_path, config, launcher, shortcut="space.e", force=True)
    bridge = bridge_path(tmp_path / "state")
    bridge.write_text(bridge.read_text() + "# tampered\n")
    before = bridge.read_text()
    with pytest.raises(EnhanceError, match="modified or not owned"):
        remove_owned_binding(config, launcher, shortcut="space.e", state_root=tmp_path / "state")
    assert bridge.read_text() == before
