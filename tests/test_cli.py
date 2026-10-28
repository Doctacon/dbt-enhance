import json
import subprocess
from types import SimpleNamespace

import pytest
import tomlkit
from typer.testing import CliRunner

from dbt_enhance import install
from dbt_enhance.cli import app
from dbt_enhance.models import EnhanceError

runner = CliRunner()


@pytest.mark.parametrize("running", [False, True])
def test_doctor_reports_last_retirement_without_starting_worker(tmp_path, monkeypatch, running):
    from dbt_enhance import cli, dbt, popup, worker

    monkeypatch.setenv("TMUX", "synthetic-test-session")
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/test/bin/hx")
    monkeypatch.setattr(popup, "tmux_binary", lambda **_: "/test/bin/tmux")
    monkeypatch.setattr(dbt, "discover_project", lambda _: tmp_path)
    monkeypatch.setattr(dbt, "find_dbt", lambda *_: tmp_path / "dbt")
    monkeypatch.setattr(worker, "project_python", lambda _: tmp_path / "python")
    monkeypatch.setattr(
        worker, "status", lambda _: {"pid": 101, "session_pid": 102} if running else None
    )
    monkeypatch.setattr(
        worker,
        "retirement_status",
        lambda _: {
            "reason": "connection_not_open",
            "session_pid": 100,
        },
    )
    monkeypatch.setattr(worker, "_spawn", lambda *_: pytest.fail("doctor started a worker"))
    result = runner.invoke(app, ["doctor", "--file", str(tmp_path / "model.sql")])
    assert result.exit_code == 0, result.output
    assert "last worker retirement: connection_not_open (session 100)" in result.output


