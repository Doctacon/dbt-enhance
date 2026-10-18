import json
import shlex
import subprocess
import sys

import pytest
import tomlkit

from dbt_enhance.helix import binding, install_config, prepare_config
from dbt_enhance.models import EnhanceError


def test_install_preserves_comments_existing_bindings_and_symlink(tmp_path):
    actual = tmp_path / "dotfiles" / "config.toml"
    actual.parent.mkdir()
    original = '# My theme\ntheme = "night"\n[keys.normal]\nspace.d = "delete_selection"\n'
    actual.write_text(original)
    link = tmp_path / "config.toml"
    link.symlink_to(actual)
    backup = install_config(link, ["/some path/dbt-enhance"])
    assert link.is_symlink()
    assert backup.read_text() == original
    assert "# My theme" in actual.read_text()
    config = tomlkit.parse(actual.read_text())
    assert config["keys"]["normal"]["space"]["d"] == "delete_selection"
    assert config["keys"]["normal"]["space"]["x"] == config["keys"]["select"]["space"]["x"]
    before = actual.read_bytes()
    assert install_config(link, ["/some path/dbt-enhance"]) is None
    assert actual.read_bytes() == before


@pytest.mark.parametrize("mode", ["normal", "select"])
def test_conflict_is_all_or_nothing(tmp_path, mode):
    config = tmp_path / "config.toml"
    original = f'[keys.{mode}.space]\nx = "quit"\n'
    config.write_text(original)
    with pytest.raises(EnhanceError, match="already has a binding"):
        install_config(config, ["dbt-enhance"])
    assert config.read_text() == original
    assert list(tmp_path.glob("*.bak")) == []
    backup = install_config(config, ["dbt-enhance"], force=True)
    assert backup.read_text() == original


def test_invalid_toml_and_scalar_prefix_are_not_overwritten(tmp_path):
    config = tmp_path / "config.toml"
    for text in ('[keys.normal]\nspace = "quit"\n', "not toml !!!"):
        config.write_text(text)
        with pytest.raises(EnhanceError):
            prepare_config(config, ["dbt-enhance"])
        assert config.read_text() == text


@pytest.mark.parametrize(
    "filename",
    [
        "models/orders.sql",
        "models/some space.sql",
        "models/Connor's.sql",
        'models/a"b`uname`$HOME.sql',
        "models/百分比%{selection}.sql",
    ],
)
def test_binding_transports_filename_and_sql_as_data(tmp_path, filename):
    receiver = tmp_path / "receive.py"
    receiver.write_text("import sys, json; print(json.dumps([sys.argv[1:], sys.stdin.read()]))")
    command = binding([sys.executable, str(receiver)])
    shell = command.removeprefix(":pipe-to ").replace("%{buffer_name}", filename)
    sql = "select '{{ ref(\"orders\") }}', '$HOME', '`id`', 'a; b'"
    result = subprocess.run(
        ["sh", "-c", shell], input=sql, capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == [["popup", "--file", filename], sql]


def test_filename_cannot_inject_a_shell_command(tmp_path):
    marker = tmp_path / "INJECTED"
    receiver = tmp_path / "receiver.py"
    receiver.write_text("import sys; print(sys.argv[-1])")
    filename = (
        f"models/'; touch {shlex.quote(str(marker))}; $(touch {shlex.quote(str(marker))}).sql"
    )
    shell = binding([sys.executable, str(receiver)]).removeprefix(":pipe-to ")
    result = subprocess.run(
        ["sh", "-c", shell.replace("%{buffer_name}", filename)],
        input="select 1",
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.rstrip("\n") == filename
    assert not marker.exists()


def test_new_config_creates_both_modes(tmp_path):
    path = tmp_path / "new" / "config.toml"
    assert install_config(path, ["dbt-enhance"]) is None
    value = tomlkit.parse(path.read_text())
    assert set(value["keys"]) == {"normal", "select"}
