from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from flowsteward_extension_sdk import (
    ArtifactAccessError,
    find_dataset_descriptor,
    write_dataset_stream,
)
from flowsteward_extension_sdk.http import verified_platform_grant_signature

from .errors import SheetsExtensionError
from .ranges import read_ranges, selected_bounds
from .validation import validated_read_input

_MESSAGE_THE_APPROVED_DATASET_OUTPUT_IS_UNAVAILABLE = "The approved dataset output is unavailable"
_MESSAGE_GOOGLE_SHEETS_RETURNED_INVALID_VALUES = "Google Sheets returned invalid values"

DATASET_BINDING_KEY = "google_sheet_dataset"
DATASET_CONTENT_TYPE = "application/x-ndjson"
MAX_ROWS = 100_000
MAX_CELLS = 1_000_000
MAX_DECODED_BYTES = 200 * 1024 * 1024
PREVIEW_ROWS = 100
PREVIEW_BYTES = 256 * 1024
READ_BATCH_ROWS = 10_000
MAX_VALUES_GET_CALLS = (MAX_ROWS + 1 + READ_BATCH_ROWS - 1) // READ_BATCH_ROWS


def read_google_sheet(
    transport: Any,
    root_payload: dict[str, Any],
    operation_input: Mapping[str, Any],
    *,
    dataset_writer: Callable[..., dict[str, Any]] = write_dataset_stream,
) -> dict[str, Any]:
    normalized = validated_read_input(operation_input)
    grant_limit = dataset_grant_limit(root_payload)
    byte_limit = min(MAX_DECODED_BYTES, grant_limit)
    sheet = selected_sheet(transport.spreadsheet_metadata(normalized["spreadsheet_id"]), normalized)
    bounds = selected_bounds(
        normalized.get("range"),
        grid_rows=sheet["row_count"],
        grid_columns=sheet["column_count"],
    )
    _validate_requested_bounds(bounds, header_mode=normalized["header_mode"])
    provider_ranges = read_ranges(
        sheet["title"],
        relative_range=normalized.get("range"),
        grid_rows=sheet["row_count"],
        grid_columns=sheet["column_count"],
        batch_rows=READ_BATCH_ROWS,
    )
    state: dict[str, Any] = {
        "headers": [],
        "header_consumed": normalized["header_mode"] == "none",
        "row_count": 0,
        "column_count": 0,
        "cell_count": 0,
        "bytes": 0,
        "preview": [],
        "preview_bytes": 0,
        "max_columns": bounds.end_column - bounds.start_column + 1,
    }
    chunks = _dataset_chunks(
        transport,
        normalized["spreadsheet_id"],
        provider_ranges,
        state=state,
        byte_limit=byte_limit,
    )
    try:
        written = dataset_writer(
            root_payload,
            chunks,
            binding_key=DATASET_BINDING_KEY,
            content_type=DATASET_CONTENT_TYPE,
        )
    except SheetsExtensionError:
        raise
    except Exception as exc:
        raise SheetsExtensionError(
            "dataset_output_unavailable", _MESSAGE_THE_APPROVED_DATASET_OUTPUT_IS_UNAVAILABLE
        ) from exc
    if not isinstance(written, Mapping):
        raise SheetsExtensionError(
            "dataset_output_unavailable", _MESSAGE_THE_APPROVED_DATASET_OUTPUT_IS_UNAVAILABLE
        )
    handle = written.get("dataset_handle")
    size = written.get("size_bytes")
    if (
        not isinstance(handle, str)
        or not handle.strip()
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size != state["bytes"]
    ):
        raise SheetsExtensionError(
            "dataset_output_unavailable", _MESSAGE_THE_APPROVED_DATASET_OUTPUT_IS_UNAVAILABLE
        )
    schema = [{"name": name, "type": "string", "nullable": True} for name in state["headers"]]
    state["preview"] = [
        {name: row.get(name) for name in state["headers"]} for row in state["preview"]
    ]
    return {
        "dataset_handle": handle.strip(),
        "row_count": state["row_count"],
        "column_count": state["column_count"],
        "schema": schema,
        "preview": state["preview"],
        "spreadsheet_id": normalized["spreadsheet_id"],
        "sheet_id": sheet["sheet_id"],
    }