def test_config_validate_is_machine_readable_and_never_starts_dbt(tmp_path, monkeypatch):
    from dbt_enhance import dbt

    path = tmp_path / "preferences.toml"
    path.write_text("schema = 1\n[global]\nlimit = 42\n")
    monkeypatch.setattr(dbt, "run_query", lambda *_: pytest.fail("config executed SQL"))
    result = runner.invoke(app, ["config", "validate", "--config", str(path), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["schema"] == 1
    assert payload["helix_bridge_format"] == 2


def test_help_and_version():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("query", "popup", "setup", "doctor", "demo"):
        assert name in result.stdout
    assert "_view" not in result.stdout
    assert "_helix" not in result.stdout
    setup_help = runner.invoke(app, ["setup", "--help"], terminal_width=140)
    assert setup_help.exit_code == 0
    assert "--helix-shell-brid" in setup_help.stdout
    assert "editor.shell" in setup_help.stdout
    assert "owned fail-closed" in setup_help.stdout
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.2" in result.stdout


@pytest.mark.parametrize(
    "arguments,sql,fragment",
    [
        (["--file", "[scratch]"], "select 1", "Save this buffer"),
        (["--file", "model.sql"], "  \n", "selection is empty"),
        (["--file", "model.sql", "--limit", "0"], "select 1", "1"),
        (["--file", "model.sql", "--timeout", "nan"], "select 1", "finite"),
        (["--file", "model.sql"], "x" * 1_048_577, "exceeds 1 MiB"),
    ],
)
def test_invalid_requests_fail_before_running(arguments, sql, fragment):
    result = runner.invoke(app, ["query", "--json", *arguments], input=sql)
    assert result.exit_code != 0
    assert fragment in result.output


def test_hidden_helix_helper_roundtrips_filename_and_exact_sql(tmp_path, monkeypatch):
    from dbt_enhance import helix, popup

    captured = []
    monkeypatch.setattr(popup, "launch_popup", captured.append)
    monkeypatch.chdir(tmp_path)
    filename = "models/a 'quote' $cash `tick` %{buffer_name} ü.sql"
    raw = helix.binding("a" * 64).removeprefix(":pipe-to ").replace("%{buffer_name}", filename)
    sql = "select '{{ ref(\"orders\") }}', '$HOME'\n"
    result = runner.invoke(app, ["_helix", raw], input=sql)
    assert result.exit_code == 0, result.output
    assert captured[0].file == tmp_path / filename
    assert captured[0].sql == sql


def test_hidden_helix_helper_rejects_old_delimiter_newline_before_popup(tmp_path, monkeypatch):
    from dbt_enhance import helix, popup

    marker = tmp_path / "OWNED"
    monkeypatch.setattr(popup, "launch_popup", lambda _: pytest.fail("popup was invoked"))
    raw = (
        helix.binding("b" * 64)
        .removeprefix(":pipe-to ")
        .replace(
            "%{buffer_name}",
            f"model.sql\nDBT_ENHANCE_FILE_{'1' * 32}\ntouch {marker}",
        )
    )
    result = runner.invoke(app, ["_helix", raw], input="select 1")
    assert result.exit_code == 1
    assert "newlines" in result.output
    assert not marker.exists()


def test_hidden_helix_helper_rejects_unknown_commands(monkeypatch):
    from dbt_enhance import popup

    monkeypatch.setattr(popup, "launch_popup", lambda _: pytest.fail("popup was invoked"))
    result = runner.invoke(app, ["_helix", "printf attacker"], input="select 1")
    assert result.exit_code == 1
    assert "unknown Helix shell-bridge command" in result.output


def test_popup_receives_exact_selection_and_absolute_buffer(tmp_path, monkeypatch):
    from dbt_enhance import popup

    captured = []
    monkeypatch.setattr(popup, "launch_popup", captured.append)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["popup", "--file", "models/my query.sql"], input="select {{ 1 + 2 }}\n"
    )
    assert result.exit_code == 0, result.output
    assert captured[0].sql == "select {{ 1 + 2 }}\n"
    assert captured[0].file == tmp_path / "models/my query.sql"


@pytest.mark.parametrize(
    "direct,expected",
    [
        (None, "dbt-enhance==0.1.0"),
        ({"url": "file:///tmp/local%20project", "dir_info": {}}, "file:///tmp/local%20project"),
        (
            {
                "url": "https://example.com/repo.git",
                "vcs_info": {"vcs": "git", "commit_id": "abc123"},
            },
            "git+https://example.com/repo.git@abc123",
        ),
    ],
)
def test_install_source_follows_uvx_provenance(monkeypatch, direct, expected):
    class Package:
        version = "0.1.0"

        def read_text(self, _):
            return json.dumps(direct) if direct else None

    monkeypatch.setattr(install, "distribution", lambda _: Package())
    assert install.installation_source() == expected


def test_setup_refuses_conflicts_before_installing(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    original = '[keys.select.space]\nx = "quit"\n'
    config.write_text(original)
    commands = []
    monkeypatch.setattr(install.shutil, "which", lambda _: "uv")

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, str(tmp_path / "bin"), "")

    monkeypatch.setattr(install.subprocess, "run", run)
    with pytest.raises(EnhanceError, match="already has a binding"):
        install.setup(
            config,
            source=".",
            helix_shell_bridge=True,
            state_root=tmp_path / "state",
        )
    assert len(commands) == 1
    assert commands[0] == ["uv", "tool", "dir", "--bin"]
    assert config.read_text() == original


def test_setup_installs_stable_tool_before_config(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    binary = tmp_path / "bin" / "dbt-enhance"
    binary.parent.mkdir()
    commands = []
    monkeypatch.setattr(install.shutil, "which", lambda _: "uv")

    def run(command, **kwargs):
        commands.append(command)
        if "install" in command:
            binary.touch()
            return subprocess.CompletedProcess(command, 0, "", "")
        if "validate" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "valid": True,
                        "schema": 1,
                        "version": "0.1.2",
                        "helix_bridge_format": 2,
                    }
                ),
                "",
            )
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "0.1.2\n", "")
        return subprocess.CompletedProcess(command, 0, str(binary.parent), "")

    monkeypatch.setattr(install.subprocess, "run", run)
    installed, backup = install.setup(
        config,
        source="file:///tmp/package",
        helix_shell_bridge=True,
        state_root=tmp_path / "state",
    )
    assert installed == binary
    assert backup is None
    assert ["uv", "tool", "install", "--force", "file:///tmp/package"] in commands
    parsed = tomlkit.parse(config.read_text())
    assert parsed["editor"]["shell"] == [str(tmp_path / "state" / "helix-shell-bridge-v2")]


