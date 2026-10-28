"""Opt-in real Helix → tmux popup → dbt smoke test using a private tmux server.

Run with DBT_ENHANCE_EDITOR_SMOKE=1 uv run pytest tests/test_editor_smoke.py -v.
No user config, sessions, profiles or warehouse connections are touched.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import shlex
import shutil
import struct
import subprocess
import sys
import termios
import time
import uuid
from pathlib import Path

import pytest

from dbt_enhance.helix import install_config, remove_owned_binding

pytestmark = pytest.mark.skipif(
    os.environ.get("DBT_ENHANCE_EDITOR_SMOKE") != "1"
    or not shutil.which("hx")
    or not shutil.which("tmux"),
    reason="opt-in smoke test requires Helix 25.07+ and tmux 3.3+",
)


def test_selected_jinja_reaches_popup_without_changing_buffer(tmp_path: Path):
    project = tmp_path / "dbt project's $workspace"
    models = project / "models"
    models.mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        "name: editor_smoke\nversion: '1.0'\nconfig-version: 2\nprofile: editor_smoke\n"
    )
    (project / "profiles.yml").write_text(
        "editor_smoke:\n  target: dev\n  outputs:\n    dev:\n"
        "      type: duckdb\n      path: ':memory:'\n      threads: 1\n"
    )
    # Quotes/dollars must stay data, and the unselected SQL must not be executed.
    model = models / "query.sql"
    original = "select {{ 6 * 7 }} as selected_value\nthis is deliberately invalid SQL\n"
    model.write_text(original)
    config = tmp_path / "config.toml"
    config.write_text('[editor]\nshell = ["/bin/sh", "-c"]\n')
    state_root = tmp_path / "state"
    install_config(
        config,
        [sys.executable, "-m", "dbt_enhance"],
        state_root=state_root,
        helix_shell_bridge=True,
    )
    socket = f"dbt-enhance-test-{uuid.uuid4().hex}"
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
    environment = {
        **os.environ,
        "TERM": "xterm-256color",
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "DBT_ENHANCE_DBT": str(Path(sys.executable).parent / "dbt"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)
    command = [
        "tmux",
        "-L",
        socket,
        "-f",
        "/dev/null",
        "new-session",
        "-s",
        "smoke",
        "-x",
        "140",
        "-y",
        "40",
        "hx",
        "-c",
        str(config),
        str(model),
    ]
    process = subprocess.Popen(
        command,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=environment,
        start_new_session=True,
    )
    os.close(slave)
    output = bytearray()

    def visible_output() -> bytes:
        # tmux/Helix may insert color sequences between individual SQL tokens.
        return re.sub(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|[()][0-2A-Z])", b"", output)

    def wait_for(fragment: bytes, seconds: float = 30) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            visible = visible_output()
            if fragment in visible:
                return
            if fragment == b"Complete" and b"Could not run query" in visible:
                # Read the error detail already waiting behind the status update.
                time.sleep(0.2)
                if select.select([master], [], [], 0.1)[0]:
                    output.extend(os.read(master, 65536))
                break
            if select.select([master], [], [], 0.1)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                output.extend(data)
                # Helix may ask the terminal for its current cursor position.
                if b"\x1b[6n" in data:
                    os.write(master, b"\x1b[1;1R")
            if process.poll() is not None:
                break
        pytest.fail(
            f"Terminal never displayed {fragment!r}:\n"
            f"{visible_output().decode(errors='replace')[-10000:]}"
        )

    try:
        wait_for(b"selected_value")
        time.sleep(0.5)
        # x selects only the current line. Space x invokes our normal-mode mapping.
        os.write(master, b"x x")
        wait_for(b"dbt-enhance")
        wait_for(b"Complete", 60)
        wait_for(b"42")  # The table may arrive in a later terminal write than the status.
        output.clear()
        os.write(master, b"s")
        wait_for(b"select 42 as selected_value")
        output.clear()
        os.write(master, b"q")
        wait_for(b"deliberately invalid SQL", 10)
        # Save the unchanged buffer too: this catches accidental editor replacement,
        # not merely an implementation that leaves the on-disk file untouched.
        os.write(master, b":wq\r")
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    os.read(master, 65536)
                except OSError:
                    break
        process.wait(timeout=2)
        assert model.read_text() == original
        remove_owned_binding(
            config,
            [sys.executable, "-m", "dbt_enhance"],
            state_root=state_root,
        )
        assert 'shell = ["/bin/sh", "-c"]' in config.read_text()
        assert not (state_root / "helix-shell-bridge-v2").exists()
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True, check=False)
        os.close(master)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def test_newline_filename_exploit_never_reaches_shell_popup_or_query(tmp_path: Path):
    real_tmux = shutil.which("tmux")
    assert real_tmux is not None
    marker = tmp_path / "OWNED"
    popup_marker = tmp_path / "POPUP"
    query_marker = tmp_path / "QUERY"
    # The payload can only create a marker in this test's private working directory.
    malicious = tmp_path / (f"model.sql\nDBT_ENHANCE_FILE_{'1' * 32}\ntouch OWNED")
    malicious.write_text("select 1\n")

    config = tmp_path / "config.toml"
    state_root = tmp_path / "state"
    install_config(
        config,
        [sys.executable, "-m", "dbt_enhance"],
        state_root=state_root,
        helix_shell_bridge=True,
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/bin/sh\n"
        f"case \" $* \" in *' display-popup '*) touch {shlex.quote(str(popup_marker))} ;; esac\n"
        f'exec {shlex.quote(real_tmux)} "$@"\n'
    )
    fake_tmux.chmod(0o700)
    fake_dbt = fake_bin / "dbt"
    fake_dbt.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(query_marker))}\nexit 99\n")
    fake_dbt.chmod(0o700)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "TERM": "xterm-256color",
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "XDG_RUNTIME_DIR": str(runtime),
        "DBT_ENHANCE_DBT": str(fake_dbt),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)
    socket = f"dbt-enhance-exploit-{uuid.uuid4().hex}"
    target = "exploit"
    try:
        started = subprocess.run(
            [
                real_tmux,
                "-L",
                socket,
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "exploit",
                "-x",
                "120",
                "-y",
                "30",
                "hx",
                "-c",
                str(config),
                str(malicious),
            ],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pane = subprocess.run(
                [real_tmux, "-L", socket, "capture-pane", "-p", "-t", target],
                capture_output=True,
                text=True,
                check=False,
            )
            if "select 1" in pane.stdout:
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"malicious Helix buffer did not open: {pane.stdout}\n{pane.stderr}")
        subprocess.run(
            [real_tmux, "-L", socket, "send-keys", "-t", target, "-l", "x x"],
            check=True,
        )
        time.sleep(3)
        assert not marker.exists()
        assert not popup_marker.exists()
        assert not query_marker.exists()
    finally:
        subprocess.run([real_tmux, "-L", socket, "kill-server"], capture_output=True, check=False)
