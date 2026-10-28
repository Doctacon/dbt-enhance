"""Pure, local result-preview transforms.

This module deliberately does not know about Textual or dbt.  It retains source
row/column identities so a UI can safely render a transformed view while copying
the original value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from numbers import Number
from typing import Literal

from .models import QueryResult

Kind = Literal["unknown", "text", "boolean", "number", "date", "datetime", "time"]


@dataclass(frozen=True)
class ColumnFilter:
    """One simple AND predicate.  ``column`` accepts a source column name or index."""

    column: str | int
    operator: str
    value: object = None


@dataclass(frozen=True)
class PreviewColumn:
    index: int
    name: str
    kind: str
    width: int
    pinned: bool = False


@dataclass(frozen=True)
class PreviewRow:
    index: int
    values: tuple[object, ...]


@dataclass(frozen=True)
class PreviewView:
    columns: tuple[PreviewColumn, ...]
    rows: tuple[PreviewRow, ...]
    fetched_count: int
    filtered_count: int
    cap: int


@dataclass
class PreviewModel:
    """Ephemeral projection of a :class:`QueryResult`, with no type guessing."""

    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    column_types: tuple[str, ...]
    cap: int
    _filters: list[ColumnFilter] = field(default_factory=list)
    _sort: tuple[int, str] | None = None
    _hidden: set[int] = field(default_factory=set)
    _widths: dict[int, int] = field(default_factory=dict)
    _pin_end: int | None = None

    @classmethod
    def from_result(cls, result: QueryResult) -> PreviewModel:
        kinds = tuple(result.column_types)
        # Metadata can be absent or incomplete; unknown explicitly disables ordered filters.
        kinds = tuple(
            k
            if k in {"unknown", "text", "boolean", "number", "date", "datetime", "time"}
            else "unknown"
            for k in kinds
        )
        kinds += ("unknown",) * max(0, len(result.columns) - len(kinds))
        rows = tuple(tuple(row) for row in result.rows)
        return cls(tuple(result.columns), rows, kinds[: len(result.columns)], result.limit)

    def _column_index(self, column: str | int) -> int:
        if isinstance(column, int) and not isinstance(column, bool):
            if 0 <= column < len(self.columns):
                return column
        elif isinstance(column, str):
            matches = [index for index, name in enumerate(self.columns) if name == column]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise ValueError(f"Column {column!r} is duplicated; use its ordinal index.")
        raise ValueError(f"Unknown column: {column!r}")

    @staticmethod
    def _operator(item: ColumnFilter) -> str:
        if not isinstance(item.operator, str):
            raise ValueError("Filter operator must be text.")
        return item.operator.lower().strip()

    def _normalize_known(self, index: int, value: object, *, predicate: bool) -> object:
        """Return a comparable value authorized by the worker's native kind."""
        kind = self.column_types[index]
        valid = False
        normalized = value
        try:
            if kind == "text":
                valid = isinstance(value, str)
            elif kind == "boolean":
                valid = isinstance(value, bool)
            elif kind == "number":
                # bool is a Number in Python, but is a distinct JSON/native kind.
                valid = isinstance(value, Number) and not isinstance(value, bool)
            elif kind == "date":
                if isinstance(value, datetime):
                    valid = False
                elif isinstance(value, date):
                    valid = True
                elif isinstance(value, str):
                    normalized = date.fromisoformat(value)
                    valid = True
            elif kind == "datetime":
                if isinstance(value, datetime):
                    valid = True
                elif isinstance(value, str):
                    normalized = datetime.fromisoformat(value)
                    valid = True
            elif kind == "time":
                if isinstance(value, time):
                    valid = True
                elif isinstance(value, str):
                    normalized = time.fromisoformat(value)
                    valid = True
        except ValueError:
            valid = False
        if valid:
            return normalized
        column = self.columns[index]
        if predicate:
            raise ValueError(f"{column} needs a {kind} comparison value.")
        raise ValueError(f"{column} has a value incompatible with {kind} metadata.")

    def _validate_filter(self, item: ColumnFilter) -> int:
        index = self._column_index(item.column)
        op = self._operator(item)
        if op in {"is null", "is not null"}:
            return index
        if op not in {"equals", "contains", "<", "<=", ">", ">="}:
            raise ValueError("Filter must use equals, contains, comparison, or a NULL predicate.")
        kind = self.column_types[index]
        if op == "contains":
            if kind not in {"text", "unknown"}:
                raise ValueError(
                    "Contains is available for text or explicitly literal unknown values only."
                )
            if item.value is None:
                raise ValueError(f"{self.columns[index]} needs a literal contains value.")
            if kind == "text":
                self._normalize_known(index, item.value, predicate=True)
        elif op in {"<", "<=", ">", ">="}:
            if kind not in {"number", "date", "datetime", "time"}:
                raise ValueError(
                    "Ordered comparisons need known number, date, datetime, or time metadata."
                )
            self._normalize_known(index, item.value, predicate=True)
        elif kind == "unknown":
            if item.value is None:
                raise ValueError(
                    f"{self.columns[index]} needs a literal equals value; use IS NULL for NULL."
                )
        else:
            self._normalize_known(index, item.value, predicate=True)
        return index

    def add_filter(self, filter: ColumnFilter) -> None:
        self._validate_filter(filter)
        candidate = [*self._filters, filter]
        self._transformed_rows(candidate, self._sort)
        self._filters.append(filter)

    def remove_filter(self, index: int) -> None:
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= len(self._filters)
        ):
            raise ValueError("No such filter.")
        candidate = [item for ordinal, item in enumerate(self._filters) if ordinal != index]
        self._transformed_rows(candidate, self._sort)
        self._filters[:] = candidate

    def clear_filters(self) -> None:
        """Remove all filters if the resulting sorted view remains valid."""
        self._transformed_rows([], self._sort)
        self._filters.clear()

    def set_sort(self, column: str | int, direction: str = "asc") -> None:
        index = self._column_index(column)
        if not isinstance(direction, str) or direction.lower() not in {"asc", "desc"}:
            raise ValueError("Sort direction must be asc or desc.")
        direction = direction.lower()
        if self.column_types[index] not in {
            "text",
            "boolean",
            "number",
            "date",
            "datetime",
            "time",
        }:
            raise ValueError("Sorting needs known column metadata.")
        candidate = (index, direction)
        self._transformed_rows(self._filters, candidate)
        self._sort = candidate

    def hide_column(self, column: str | int) -> None:
        index = self._column_index(column)
        if index not in self._hidden and len(self._hidden) >= len(self.columns) - 1:
            raise ValueError("Keep one column visible; restore a column before hiding another.")
        self._hidden.add(index)

    def restore_all_columns(self) -> None:
        self._hidden.clear()

    def restore_column(self, column: str | int) -> None:
        self._hidden.discard(self._column_index(column))

    def resize_column(self, column: str | int, width: int | None = None) -> None:
        index = self._column_index(column)
        if width is None:
            self._widths.pop(index, None)
            return
        if not isinstance(width, int) or width < 4:
            raise ValueError("Column width must be at least 4 characters.")
        self._widths[index] = width

    def pin_through(self, column: str | int | None) -> None:
        self._pin_end = None if column is None else self._column_index(column)

    def resize_by(self, column: str | int, delta: int) -> None:
        index = self._column_index(column)
        self.resize_column(index, max(4, self._widths.get(index, self._auto_width(index)) + delta))

    def clear_sort(self) -> None:
        self._sort = None

    def reset_transforms(self) -> None:
        self._filters.clear()
        self._sort = None
        self._hidden.clear()
        self._widths.clear()
        self._pin_end = None

    def search(self, scope: str, literal: str) -> tuple[tuple[int, int], ...]:
        """Return source (row, column) matches in the current displayed subset.

        ``scope`` identifies focused table values; full SQL/detail are UI-owned.
        """
        if scope not in {"table", "visible", "focused"}:
            raise ValueError("Search is available in the visible result table only.")
        needle = literal.casefold()
        if not needle:
            return ()
        output: list[tuple[int, int]] = []
        view = self.view()
        for row in view.rows:
            for col in view.columns:
                if needle in self._literal(row.values[col.index]).casefold():
                    output.append((row.index, col.index))
        return tuple(output)

    @staticmethod
    def _literal(value: object) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return str(value).lower()
        return str(value)

    def _matches(self, row: tuple[object, ...], filter: ColumnFilter) -> bool:
        i = self._validate_filter(filter)
        value = row[i] if i < len(row) else None
        op = self._operator(filter)
        wanted = filter.value
        if op == "is null":
            return value is None
        if op == "is not null":
            return value is not None
        if value is None:
            return False
        kind = self.column_types[i]
        if op == "contains":
            if kind == "text":
                value = self._normalize_known(i, value, predicate=False)
                wanted = self._normalize_known(i, wanted, predicate=True)
            return self._literal(wanted).casefold() in self._literal(value).casefold()
        if kind == "unknown":
            # Unknown values have literal semantics only; never infer a SQL/native kind.
            return self._literal(value) == self._literal(wanted)
        value = self._normalize_known(i, value, predicate=False)
        wanted = self._normalize_known(i, wanted, predicate=True)
        try:
            if op == "equals":
                return value == wanted
            if op == "<":
                return value < wanted  # type: ignore[operator]
            if op == "<=":
                return value <= wanted  # type: ignore[operator]
            if op == ">":
                return value > wanted  # type: ignore[operator]
            return value >= wanted  # type: ignore[operator]
        except TypeError:
            raise ValueError(
                f"{self.columns[i]} values cannot be compared despite {kind} metadata."
            ) from None

    def _transformed_rows(
        self,
        filters: list[ColumnFilter],
        sort: tuple[int, str] | None,
    ) -> list[tuple[int, tuple[object, ...]]]:
        indexed = [
            (i, row)
            for i, row in enumerate(self.rows)
            if all(self._matches(row, item) for item in filters)
        ]
        if sort:
            col, direction = sort
            # NULL always follows values. Python's stable sort retains fetched order for ties.
            keyed = [
                (item, self._normalize_known(col, item[1][col], predicate=False))
                for item in indexed
                if col < len(item[1]) and item[1][col] is not None
            ]
            absent = [item for item in indexed if col >= len(item[1]) or item[1][col] is None]
            try:
                keyed.sort(key=lambda pair: pair[1], reverse=direction == "desc")
            except TypeError:
                kind = self.column_types[col]
                raise ValueError(
                    f"{self.columns[col]} values cannot be ordered despite {kind} metadata."
                ) from None
            indexed = [item for item, _ in keyed] + absent
        return indexed

    def view(self) -> PreviewView:
        indexed = self._transformed_rows(self._filters, self._sort)
        visible = tuple(i for i in range(len(self.columns)) if i not in self._hidden)
        cols = tuple(
            PreviewColumn(
                i,
                self.columns[i],
                self.column_types[i],
                self._widths.get(i, self._auto_width(i)),
                self._pin_end is not None and i <= self._pin_end,
            )
            for i in visible
        )
        return PreviewView(
            cols,
            tuple(PreviewRow(i, row) for i, row in indexed),
            len(self.rows),
            len(indexed),
            self.cap,
        )

    def _auto_width(self, index: int) -> int:
        samples = [
            self._literal(row[index] if index < len(row) else None) for row in self.rows[:40]
        ]
        return min(30, max(8, len(self.columns[index]), *(len(v) for v in samples)))