def _validate_requested_bounds(bounds: Any, *, header_mode: str) -> None:
    requested_rows = bounds.end_row - bounds.start_row + 1
    requested_columns = bounds.end_column - bounds.start_column + 1
    header_rows = 1 if header_mode == "first_row" else 0
    maximum_rows = MAX_ROWS + header_rows
    maximum_cells = MAX_CELLS + header_rows * requested_columns
    request_count = (requested_rows + READ_BATCH_ROWS - 1) // READ_BATCH_ROWS
    if (
        requested_rows > maximum_rows
        or requested_rows * requested_columns > maximum_cells
        or request_count > MAX_VALUES_GET_CALLS
    ):
        raise SheetsExtensionError(
            "sheet_too_large", "The requested Google Sheet span is too large"
        )


def dataset_grant_limit(payload: dict[str, Any]) -> int:
    try:
        descriptor = find_dataset_descriptor(
            payload, binding_key=DATASET_BINDING_KEY, role="output"
        )
        access = descriptor.get("access")
        max_size = access.get("max_size_bytes") if isinstance(access, Mapping) else None
        if (
            descriptor.get("role") != "output"
            or descriptor.get("binding_key") != DATASET_BINDING_KEY
            or descriptor.get("content_type") != DATASET_CONTENT_TYPE
            or not isinstance(access, Mapping)
            or access.get("transport") != "presigned_url"
            or access.get("mode") not in {"write", "read_write"}
            or isinstance(max_size, bool)
            or not isinstance(max_size, int)
            or max_size <= 0
            or not verified_platform_grant_signature(descriptor, dict(access))
            or not _scope_matches(payload, descriptor)
        ):
            raise ArtifactAccessError("invalid dataset grant")
        return max_size
    except (ArtifactAccessError, TypeError, ValueError) as exc:
        raise SheetsExtensionError(
            "dataset_output_unavailable", _MESSAGE_THE_APPROVED_DATASET_OUTPUT_IS_UNAVAILABLE
        ) from exc


def selected_sheet(metadata: Any, normalized: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata, Mapping) or set(metadata) - {"sheets"}:
        raise SheetsExtensionError(
            "provider_unavailable", "Google Sheets returned invalid spreadsheet metadata"
        )
    rows = metadata.get("sheets")
    if not isinstance(rows, list):
        raise SheetsExtensionError(
            "provider_unavailable", "Google Sheets returned invalid spreadsheet metadata"
        )
    sheets = [_sheet_properties(row) for row in rows]
    if "sheet_id" in normalized:
        matches = [row for row in sheets if row["sheet_id"] == normalized["sheet_id"]]
    else:
        matches = [row for row in sheets if row["title"] == normalized["sheet_title"]]
    if not matches:
        raise SheetsExtensionError("google_sheet_not_found", "The selected sheet was not found")
    if len(matches) != 1:
        raise SheetsExtensionError(
            "google_sheet_ambiguous", "Select this sheet by its numeric sheet ID"
        )
    return matches[0]


def _sheet_properties(value: Any) -> dict[str, Any]:
    properties = value.get("properties") if isinstance(value, Mapping) else None
    if not isinstance(properties, Mapping) or set(properties) - {
        "sheetId",
        "title",
        "gridProperties",
    }:
        raise SheetsExtensionError(
            "provider_unavailable", "Google Sheets returned invalid sheet metadata"
        )
    grid = properties.get("gridProperties")
    sheet_id = properties.get("sheetId")
    title = properties.get("title")
    rows = grid.get("rowCount") if isinstance(grid, Mapping) else None
    columns = grid.get("columnCount") if isinstance(grid, Mapping) else None
    if (
        isinstance(sheet_id, bool)
        or not isinstance(sheet_id, int)
        or sheet_id < 0
        or not isinstance(title, str)
        or not 1 <= len(title) <= 100
        or any(ord(char) < 32 or ord(char) == 127 for char in title)
        or not isinstance(grid, Mapping)
        or set(grid) - {"rowCount", "columnCount"}
        or isinstance(rows, bool)
        or not isinstance(rows, int)
        or rows <= 0
        or isinstance(columns, bool)
        or not isinstance(columns, int)
        or columns <= 0
    ):
        raise SheetsExtensionError(
            "provider_unavailable", "Google Sheets returned invalid sheet metadata"
        )
    return {"sheet_id": sheet_id, "title": title, "row_count": rows, "column_count": columns}


