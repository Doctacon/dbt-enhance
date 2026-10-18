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
import shutil
import struct
import subprocess
import sys
import termios
import time
import uuid
from pathlib import Path

import pytest

from dbt_enhance.helix import install_config

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
    install_config(config, [sys.executable, "-m", "dbt_enhance"])
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
