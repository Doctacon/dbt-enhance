"""Private request transport into a tmux-owned terminal (not Helix's pipes)."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from dbt_enhance.models import EnhanceError, QueryRequest

# These belong to the popup's terminal, not the editor's terminal.
_TERMINAL_ENV = {"TERM", "TERM_PROGRAM", "TMUX", "TMUX_PANE", "LINES", "COLUMNS"}


def tmux_binary(*, require_session: bool = True) -> str:
    executable = shutil.which("tmux")
    if not executable:
        raise EnhanceError(
            "tmux 3.3+ is required for popups. Install tmux, then run Helix inside tmux."
        )
    version = subprocess.run([executable, "-V"], capture_output=True, text=True, check=False)
    match = re.search(r"tmux (\d+)\.(\d+)", version.stdout)
    if version.returncode or not match or tuple(map(int, match.groups())) < (3, 3):
        raise EnhanceError("tmux 3.3+ is required. Upgrade tmux before using popups.")
    if require_session and not os.environ.get("TMUX"):
        raise EnhanceError(
            "No tmux session. Run `tmux new-session -A -s work`, then launch Yazi or Helix there. "
            "For a standalone result screen, use `dbt-enhance query --file ...` instead."
        )
    return executable


def launch_popup(request: QueryRequest) -> None:
    """Block until the popup closes, then remove all transient request data."""
    executable = tmux_binary()
    with tempfile.TemporaryDirectory(prefix="dbt-enhance-popup-") as directory:
        path = Path(directory) / "request.json"
        data = asdict(request)
        for key in ("file", "profiles_dir", "dbt"):
            if data[key] is not None:
                data[key] = str(data[key])
        # tmux's server environment may be old. Pass the editor's current env
        # privately, not as -e arguments exposing credentials in process listings.
        payload = {"request": data, "environment": dict(os.environ)}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
        child = [sys.executable, "-m", "dbt_enhance", "_view", str(path), "--consume"]
        command = [
            executable,
            "display-popup",
            "-E",
            "-w",
            "92%",
            "-h",
            "88%",
            "-T",
            " dbt-enhance ",
            "-d",
            str(request.file.parent),
        ]
        if os.environ.get("TMUX_PANE"):
            command += ["-t", os.environ["TMUX_PANE"]]
        command.append(shlex.join(child))
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode:
            message = completed.stderr.strip() or "The popup could not open or its process failed."
            raise EnhanceError(f"tmux: {message}")


def read_request(path: Path, *, consume: bool = False) -> QueryRequest:
    """Read a private handoff; optionally delete it immediately after decoding."""
    try:
        payload = json.loads(path.read_text())
        data = payload["request"]
        environment = payload["environment"]
        if not isinstance(environment, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and "=" not in k and "\0" not in k + v
            for k, v in environment.items()
        ):
            raise ValueError("invalid environment")
        for key in ("file", "profiles_dir", "dbt"):
            if data[key] is not None:
                data[key] = Path(data[key])
        request = QueryRequest(**data)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise EnhanceError(f"Invalid popup request: {exc}") from exc
    if consume:
        path.unlink()
    terminal = {key: value for key, value in os.environ.items() if key in _TERMINAL_ENV}
    os.environ.clear()
    os.environ.update(
        {key: value for key, value in environment.items() if key not in _TERMINAL_ENV}
    )
    os.environ.update(terminal)
    return request
