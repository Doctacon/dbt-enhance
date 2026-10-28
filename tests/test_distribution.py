"""Source-install cache and standalone-helper compatibility contracts."""

import ast
import fnmatch
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_support_policy_changes_invalidate_source_install_cache():
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text())
    patterns = [item["file"] for item in configuration["tool"]["uv"]["cache-keys"]]
    assert any(
        fnmatch.fnmatchcase("src/dbt_enhance/support-matrix.json", pattern) for pattern in patterns
    ), "changing certification evidence must not reuse a stale source-install wheel"


@pytest.mark.parametrize("helper", ["_wire", "_session", "_worker_helper", "_project_helper"])
def test_project_helpers_remain_python_310_syntax_compatible(helper):
    source = ROOT / "src/dbt_enhance" / f"{helper}.py"
    ast.parse(source.read_text(), filename=str(source), feature_version=(3, 10))