def _dataset_chunks(
    transport: Any,
    spreadsheet_id: str,
    provider_ranges: Iterable[str],
    *,
    state: dict[str, Any],
    byte_limit: int,
):
    for provider_range in provider_ranges:
        page = _values_page(transport.values_get(spreadsheet_id, provider_range))
        for values in page:
            if len(values) > state["max_columns"]:
                raise SheetsExtensionError(
                    "provider_unavailable",
                    "Google Sheets returned values outside the requested range",
                )
            if not state["header_consumed"]:
                state["headers"] = _headers(values)
                state["column_count"] = len(values)
                state["header_consumed"] = True
                continue
            _extend_headers(state["headers"], len(values))
            state["column_count"] = max(state["column_count"], len(values))
            state["row_count"] += 1
            state["cell_count"] += len(values)
            if state["row_count"] > MAX_ROWS or state["cell_count"] > MAX_CELLS:
                raise SheetsExtensionError("sheet_too_large", "The Google Sheet is too large")
            row = {
                name: values[index] if index < len(values) else None
                for index, name in enumerate(state["headers"])
            }
            encoded = (
                json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            )
            state["bytes"] += len(encoded)
            if state["bytes"] > byte_limit:
                raise SheetsExtensionError("sheet_too_large", "The Google Sheet is too large")
            if (
                len(state["preview"]) < PREVIEW_ROWS
                and state["preview_bytes"] + len(encoded) <= PREVIEW_BYTES
            ):
                state["preview"].append(dict(row))
                state["preview_bytes"] += len(encoded)
            yield encoded


def _values_page(value: Any) -> list[list[str | None]]:
    if not isinstance(value, Mapping) or set(value) - {"range", "majorDimension", "values"}:
        raise SheetsExtensionError(
            "provider_unavailable", _MESSAGE_GOOGLE_SHEETS_RETURNED_INVALID_VALUES
        )
    major = value.get("majorDimension")
    provider_range = value.get("range")
    if provider_range is not None and (
        not isinstance(provider_range, str)
        or not provider_range
        or len(provider_range) > 1024
        or any(ord(char) < 32 or ord(char) == 127 for char in provider_range)
    ):
        raise SheetsExtensionError(
            "provider_unavailable", _MESSAGE_GOOGLE_SHEETS_RETURNED_INVALID_VALUES
        )
    if major is not None and major != "ROWS":
        raise SheetsExtensionError(
            "provider_unavailable", _MESSAGE_GOOGLE_SHEETS_RETURNED_INVALID_VALUES
        )
    rows = value.get("values", [])
    if not isinstance(rows, list):
        raise SheetsExtensionError(
            "provider_unavailable", _MESSAGE_GOOGLE_SHEETS_RETURNED_INVALID_VALUES
        )
    result: list[list[str | None]] = []
    for row in rows:
        if not isinstance(row, list) or any(
            cell is not None and (not isinstance(cell, str) or len(cell) > 50_000) for cell in row
        ):
            raise SheetsExtensionError(
                "provider_unavailable", _MESSAGE_GOOGLE_SHEETS_RETURNED_INVALID_VALUES
            )
        result.append([cell if cell not in {""} else None for cell in row])
    return result


def _headers(values: list[str | None]) -> list[str]:
    result: list[str] = []
    used: set[str] = set()
    for index, value in enumerate(values, start=1):
        base = value.strip() if isinstance(value, str) else ""
        if len(base) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in base):
            raise SheetsExtensionError(
                "provider_unavailable", "Google Sheets returned an invalid header"
            )
        if not base:
            base = f"column_{index}"
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        result.append(candidate)
        used.add(candidate)
    return result


def _extend_headers(headers: list[str], width: int) -> None:
    used = set(headers)
    while len(headers) < width:
        index = len(headers) + 1
        candidate = f"column_{index}"
        suffix = 2
        while candidate in used:
            candidate = f"column_{index}_{suffix}"
            suffix += 1
        headers.append(candidate)
        used.add(candidate)


def _scope_matches(payload: Mapping[str, Any], descriptor: Mapping[str, Any]) -> bool:
    host = payload.get("host")
    host_scope = host.get("scope") if isinstance(host, Mapping) else None
    grant_scope = descriptor.get("scope")
    if not isinstance(host_scope, Mapping) or not isinstance(grant_scope, Mapping):
        return False
    return all(
        str(host_scope.get(field) or "").strip()
        and str(host_scope.get(field) or "").strip() == str(grant_scope.get(field) or "").strip()
        for field in ("account_id", "project_id")
    )


__all__ = ["read_google_sheet", "selected_sheet"]