def test_worker_backend_is_default_with_explicit_process_opt_out(tmp_path, monkeypatch):
    from dbt_enhance import popup

    captured = []
    monkeypatch.setattr(popup, "launch_popup", captured.append)
    args = ["popup", "--file", str(tmp_path / "model.sql")]
    assert runner.invoke(app, args, input="select 1").exit_code == 0
    assert captured[-1].backend == "worker"
    assert runner.invoke(app, [*args, "--backend", "process"], input="select 1").exit_code == 0
    assert captured[-1].backend == "process"
    monkeypatch.setenv("DBT_ENHANCE_BACKEND", "process")
    assert runner.invoke(app, args, input="select 1").exit_code == 0
    assert captured[-1].backend == "process"
    assert runner.invoke(app, [*args, "--backend", "worker"], input="select 1").exit_code == 0
    assert captured[-1].backend == "worker"
    assert runner.invoke(app, [*args, "--backend", "invalid"], input="select 1").exit_code != 0


def test_query_applies_user_project_settings_and_keeps_safety_independent(tmp_path, monkeypatch):
    from dbt_enhance import popup
    from dbt_enhance.settings import settings_path

    project = tmp_path / "project"
    project.mkdir()
    (project / "dbt_project.yml").write_text("name: example\n")
    path = settings_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        "schema = 1\n[global]\nlimit = 80\nworker_idle_seconds = 45\n"
        f'[[project]]\npath = "{project}"\nlimit = 60\ntheme = "light"\n'
        f'[[safety.project_targets]]\nproject = "{project}"\ntargets = ["dev"]\n'
    )
    seen = []
    monkeypatch.setattr(popup, "launch_popup", seen.append)
    monkeypatch.setenv("DBT_ENHANCE_LIMIT", "40")
    args = ["popup", "--file", str(project / "model.sql")]
    result = runner.invoke(app, [*args, "--limit", "20"], input="select 1")
    assert result.exit_code == 0, result.output
    assert seen[-1].limit == 20
    assert seen[-1].worker_idle_seconds == 45
    assert seen[-1].theme == "light"
    assert seen[-1].allowed_targets == ("dev",)
    result = runner.invoke(app, args, input="select 1")
    assert result.exit_code == 0, result.output
    assert seen[-1].limit == 40


def test_query_command_uses_worker_default(tmp_path, monkeypatch):
    from dbt_enhance import ui

    captured = []

    class FakeApp:
        def __init__(self, *, request):
            captured.append(request)

        def run(self):
            pass

    monkeypatch.setattr(ui, "QueryApp", FakeApp)
    args = ["query", "--file", str(tmp_path / "model.sql"), "--sql", "select 1"]
    assert runner.invoke(app, args).exit_code == 0
    assert captured[-1].backend == "worker"
    assert runner.invoke(app, [*args, "--backend", "process"]).exit_code == 0
    assert captured[-1].backend == "process"


def test_stop_resolves_project_without_starting_a_query(tmp_path, monkeypatch):
    from dbt_enhance import worker

    (tmp_path / "dbt_project.yml").touch()
    seen = []
    monkeypatch.setattr(worker, "stop", lambda project: seen.append(project) or True)
    result = runner.invoke(app, ["stop", "--file", str(tmp_path / "model.sql")])
    assert result.exit_code == 0, result.output
    assert "stopped" in result.stdout
    assert seen == [tmp_path.resolve()]


