"""Display-only sanitization; original values remain untouched for copying."""

import re

# Keep terminal escapes and invisible C0/C1 controls out of Rich and the terminal.
# Multiline source views deliberately retain real newlines and tab layout.
_UNSAFE_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean_inline(value: object, *, cap: int | None = None) -> str:
    """Return plain safe text for a cell or label, never Rich markup."""
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 - arbitrary returned values may have broken __str__
        text = "<unprintable value>"
    text = text.replace("\t", "⇥").replace("\n", "↵").replace("\r", "␍")
    text = _UNSAFE_CONTROLS.sub("�", text)
    if cap is not None and len(text) > cap:
        return f"{text[: max(0, cap - 1)]}…"
    return text


def safe_multiline(value: object, *, cap: int | None = None) -> str:
    """Return literal safe text without changing real layout or visible glyphs."""
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 - native errors may contain arbitrary driver values
        text = "<unprintable value>"
    text = text.replace("\r\n", "\n").replace("\r", "␍")
    text = _UNSAFE_CONTROLS.sub("�", text)
    if cap is not None and len(text) > cap:
        return f"{text[: max(0, cap - 1)]}…"
    return text
