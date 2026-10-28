#!/usr/bin/env python3
"""Standalone native dbt configuration probe.

The project interpreter runs this file directly.  Input and output are bounded
JSON streams; selected SQL, credentials, and exception metadata are never part
of this protocol.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

_LIMIT = 64 * 1024


def _read() -> dict[str, Any]:
    data = bytearray()
    while True:
        chunk = os.read(0, min(65536, _LIMIT + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > _LIMIT:
            raise ValueError
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError
    return value


def _load_session() -> Any:
    path = Path(__file__).resolve().with_name("_session.py")
    spec = importlib.util.spec_from_file_location("_dbt_enhance_project_session", path)
    if spec is None or spec.loader is None:
        raise RuntimeError
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # Do not allow the helper directory's dbt.py (in an installed wheel) to
    # shadow the project's real dbt package.
    helper_directory = str(path.parent)
    sys.path[:] = [entry for entry in sys.path if os.path.realpath(entry) != helper_directory]
    return module


def main() -> int:
    try:
        context = _read()
        session = _load_session()
        resolved = session.resolve_configuration(context)
        payload = {"ok": True, **resolved}
    except BaseException as error:
        try:
            target_denied = isinstance(error, session.TargetNotAllowed)
            unsupported = isinstance(error, session.UnsupportedSession)
        except BaseException:
            target_denied = unsupported = False
        payload = {
            "ok": False,
            "code": (
                "target_not_allowed"
                if target_denied
                else "unsupported_environment"
                if unsupported
                else "preflight_failed"
            ),
            "message": (
                session._scrub_error(error)[:4096]
                if unsupported
                else "Native dbt configuration could not be resolved safely."
            ),
        }
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) > _LIMIT:
        encoded = (
            b'{"ok":false,"code":"preflight_failed","message":'
            b'"Native dbt configuration response exceeded its safety limit."}'
        )
    os.write(1, encoded)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
