"""Bounded-memory literal navigation over a complete fetched text value."""

from __future__ import annotations

import re


class LiteralSearch:
    """Keep only the current span, not an unbounded list of every occurrence.

    Escaped patterns provide Unicode case-insensitive matching without treating
    user input as regex syntax. A lazily reversed source makes previous-match
    navigation linear in source size rather than in the number of matches.
    """

    def __init__(self, text: str, literal: str) -> None:
        self.text = text
        self.literal = literal
        self.span: tuple[int, int] | None = None
        self._pattern = re.compile(re.escape(literal), re.IGNORECASE) if literal else None
        self._reverse_pattern = (
            re.compile(re.escape(literal[::-1]), re.IGNORECASE) if literal else None
        )
        self._reversed: str | None = None

    def step(self, *, previous: bool = False) -> tuple[int, int] | None:
        if self._pattern is None:
            return None
        if previous:
            if self._reversed is None:
                self._reversed = self.text[::-1]
            assert self._reverse_pattern is not None
            offset = len(self.text) - self.span[1] + 1 if self.span else 0
            match = self._reverse_pattern.search(self._reversed, offset)
            if match is None:
                match = self._reverse_pattern.search(self._reversed)
            if match is None:
                return None
            self.span = (len(self.text) - match.end(), len(self.text) - match.start())
        else:
            offset = self.span[0] + 1 if self.span else 0
            match = self._pattern.search(self.text, offset) or self._pattern.search(self.text)
            if match is None:
                return None
            self.span = match.span()
        return self.span
