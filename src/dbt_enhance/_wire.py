"""Bounded JSON framing shared by the tool and standalone project workers.

Keep this module stdlib-only and compatible with the project's Python 3.10+.
It must also be importable without installing dbt-enhance in that environment.
"""

from __future__ import annotations

import json
import struct
from typing import Any

PROTOCOL = 1
MAX_FRAME = 16 * 1024 * 1024
MAX_REQUEST = 8 * 1024 * 1024  # JSON escaping can expand a 1 MiB SQL selection sixfold.
MAX_SQL = 1024 * 1024
IDLE_SECONDS = 600.0

# Retirement diagnostics are persisted across worker replacement. Keep this
# deliberately closed: protocol peers must never turn exception text, SQL, or
# adapter-provided values into durable state.
RETIREMENT_REASONS = frozenset(
    {
        "request_setup_failed",
        "non_dbt_error",
        "driver_error_missing",
        "sqlstate_not_recoverable",
        "connection_not_open",
        "recovery_check_failed",
        "request_cleanup_failed",
        "session_error",
        "worker_failure",
        "timeout",
        "client_disconnect",
    }
)


def validate_retirement_reason(value: object) -> str | None:
    """Return only a fixed, non-sensitive retirement reason code."""
    return value if isinstance(value, str) and value in RETIREMENT_REASONS else None


class WireError(Exception):
    """Invalid, incomplete, or oversized worker transport (never include its data)."""


def encode(message: dict[str, Any], maximum: int = MAX_FRAME) -> bytes:
    try:
        # dbt/Agate previews use Python JSON's NaN/Infinity extension. Preserve
        # those numeric values; finite control fields are validated separately.
        data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise WireError("Worker message could not be serialized.") from exc
    if not data or len(data) > maximum:
        raise WireError("Worker message exceeded its transport limit; lower the preview limit.")
    return struct.pack("!I", len(data)) + data


class Decoder:
    """Incremental framing: socket timeouts never discard a partly received message."""

    def __init__(self, maximum: int = MAX_FRAME) -> None:
        self.maximum = maximum
        self.buffer = bytearray()

    def feed(self, data: bytes) -> None:
        self.buffer.extend(data)
        # Callers read at most 64 KiB and pop completed frames between reads.
        if len(self.buffer) > self.maximum + 4 + 65536:
            raise WireError("Worker message exceeded its transport limit.")
        self._size()

    def _size(self) -> int | None:
        if len(self.buffer) < 4:
            return None
        size = struct.unpack("!I", self.buffer[:4])[0]
        if not 0 < size <= self.maximum:
            raise WireError("Worker message exceeded its transport limit.")
        return size

    def pop(self) -> dict[str, Any] | None:
        size = self._size()
        if size is None or len(self.buffer) < size + 4:
            return None
        data = bytes(self.buffer[4 : size + 4])
        del self.buffer[: size + 4]
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise WireError("Worker sent malformed JSON.") from exc
        if not isinstance(value, dict):
            raise WireError("Worker message must be a JSON object.")
        return value
