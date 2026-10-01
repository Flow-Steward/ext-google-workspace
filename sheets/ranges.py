from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import SheetsExtensionError

_MESSAGE_RANGE_MUST_BE_A_RELATIVE_A1_RANGE = "range must be a relative A1 range"

_CELL_RE = re.compile(r"(\$?)([A-Za-z]{1,5})(\$?)([1-9](?a:\d)*)")
_COLUMN_RE = re.compile(r"(\$?)([A-Za-z]{1,5})")
_ROW_RE = re.compile(r"(\$?)([1-9](?a:\d)*)")


@dataclass(frozen=True)
class A1Bounds:
    start_column: int
    end_column: int
    start_row: int
    end_row: int


def normalize_relative_a1(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if (
        not text
        or len(text) > 512
        or "!" in text
        or any(ord(char) < 32 or ord(char) == 127 for char in text)
    ):
        raise SheetsExtensionError("invalid_payload", _MESSAGE_RANGE_MUST_BE_A_RELATIVE_A1_RANGE)
    pieces = text.split(":")
    if len(pieces) > 2:
        raise SheetsExtensionError("invalid_payload", _MESSAGE_RANGE_MUST_BE_A_RELATIVE_A1_RANGE)
    first = _reference(pieces[0])
    second = _reference(pieces[1]) if len(pieces) == 2 else first
    if first[0] != second[0]:
        raise SheetsExtensionError("invalid_payload", "range must use matching A1 bounds")
    if first[1] > second[1] or first[2] > second[2]:
        raise SheetsExtensionError("invalid_payload", "range bounds are reversed")
    return ":".join(_normalize_reference(piece) for piece in pieces)


def read_ranges(
    sheet_title: str,
    *,
    relative_range: str | None,
    grid_rows: int,
    grid_columns: int,
    batch_rows: int,
):
    if (
        isinstance(grid_rows, bool)
        or not isinstance(grid_rows, int)
        or grid_rows <= 0
        or isinstance(grid_columns, bool)
        or not isinstance(grid_columns, int)
        or grid_columns <= 0
        or isinstance(batch_rows, bool)
        or not isinstance(batch_rows, int)
        or batch_rows <= 0
    ):
        raise SheetsExtensionError("provider_unavailable", "Google Sheets grid is invalid")
    bounds = selected_bounds(
        relative_range,
        grid_rows=grid_rows,
        grid_columns=grid_columns,
    )
    title = "'" + sheet_title.replace("'", "''") + "'!"
    for start in range(bounds.start_row, bounds.end_row + 1, batch_rows):
        end = min(bounds.end_row, start + batch_rows - 1)
        yield (
            f"{title}{column_name(bounds.start_column)}{start}:"
            f"{column_name(bounds.end_column)}{end}"
        )


def selected_bounds(relative_range: str | None, *, grid_rows: int, grid_columns: int) -> A1Bounds:
    return (
        _bounds(normalize_relative_a1(relative_range), grid_rows, grid_columns)
        if relative_range is not None
        else A1Bounds(1, grid_columns, 1, grid_rows)
    )


def _reference(value: str) -> tuple[str, int, int]:
    match = _CELL_RE.fullmatch(value)
    if match:
        return "cell", column_number(match.group(2)), int(match.group(4))
    match = _COLUMN_RE.fullmatch(value)
    if match:
        number = column_number(match.group(2))
        return "column", number, number
    match = _ROW_RE.fullmatch(value)
    if match:
        number = int(match.group(2))
        return "row", number, number
    raise SheetsExtensionError("invalid_payload", _MESSAGE_RANGE_MUST_BE_A_RELATIVE_A1_RANGE)


def _normalize_reference(value: str) -> str:
    match = _CELL_RE.fullmatch(value)
    if match:
        return f"{match.group(1)}{match.group(2).upper()}{match.group(3)}{match.group(4)}"
    match = _COLUMN_RE.fullmatch(value)
    if match:
        return f"{match.group(1)}{match.group(2).upper()}"
    match = _ROW_RE.fullmatch(value)
    if match:
        return f"{match.group(1)}{match.group(2)}"
    raise SheetsExtensionError("invalid_payload", _MESSAGE_RANGE_MUST_BE_A_RELATIVE_A1_RANGE)


def _bounds(value: str, grid_rows: int, grid_columns: int) -> A1Bounds:
    pieces = value.split(":")
    first = _reference(pieces[0])
    second = _reference(pieces[-1])
    if first[0] == "cell":
        bounds = A1Bounds(first[1], second[1], first[2], second[2])
    elif first[0] == "column":
        bounds = A1Bounds(first[1], second[1], 1, grid_rows)
    else:
        bounds = A1Bounds(1, grid_columns, first[1], second[1])
    if bounds.end_row > grid_rows or bounds.end_column > grid_columns:
        raise SheetsExtensionError("invalid_payload", "range exceeds the selected sheet grid")
    return bounds


def column_number(value: str) -> int:
    result = 0
    for char in value.upper():
        result = result * 26 + ord(char) - 64
    if result <= 0 or result > 18278:
        raise SheetsExtensionError("invalid_payload", "A1 column is invalid")
    return result


def column_name(value: int) -> str:
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def cell_coordinates(value: object) -> tuple[int, int]:
    normalized = normalize_relative_a1(value)
    if ":" in normalized:
        raise SheetsExtensionError("invalid_payload", "start_cell must be one A1 cell")
    reference = _reference(normalized)
    if reference[0] != "cell":
        raise SheetsExtensionError("invalid_payload", "start_cell must be one A1 cell")
    return reference[1], reference[2]
