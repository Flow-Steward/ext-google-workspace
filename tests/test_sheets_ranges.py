from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("A1", "A1"),
        ("a1:b20", "A1:B20"),
        ("A:C", "A:C"),
        ("2:10", "2:10"),
        ("$A$1:$C$9", "$A$1:$C$9"),
    ],
)
def test_relative_a1_ranges_are_normalized(value, expected) -> None:
    from sheets.ranges import normalize_relative_a1

    assert normalize_relative_a1(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "Sheet1!A1",
        "'Other sheet'!A1:B2",
        "R1C1",
        "R[-1]C",
        "A0",
        "B2:A1",
        "A1\n:B2",
        "x" * 513,
        "",
    ],
)
def test_relative_a1_rejects_cross_sheet_r1c1_controls_and_invalid_bounds(value) -> None:
    from sheets.errors import SheetsExtensionError
    from sheets.ranges import normalize_relative_a1

    with pytest.raises(SheetsExtensionError) as exc_info:
        normalize_relative_a1(value)
    assert exc_info.value.code == "invalid_payload"


def test_provider_ranges_quote_sheet_title_and_batch_rows() -> None:
    from sheets.ranges import read_ranges

    assert list(
        read_ranges(
            "Supplier's stock",
            relative_range="B2:D205",
            grid_rows=1000,
            grid_columns=26,
            batch_rows=100,
        )
    ) == [
        "'Supplier''s stock'!B2:D101",
        "'Supplier''s stock'!B102:D201",
        "'Supplier''s stock'!B202:D205",
    ]


def test_omitted_range_reads_bounded_grid_in_batches() -> None:
    from sheets.ranges import read_ranges

    assert list(
        read_ranges("Data", relative_range=None, grid_rows=205, grid_columns=3, batch_rows=100)
    ) == ["'Data'!A1:C100", "'Data'!A101:C200", "'Data'!A201:C205"]
