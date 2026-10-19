"""Keep disposable test projects independent of the developer's artifact/cache paths."""

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
