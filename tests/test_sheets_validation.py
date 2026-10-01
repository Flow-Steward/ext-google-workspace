from __future__ import annotations

import pytest


def _input(**overrides):
    value = {
        "connection_ref": "google-a",
        "spreadsheet_id": "spreadsheet-1",
        "sheet_id": 123,
        "header_mode": "first_row",
    }
    value.update(overrides)
    return value


def test_read_input_requires_exactly_one_sheet_selector_and_closed_fields() -> None:
    from sheets.errors import SheetsExtensionError
    from sheets.validation import validated_read_input

    assert validated_read_input(_input())["sheet_id"] == 123
    assert (
        validated_read_input(_input(sheet_id=None, sheet_title="Invoices"))["sheet_title"]
        == "Invoices"
    )

    for value in (
        _input(sheet_id=None),
        _input(sheet_title="Invoices"),
        _input(extra="forbidden"),
        _input(sheet_id=True),
        _input(sheet_id=-1),
        _input(sheet_id=None, sheet_title="x" * 101),
    ):
        with pytest.raises(SheetsExtensionError) as exc_info:
            validated_read_input(value)
        assert exc_info.value.code == "invalid_payload"


@pytest.mark.parametrize("field", ["connection_ref", "spreadsheet_id"])
def test_read_input_rejects_urls_controls_and_oversized_ids(field) -> None:
    from sheets.errors import SheetsExtensionError
    from sheets.validation import validated_read_input

    for invalid in ("https://docs.google.com/x", "bad\nvalue", "x" * 513):
        _raises_input_46_1 = _input(**{field: invalid})
        with pytest.raises(SheetsExtensionError) as exc_info:
            validated_read_input(_raises_input_46_1)
        assert exc_info.value.code == "invalid_payload"


@pytest.mark.parametrize("header_mode", ["FIRST_ROW", "none"])
def test_header_mode_is_closed_and_normalized(header_mode) -> None:
    from sheets.validation import validated_read_input

    assert (
        validated_read_input(_input(header_mode=header_mode))["header_mode"] == header_mode.lower()
    )
