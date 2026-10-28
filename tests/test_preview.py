from datetime import date
from pathlib import Path

import pytest

from dbt_enhance.models import QueryResult
from dbt_enhance.preview import ColumnFilter, PreviewModel


def _model() -> PreviewModel:
    return PreviewModel.from_result(
        QueryResult(
            ["id", "name", "enabled"],
            [[2, "Beta", False], [1, "alpha", True], [None, "alphabet", True]],
            "select 1",
            0.1,
            Path("."),
            50,
            column_types=["number", "text", "boolean"],
        )
    )


def test_transforms_keep_source_identities_and_search_visible_values() -> None:
    preview = _model()
    preview.add_filter(ColumnFilter("name", "contains", "alp"))
    preview.set_sort("id", "desc")
    preview.hide_column("enabled")

    view = preview.view()
    assert [row.index for row in view.rows] == [1, 2]
    assert [column.name for column in view.columns] == ["id", "name"]
    assert preview.search("table", "ALP") == ((1, 1), (2, 1))


def test_column_controls_preserve_ordinals_and_keep_a_recoverable_view() -> None:
    preview = PreviewModel.from_result(
        QueryResult(
            ["dup", "dup", "name"],
            [[1, 2, "alpha"]],
            "",
            0,
            Path("."),
            10,
            column_types=["number", "number", "text"],
        )
    )
    preview.hide_column(0)
    preview.resize_by(1, 3)
    preview.pin_through(1)
    assert [column.index for column in preview.view().columns] == [1, 2]
    assert preview.view().columns[0].pinned
    with pytest.raises(ValueError, match="Keep one column visible"):
        preview.hide_column(1)
        preview.hide_column(2)
    preview.restore_all_columns()
    preview.clear_sort()
    assert len(preview.view().columns) == 3


def test_ordered_filters_require_returned_metadata_and_typed_value() -> None:
    preview = _model()
    with pytest.raises(ValueError, match="needs a number"):
        preview.add_filter(ColumnFilter("id", ">", "2"))

    unknown = PreviewModel.from_result(QueryResult(["value"], [["2"]], "", 0, Path("."), 1))
    with pytest.raises(ValueError, match="known column metadata"):
        unknown.set_sort("value")


def test_serialized_temporal_values_use_native_comparison_semantics() -> None:
    preview = PreviewModel.from_result(
        QueryResult(
            ["day", "instant"],
            [
                ["2025-01-02", "2025-01-01T10:00:00+02:00"],
                ["2025-01-01", "2025-01-01T08:30:00+00:00"],
            ],
            "",
            0,
            Path("."),
            10,
            column_types=["date", "datetime"],
        )
    )

    preview.add_filter(ColumnFilter("day", ">", date(2025, 1, 1)))
    assert [row.index for row in preview.view().rows] == [0]

    preview.clear_filters()
    preview.set_sort("instant")
    assert [row.index for row in preview.view().rows] == [0, 1]
    assert preview.view().rows[0].values[1] == "2025-01-01T10:00:00+02:00"


def test_native_temporal_values_are_supported_without_changing_rows() -> None:
    native = date(2025, 1, 2)
    preview = PreviewModel.from_result(
        QueryResult(
            ["day"],
            [[native], [date(2025, 1, 1)]],
            "",
            0,
            Path("."),
            10,
            column_types=["date"],
        )
    )

    preview.add_filter(ColumnFilter("day", "equals", native))
    assert preview.view().rows[0].values[0] is native


def test_unknown_values_stay_literal_and_known_scalar_kinds_are_distinct() -> None:
    unknown = PreviewModel.from_result(QueryResult(["value"], [["2"], [2]], "", 0, Path("."), 10))
    unknown.add_filter(ColumnFilter("value", "equals", "2"))
    assert [row.index for row in unknown.view().rows] == [0, 1]
    with pytest.raises(ValueError, match="known number, date, datetime, or time"):
        unknown.add_filter(ColumnFilter("value", ">", 1))

    number = _model()
    with pytest.raises(ValueError, match="needs a number"):
        number.add_filter(ColumnFilter("id", "equals", True))
    with pytest.raises(ValueError, match="needs a boolean"):
        number.add_filter(ColumnFilter("enabled", "equals", 1))


def test_failed_sort_and_filter_changes_leave_the_previous_view_usable() -> None:
    preview = PreviewModel.from_result(
        QueryResult(
            ["value"],
            [[1], ["not-a-native-number"]],
            "",
            0,
            Path("."),
            10,
            column_types=["number"],
        )
    )
    original = preview.view()

    with pytest.raises(ValueError, match="value.*number"):
        preview.set_sort("value")
    assert preview.view() == original

    with pytest.raises(ValueError, match="value.*number"):
        preview.add_filter(ColumnFilter("value", ">", 0))
    assert preview.view() == original


def test_filter_removal_is_transactional_when_it_would_expose_bad_sort_data() -> None:
    preview = PreviewModel.from_result(
        QueryResult(
            ["kind", "value"],
            [["good", 1], ["bad", "not-a-native-number"]],
            "",
            0,
            Path("."),
            10,
            column_types=["text", "number"],
        )
    )
    preview.add_filter(ColumnFilter("kind", "equals", "good"))
    preview.set_sort("value")
    expected = preview.view()

    with pytest.raises(ValueError, match="value.*number"):
        preview.remove_filter(0)
    assert preview.view() == expected
    with pytest.raises(ValueError, match="value.*number"):
        preview.clear_filters()
    assert preview.view() == expected


def test_invalid_ordinals_and_malformed_temporal_values_are_rejected() -> None:
    preview = _model()
    preview.add_filter(ColumnFilter("name", "contains", "a"))
    with pytest.raises(ValueError, match="No such filter"):
        preview.remove_filter(-1)
    with pytest.raises(ValueError, match="Unknown column"):
        preview.hide_column(True)
    with pytest.raises(ValueError, match="Unknown column"):
        preview.add_filter(ColumnFilter(True, "equals", 1))

    malformed = PreviewModel.from_result(
        QueryResult(
            ["day"],
            [["not-an-iso-date"]],
            "",
            0,
            Path("."),
            10,
            column_types=["date"],
        )
    )
    with pytest.raises(ValueError, match="day.*date"):
        malformed.add_filter(ColumnFilter("day", ">", date(2025, 1, 1)))
    assert len(malformed.view().rows) == 1
