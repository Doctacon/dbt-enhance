"""Keep disposable test projects independent of the developer's artifact/cache paths."""

import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def isolated_dbt_paths(monkeypatch):
    for name in (
        "DBT_TARGET_PATH",
        "DBT_LOG_PATH",
        "DBT_PARTIAL_PARSE",
        "DBT_PARTIAL_PARSE_FILE_PATH",
        "DBT_ENHANCE_BACKEND",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolated_tool_settings(tmp_path, monkeypatch):
    """Never read or write the operator's preferences/diagnostic history in tests."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "tool-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "tool-state"))
    for name in tuple(os.environ):
        if name.startswith("DBT_ENHANCE_") and name != "DBT_ENHANCE_EDITOR_SMOKE":
            monkeypatch.delenv(name, raising=False)
    # A short owned path fits Unix socket limits and never inventories or stops
    # workers belonging to the developer's real projects.
    with tempfile.TemporaryDirectory(prefix="dbe-test-", dir="/tmp") as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        yield