def test_config_explain_reports_precedence_sources_and_separate_safety(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "dbt_project.yml").write_text("name: safe\n")
    config_home = tmp_path / "config"
    preferences = config_home / "dbt-enhance" / "config.toml"
    preferences.parent.mkdir(parents=True)
    preferences.write_text(
        'schema = 1\n[global]\nlimit = 90\ntheme = "dark"\n'
        f'[[project]]\npath = "{project}"\nlimit = 70\n'
        f'[[safety.project_targets]]\nproject = "{project}"\ntargets = ["dev"]\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("DBT_ENHANCE_LIMIT", "50")
    result = runner.invoke(
        app,
        ["config", "explain", "--file", str(project / "model.sql"), "--limit", "30", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["values"]["limit"] == 30
    assert payload["sources"]["limit"] == "cli"
    assert payload["values"]["theme"] == "dark"
    assert payload["sources"]["theme"] == "global"
    assert payload["allowed_targets"] == ["dev"]
    assert "allowed_targets" not in payload["values"]


def test_diagnostics_report_is_strictly_shareable_metadata(tmp_path, monkeypatch):
    from dbt_enhance.diagnostics import DiagnosticStore

    project = tmp_path / "customer-secret-project"
    project.mkdir()
    (project / "dbt_project.yml").touch()
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    store = DiagnosticStore()
    store.record_failure(
        project,
        {
            "outcome": "failure",
            "stage": "execution",
            "reason": "sql_error",
            "backend": "worker",
            "raw_error": "password=secret from customer_table",
            "sql": "select * from customer_table",
            "target": "production-customer",
            "path": str(project),
        },
    )
    result = runner.invoke(
        app, ["diagnostics", "report", "--file", str(project / "model.sql"), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["failures"][0]["reason"] == "sql_error"
    for secret in ("customer-secret-project", "customer_table", "production-customer", "password"):
        assert secret not in result.stdout


def test_doctor_query_mode_never_requires_popup_tools_or_queries_by_default(tmp_path, monkeypatch):
    from dbt_enhance import cli, dbt

    monkeypatch.setattr(cli.shutil, "which", lambda _: None)
    monkeypatch.setattr(dbt, "run_query", lambda *_: pytest.fail("doctor queried without consent"))
    result = runner.invoke(app, ["doctor", "--mode", "query"])
    assert result.exit_code == 0, result.output
    assert "Helix" not in result.stdout
    assert "tmux" not in result.stdout
    assert "backend/support" in result.stdout


def test_setup_validates_prerequisites_but_does_not_query_without_live_smoke(tmp_path, monkeypatch):
    from dbt_enhance import cli, dbt, install, popup

    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/test/bin/{name}")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **_: subprocess.CompletedProcess(command, 0, "helix 25.07.1\n", ""),
    )
    monkeypatch.setattr(popup, "tmux_binary", lambda **_: "/test/bin/tmux")
    monkeypatch.setattr(dbt, "run_query", lambda *_: pytest.fail("setup queried without consent"))
    seen = []

    def fake_setup(path, **kwargs):
        seen.append((path, kwargs))
        return tmp_path / "bin" / "dbt-enhance", None

    monkeypatch.setattr(install, "setup", fake_setup)
    result = runner.invoke(app, ["setup", "--helix-shell-bridge"])
    assert result.exit_code == 0, result.output
    assert "readiness is not asserted" in result.stdout
    assert seen[0][1]["shortcut"] == "space.x"
    assert seen[0][1]["helix_shell_bridge"] is True
    assert seen[0][1]["state_root"] == tmp_path / "state" / "dbt-enhance"


def test_setup_rejects_unsupported_helix_before_install(tmp_path, monkeypatch):
    from dbt_enhance import cli, install, popup

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/test/bin/{name}")
    monkeypatch.setattr(popup, "tmux_binary", lambda **_: "/test/bin/tmux")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **_: subprocess.CompletedProcess(command, 0, "helix 24.03\n", ""),
    )
    monkeypatch.setattr(install, "setup", lambda *_args, **_kwargs: pytest.fail("setup ran"))

    result = runner.invoke(app, ["setup", "--helix-shell-bridge"])
    assert result.exit_code == 1
    assert "Helix 25.07 or newer" in result.output


def test_lifecycle_commands_are_preview_first_and_partial_errors_fail(monkeypatch):
    from dbt_enhance import lifecycle

    plan = SimpleNamespace(effects=("replace package",), workers=())
    applied = []
    monkeypatch.setattr(lifecycle, "preview_update", lambda *_, **__: plan)
    monkeypatch.setattr(
        lifecycle,
        "apply_update",
        lambda value: (
            applied.append(value)
            or lifecycle.LifecycleResult("update", ("package installed",), ("manifest failed",))
        ),
    )

    preview = runner.invoke(app, ["update", "--source", "explicit-ref"])
    assert preview.exit_code == 0, preview.output
    assert "replace package" in preview.stdout
    assert "Dry run only" in preview.stdout
    assert applied == []

    applied_result = runner.invoke(app, ["update", "--source", "explicit-ref", "--apply"])
    assert applied_result.exit_code == 1
    assert applied == [plan]
    assert "DONE  package installed" in applied_result.stdout
    assert "ERROR manifest failed" in applied_result.output
