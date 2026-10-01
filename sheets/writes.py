from __future__ import annotations

import json
import math
import tempfile
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, BinaryIO

from drive.config import artifact_max_bytes
from flowsteward_extension_sdk import (
    ArtifactAccessError,
    find_dataset_descriptor,
    stream_dataset_bytes,
)

from .errors import SheetsExtensionError
from .ranges import cell_coordinates, column_name, normalize_relative_a1, selected_bounds
from .reads import selected_sheet
from .validation import bounded_text, enum, identifier

_MESSAGE_THE_INPUT_DATASET_ROW_COUNT_IS_INVALID = "The input dataset row count is invalid"
_MESSAGE_INVALID_DATASET_SCHEMA = "invalid dataset schema"

MAX_INLINE_ROWS = 100
MAX_INLINE_COLUMNS = 100
MAX_INLINE_CELLS = 10_000
MAX_CELL_TEXT = 50_000
MAX_REQUEST_BYTES = 2 * 1024 * 1024
SPREADSHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
DATASET_BINDING_KEY = "google_sheet_input_dataset"
DATASET_CONTENT_TYPE = "application/x-ndjson"
MAX_DATASET_ROWS = 100_000
MAX_DATASET_CELLS = 1_000_000


@dataclass
class RowSource:
    staged_rows: BinaryIO
    declared_rows: int
    declared_columns: int

    @property
    def rows(self) -> Iterator[list[Any]]:
        self.staged_rows.seek(0)
        for raw_line in self.staged_rows:
            value = json.loads(raw_line)
            if not isinstance(value, list):
                raise SheetsExtensionError(
                    "dataset_input_unavailable", "The staged input dataset is invalid"
                )
            yield value

    def validate_batches(self, *, max_bytes: int = MAX_REQUEST_BYTES) -> None:
        deque(
            _row_batches(
                self.rows,
                width=self.declared_columns,
                expected_rows=self.declared_rows,
                max_bytes=max_bytes,
            ),
            maxlen=0,
        )

    def close(self) -> None:
        self.staged_rows.close()

    def __enter__(self) -> RowSource:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


WRITE_COMMON_FIELDS = {
    "connection_ref",
    "mode",
    "dataset_handle",
    "inline_rows",
    "start_cell",
}
CREATE_FIELDS = WRITE_COMMON_FIELDS | {
    "spreadsheet_title",
    "destination_folder_id",
    "sheet_title",
}
OVERWRITE_FIELDS = WRITE_COMMON_FIELDS | {
    "spreadsheet_id",
    "sheet_id",
    "sheet_title",
}
APPEND_FIELDS = {
    "connection_ref",
    "spreadsheet_id",
    "sheet_id",
    "sheet_title",
    "table_range",
    "dataset_handle",
    "inline_rows",
}


def validated_write_input(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SheetsExtensionError("invalid_payload", "Google Sheets write input is invalid")
    mode = enum(value.get("mode"), {"create", "overwrite"})
    allowed = CREATE_FIELDS if mode == "create" else OVERWRITE_FIELDS
    if set(value) - allowed:
        raise SheetsExtensionError("invalid_payload", "Google Sheets write input is invalid")
    result = _data_plane(value)
    result.update(
        {
            "connection_ref": identifier(value.get("connection_ref"), "connection_ref"),
            "mode": mode,
            "start_cell": _start_cell(value.get("start_cell", "A1")),
        }
    )
    if mode == "create":
        result.update(
            {
                "spreadsheet_title": bounded_text(
                    value.get("spreadsheet_title"), field="spreadsheet_title", maximum=200
                ),
                "destination_folder_id": identifier(
                    value.get("destination_folder_id", "root"), "destination_folder_id"
                ),
                "sheet_title": bounded_text(
                    value.get("sheet_title", "Sheet1"), field="sheet_title", maximum=100
                ),
            }
        )
    else:
        result["spreadsheet_id"] = identifier(value.get("spreadsheet_id"), "spreadsheet_id")
        result.update(_sheet_selector(value))
    return result


def validated_append_input(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - APPEND_FIELDS:
        raise SheetsExtensionError("invalid_payload", "Google Sheets append input is invalid")
    result = _data_plane(value)
    result.update(
        {
            "connection_ref": identifier(value.get("connection_ref"), "connection_ref"),
            "spreadsheet_id": identifier(value.get("spreadsheet_id"), "spreadsheet_id"),
            "table_range": normalize_relative_a1(value.get("table_range")),
            **_sheet_selector(value),
        }
    )
    return result


def write_google_sheet(
    sheets_transport: Any,
    drive_transport: Any,
    root_payload: dict[str, Any],
    operation_input: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = validated_write_input(operation_input)
    with row_source(root_payload, normalized) as source:
        return _write_google_sheet_from_source(
            sheets_transport,
            drive_transport,
            root_payload,
            normalized,
            source,
        )


def _write_google_sheet_from_source(
    sheets_transport: Any,
    drive_transport: Any,
    root_payload: dict[str, Any],
    normalized: Mapping[str, Any],
    source: RowSource,
) -> dict[str, Any]:
    operation_id = _operation_id(root_payload)
    web_view_link = None
    if normalized["mode"] == "create":
        folder_id = normalized["destination_folder_id"]
        file_row = drive_transport.find_by_operation_marker(operation_id, folder_id)
        if file_row is None:
            file_row = drive_transport.create_google_spreadsheet(
                {
                    "name": normalized["spreadsheet_title"],
                    "parents": [folder_id],
                    "mimeType": SPREADSHEET_MIME_TYPE,
                    "appProperties": {"flow_steward_operation_id": operation_id},
                }
            )
        spreadsheet_id, web_view_link = _created_spreadsheet(file_row)
        metadata = sheets_transport.spreadsheet_metadata(spreadsheet_id)
        sheet = _create_sheet(sheets_transport, spreadsheet_id, metadata, normalized["sheet_title"])
    else:
        spreadsheet_id = normalized["spreadsheet_id"]
        metadata = sheets_transport.spreadsheet_metadata(spreadsheet_id)
        sheet = selected_sheet(metadata, normalized)
        _clear_overwrite_rectangle(
            sheets_transport,
            spreadsheet_id,
            sheet,
            normalized,
            incoming_rows=source.declared_rows,
            incoming_columns=source.declared_columns,
        )
    row_count = _write_value_batches(
        sheets_transport,
        spreadsheet_id,
        sheet["title"],
        normalized["start_cell"],
        source.rows,
        width=source.declared_columns,
        expected_rows=source.declared_rows,
    )
    return {
        "spreadsheet_id": spreadsheet_id,
        "sheet_id": sheet["sheet_id"],
        "web_view_link": web_view_link,
        "row_count": row_count,
        "column_count": source.declared_columns,
    }


def append_google_sheet_rows(
    sheets_transport: Any,
    root_payload: dict[str, Any],
    operation_input: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = validated_append_input(operation_input)
    with row_source(root_payload, normalized) as source:
        source.validate_batches(max_bytes=256 * 1024)
        return _append_google_sheet_rows_from_source(
            sheets_transport,
            root_payload,
            normalized,
            source,
        )


def _append_google_sheet_rows_from_source(
    sheets_transport: Any,
    root_payload: dict[str, Any],
    normalized: Mapping[str, Any],
    source: RowSource,
) -> dict[str, Any]:
    operation_id = _operation_id(root_payload)
    spreadsheet_id = normalized["spreadsheet_id"]
    metadata = sheets_transport.spreadsheet_metadata(spreadsheet_id)
    sheet = selected_sheet(metadata, normalized)
    bounds = selected_bounds(
        normalized["table_range"],
        grid_rows=sheet["row_count"],
        grid_columns=sheet["column_count"],
    )
    provider_table_range = _quoted_range(sheet["title"], normalized["table_range"])
    existing_values = _strict_values(
        sheets_transport.values_get(spreadsheet_id, provider_table_range)
    )
    start_row = bounds.start_row + len(existing_values)
    width = source.declared_columns
    if width > bounds.end_column - bounds.start_column + 1:
        raise SheetsExtensionError(
            "invalid_payload", "Append rows exceed the selected table columns"
        )
    first_appended_row: int | None = None
    last_appended_row: int | None = None
    next_row = start_row
    current_grid_rows = sheet["row_count"]
    actual_rows = 0
    for chunk_index, rows in enumerate(
        _row_batches(
            source.rows,
            width=width,
            expected_rows=source.declared_rows,
            max_bytes=256 * 1024,
        )
    ):
        chunk_id = f"{operation_id}:chunk:{chunk_index}"
        marker = sheets_transport.find_developer_marker(spreadsheet_id, chunk_id)
        if marker is not None:
            marker_rows = _marker_rows(marker, sheet_id=sheet["sheet_id"])
            if marker_rows is not None:
                marker_start, marker_end = marker_rows
                next_row = max(next_row, marker_end + 1)
                current_grid_rows = max(current_grid_rows, marker_end)
                first_appended_row = (
                    marker_start
                    if first_appended_row is None
                    else min(first_appended_row, marker_start)
                )
                last_appended_row = (
                    marker_end if last_appended_row is None else max(last_appended_row, marker_end)
                )
            actual_rows += len(rows)
            continue
        chunk_start = next_row
        chunk_end = chunk_start + len(rows) - 1
        requests = _append_requests(
            sheet_id=sheet["sheet_id"],
            start_column=bounds.start_column,
            start_row=chunk_start,
            end_row=chunk_end,
            grid_rows=current_grid_rows,
            rows=rows,
            chunk_id=chunk_id,
        )
        sheets_transport.batch_update(spreadsheet_id, requests)
        next_row = chunk_end + 1
        current_grid_rows = max(current_grid_rows, chunk_end)
        first_appended_row = (
            chunk_start if first_appended_row is None else min(first_appended_row, chunk_start)
        )
        last_appended_row = (
            chunk_end if last_appended_row is None else max(last_appended_row, chunk_end)
        )
        actual_rows += len(rows)
    if actual_rows != source.declared_rows:
        raise SheetsExtensionError(
            "dataset_input_unavailable", _MESSAGE_THE_INPUT_DATASET_ROW_COUNT_IS_INVALID
        )
    if first_appended_row is None or last_appended_row is None:
        first_appended_row = start_row
        last_appended_row = start_row + source.declared_rows - 1
    appended_range = _absolute_range(
        sheet["title"],
        bounds.start_column,
        first_appended_row,
        bounds.start_column + width - 1,
        last_appended_row,
    )
    return {
        "spreadsheet_id": spreadsheet_id,
        "sheet_id": sheet["sheet_id"],
        "appended_range": appended_range,
        "row_count": source.declared_rows,
        "column_count": width,
    }


def _append_requests(
    *,
    sheet_id: int,
    start_column: int,
    start_row: int,
    end_row: int,
    grid_rows: int,
    rows: list[list[Any]],
    chunk_id: str,
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    if end_row > grid_rows:
        requests.append(
            {
                "appendDimension": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "length": end_row - grid_rows,
                }
            }
        )
    requests.extend(
        [
            {
                "updateCells": {
                    "start": {
                        "sheetId": sheet_id,
                        "rowIndex": start_row - 1,
                        "columnIndex": start_column - 1,
                    },
                    "rows": [_cell_row(row) for row in rows],
                    "fields": "userEnteredValue",
                }
            },
            {
                "createDeveloperMetadata": {
                    "developerMetadata": {
                        "metadataKey": "flow_steward_operation_id",
                        "metadataValue": chunk_id,
                        "visibility": "DOCUMENT",
                        "location": {
                            "dimensionRange": {
                                "sheetId": sheet_id,
                                "dimension": "ROWS",
                                "startIndex": start_row - 1,
                                "endIndex": end_row,
                            }
                        },
                    }
                }
            },
        ]
    )
    return requests


def _data_plane(value: Mapping[str, Any]) -> dict[str, Any]:
    has_dataset = value.get("dataset_handle") is not None
    has_inline = value.get("inline_rows") is not None
    if has_dataset == has_inline:
        raise SheetsExtensionError(
            "invalid_payload", "Exactly one of dataset_handle or inline_rows is required"
        )
    if has_dataset:
        return {
            "dataset_handle": bounded_text(
                value.get("dataset_handle"), field="dataset_handle", maximum=512
            )
        }
    return {"inline_rows": _inline_rows(value.get("inline_rows"))}


def _inline_rows(value: Any) -> list[list[Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_INLINE_ROWS:
        raise SheetsExtensionError("invalid_payload", "inline_rows is invalid")
    result: list[list[Any]] = []
    cells = 0
    for row in value:
        if not isinstance(row, list) or not 1 <= len(row) <= MAX_INLINE_COLUMNS:
            raise SheetsExtensionError("invalid_payload", "inline_rows is invalid")
        normalized: list[Any] = []
        for cell in row:
            if isinstance(cell, float) and not math.isfinite(cell):
                raise SheetsExtensionError(
                    "invalid_payload", "inline_rows contains a non-finite number"
                )
            if not (cell is None or isinstance(cell, (str, bool, int, float))) or (
                isinstance(cell, str) and len(cell) > MAX_CELL_TEXT
            ):
                raise SheetsExtensionError(
                    "invalid_payload", "inline_rows contains an invalid cell"
                )
            normalized.append(cell)
        cells += len(normalized)
        if cells > MAX_INLINE_CELLS:
            raise SheetsExtensionError("invalid_payload", "inline_rows has too many cells")
        result.append(normalized)
    return result


def _sheet_selector(value: Mapping[str, Any]) -> dict[str, Any]:
    has_id = value.get("sheet_id") is not None
    has_title = value.get("sheet_title") is not None
    if has_id == has_title:
        raise SheetsExtensionError(
            "invalid_payload", "Exactly one of sheet_id or sheet_title is required"
        )
    if has_id:
        sheet_id = value.get("sheet_id")
        if isinstance(sheet_id, bool) or not isinstance(sheet_id, int) or sheet_id < 0:
            raise SheetsExtensionError("invalid_payload", "sheet_id is invalid")
        return {"sheet_id": sheet_id}
    return {"sheet_title": bounded_text(value.get("sheet_title"), field="sheet_title", maximum=100)}


def _start_cell(value: Any) -> str:
    normalized = normalize_relative_a1(value)
    cell_coordinates(normalized)
    return normalized


def row_source(payload: dict[str, Any], normalized: Mapping[str, Any]) -> RowSource:
    rows = normalized.get("inline_rows")
    if isinstance(rows, list):
        return _staged_row_source(
            rows,
            declared_rows=len(rows),
            declared_columns=_row_width(rows),
        )
    handle = normalized.get("dataset_handle")
    try:
        descriptor = find_dataset_descriptor(
            payload,
            dataset_id=str(handle or ""),
            binding_key=DATASET_BINDING_KEY,
            role="input",
        )
        access = descriptor.get("access")
        size = descriptor.get("size_bytes")
        max_size = access.get("max_size_bytes") if isinstance(access, Mapping) else None
        row_count = descriptor.get("row_count")
        schema = descriptor.get("schema")
        max_dataset_bytes = artifact_max_bytes()
        if (
            descriptor.get("role") != "input"
            or descriptor.get("binding_key") != DATASET_BINDING_KEY
            or descriptor.get("content_type") != DATASET_CONTENT_TYPE
            or not isinstance(access, Mapping)
            or access.get("transport") != "presigned_url"
            or access.get("mode") not in {"read", "read_write"}
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 <= size <= max_dataset_bytes
            or isinstance(max_size, bool)
            or not isinstance(max_size, int)
            or max_size < size
            or isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or not 1 <= row_count <= MAX_DATASET_ROWS
            or not _scope_matches(payload, descriptor)
        ):
            raise ArtifactAccessError("invalid dataset grant")
        columns = _dataset_columns(schema)
        if row_count * len(columns) > MAX_DATASET_CELLS:
            raise SheetsExtensionError("sheet_too_large", "The input dataset is too large")
        chunks = stream_dataset_bytes(
            payload,
            dataset_id=str(handle),
            binding_key=DATASET_BINDING_KEY,
            chunk_size=1024 * 1024,
            timeout_seconds=30.0,
        )
        return _staged_row_source(
            _dataset_rows(
                chunks,
                columns=columns,
                expected_rows=row_count,
                max_size_bytes=max_dataset_bytes,
            ),
            declared_rows=row_count,
            declared_columns=len(columns),
        )
    except SheetsExtensionError:
        raise
    except (ArtifactAccessError, TypeError, ValueError) as exc:
        raise SheetsExtensionError(
            "dataset_input_unavailable", "The approved input dataset is unavailable"
        ) from exc


def _staged_row_source(
    rows: Iterable[list[Any]],
    *,
    declared_rows: int,
    declared_columns: int,
) -> RowSource:
    # Ownership transfers to RowSource, whose operation boundary closes the spool.
    staged = tempfile.SpooledTemporaryFile(  # noqa: SIM115
        max_size=8 * 1024 * 1024, mode="w+b"
    )
    try:
        for row in rows:
            staged.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
        source = RowSource(
            staged_rows=staged,
            declared_rows=declared_rows,
            declared_columns=declared_columns,
        )
        source.validate_batches()
        return source
    except (OSError, TypeError, ValueError) as exc:
        staged.close()
        raise SheetsExtensionError(
            "dataset_input_unavailable", "The input dataset could not be validated"
        ) from exc
    except Exception:
        staged.close()
        raise


def _operation_id(payload: Mapping[str, Any]) -> str:
    runtime = payload.get("runtime_context")
    value = runtime.get("external_effect_id") if isinstance(runtime, Mapping) else None
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise SheetsExtensionError(
            "invalid_payload", "A stable host external-effect operation ID is required"
        )
    return text


def _created_spreadsheet(value: Any) -> tuple[str, str | None]:
    if not isinstance(value, Mapping) or set(value) - {
        "id",
        "name",
        "mimeType",
        "webViewLink",
        "size",
    }:
        raise SheetsExtensionError("provider_unavailable", "Google Drive returned invalid metadata")
    file_id = value.get("id")
    link = value.get("webViewLink")
    if (
        not isinstance(file_id, str)
        or not file_id
        or len(file_id) > 512
        or value.get("mimeType") != SPREADSHEET_MIME_TYPE
        or (link is not None and (not isinstance(link, str) or len(link) > 2048))
    ):
        raise SheetsExtensionError("provider_unavailable", "Google Drive returned invalid metadata")
    return file_id, link or None


def _create_sheet(transport: Any, spreadsheet_id: str, metadata: Any, title: str):
    if not isinstance(metadata, Mapping) or not isinstance(metadata.get("sheets"), list):
        raise SheetsExtensionError(
            "provider_unavailable", "Google Sheets returned invalid metadata"
        )
    matches = []
    for row in metadata["sheets"]:
        try:
            matches.append(selected_sheet({"sheets": [row]}, {"sheet_title": title}))
        except SheetsExtensionError as exc:
            if exc.code != "google_sheet_not_found":
                raise
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1 or len(metadata["sheets"]) != 1:
        raise SheetsExtensionError("google_sheet_ambiguous", "The created spreadsheet is ambiguous")
    first = selected_sheet(
        metadata,
        {
            "sheet_id": metadata["sheets"][0].get("properties", {}).get("sheetId")
            if isinstance(metadata["sheets"][0], Mapping)
            else None
        },
    )
    transport.rename_sheet(spreadsheet_id, first["sheet_id"], title)
    return {**first, "title": title}


def _clear_overwrite_rectangle(
    transport: Any,
    spreadsheet_id: str,
    sheet: Mapping[str, Any],
    normalized: Mapping[str, Any],
    *,
    incoming_rows: int,
    incoming_columns: int,
) -> None:
    start_column, start_row = cell_coordinates(normalized["start_cell"])
    old_range = _absolute_range(
        sheet["title"], start_column, start_row, sheet["column_count"], sheet["row_count"]
    )
    old_values = _strict_values(transport.values_get(spreadsheet_id, old_range))
    old_width = max((len(row) for row in old_values), default=0)
    height = max(len(old_values), incoming_rows)
    width = max(old_width, incoming_columns)
    if height and width:
        transport.values_clear(
            spreadsheet_id,
            _absolute_range(
                sheet["title"],
                start_column,
                start_row,
                start_column + width - 1,
                start_row + height - 1,
            ),
        )


def _write_value_batches(
    transport: Any,
    spreadsheet_id: str,
    sheet_title: str,
    start_cell: str,
    rows: Iterable[list[Any]],
    *,
    width: int,
    expected_rows: int,
) -> int:
    start_column, start_row = cell_coordinates(start_cell)
    batch_start = start_row
    actual_rows = 0
    for batch in _row_batches(rows, width=width, expected_rows=expected_rows):
        _send_value_batch(
            transport,
            spreadsheet_id,
            sheet_title,
            start_column,
            batch_start,
            width,
            batch,
        )
        batch_start += len(batch)
        actual_rows += len(batch)
    if actual_rows != expected_rows:
        raise SheetsExtensionError(
            "dataset_input_unavailable", _MESSAGE_THE_INPUT_DATASET_ROW_COUNT_IS_INVALID
        )
    return actual_rows


def _row_batches(
    rows: Iterable[list[Any]],
    *,
    width: int,
    expected_rows: int,
    max_bytes: int = MAX_REQUEST_BYTES,
) -> Iterator[list[list[Any]]]:
    batch: list[list[Any]] = []
    actual_rows = 0
    effective_limit = max_bytes
    for row in rows:
        if not isinstance(row, list) or len(row) > width:
            raise SheetsExtensionError(
                "dataset_input_unavailable", "The input dataset shape is invalid"
            )
        actual_rows += 1
        if actual_rows > expected_rows:
            raise SheetsExtensionError(
                "dataset_input_unavailable", _MESSAGE_THE_INPUT_DATASET_ROW_COUNT_IS_INVALID
            )
        candidate = [*batch, row]
        encoded = json.dumps(
            {"values": candidate}, separators=(",", ":"), ensure_ascii=False
        ).encode()
        if len(encoded) > effective_limit and batch:
            yield batch
            batch = [row]
            if (
                len(
                    json.dumps(
                        {"values": batch}, separators=(",", ":"), ensure_ascii=False
                    ).encode()
                )
                > effective_limit
            ):
                raise SheetsExtensionError("sheet_too_large", "One Sheets write row is too large")
        elif len(encoded) > effective_limit:
            raise SheetsExtensionError("sheet_too_large", "One Sheets write row is too large")
        else:
            batch = candidate
    if batch:
        yield batch
    if actual_rows != expected_rows:
        raise SheetsExtensionError(
            "dataset_input_unavailable", _MESSAGE_THE_INPUT_DATASET_ROW_COUNT_IS_INVALID
        )


def _send_value_batch(
    transport: Any,
    spreadsheet_id: str,
    title: str,
    start_column: int,
    start_row: int,
    width: int,
    rows: list[list[Any]],
) -> None:
    provider_range = _absolute_range(
        title,
        start_column,
        start_row,
        start_column + width - 1,
        start_row + len(rows) - 1,
    )
    transport.values_update(spreadsheet_id, provider_range, rows, value_input_option="RAW")


def _strict_values(value: Any) -> list[list[Any]]:
    if not isinstance(value, Mapping) or set(value) - {"range", "majorDimension", "values"}:
        raise SheetsExtensionError("provider_unavailable", "Google Sheets returned invalid values")
    rows = value.get("values", [])
    if not isinstance(rows, list) or any(
        not isinstance(row, list)
        or any(cell is not None and not isinstance(cell, str) for cell in row)
        for row in rows
    ):
        raise SheetsExtensionError("provider_unavailable", "Google Sheets returned invalid values")
    return rows


def _row_width(rows: list[list[Any]]) -> int:
    return max((len(row) for row in rows), default=0)


def _dataset_columns(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        if set(value) != {"columns"}:
            raise ArtifactAccessError(_MESSAGE_INVALID_DATASET_SCHEMA)
        value = value.get("columns")
    if not isinstance(value, list) or not 1 <= len(value) <= 18_278:
        raise ArtifactAccessError(_MESSAGE_INVALID_DATASET_SCHEMA)
    result: list[str] = []
    for row in value:
        name = row.get("name") if isinstance(row, Mapping) else None
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 255
            or name in result
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
        ):
            raise ArtifactAccessError(_MESSAGE_INVALID_DATASET_SCHEMA)
        result.append(name)
    return result


def _dataset_rows(
    chunks: Iterable[bytes],
    *,
    columns: list[str],
    expected_rows: int,
    max_size_bytes: int,
) -> Iterator[list[Any]]:
    buffer = bytearray()
    row_count = 0
    total_bytes = 0
    for raw in chunks:
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            raise SheetsExtensionError(
                "dataset_input_unavailable", "The input dataset stream is invalid"
            )
        buffer.extend(raw)
        total_bytes += len(raw)
        if total_bytes > max_size_bytes or len(buffer) > MAX_REQUEST_BYTES:
            raise SheetsExtensionError("sheet_too_large", "The input dataset is too large")
        while b"\n" in buffer:
            line, _, remainder = buffer.partition(b"\n")
            buffer = bytearray(remainder)
            if not line:
                continue
            row_count += 1
            yield _dataset_line(line, columns)
    if buffer:
        row_count += 1
        yield _dataset_line(bytes(buffer), columns)
    if row_count != expected_rows:
        raise SheetsExtensionError(
            "dataset_input_unavailable", _MESSAGE_THE_INPUT_DATASET_ROW_COUNT_IS_INVALID
        )


def _dataset_line(line: bytes, columns: list[str]) -> list[Any]:
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SheetsExtensionError(
            "dataset_input_unavailable", "The input dataset contains invalid NDJSON"
        ) from exc
    if not isinstance(value, Mapping) or set(value) - set(columns):
        raise SheetsExtensionError(
            "dataset_input_unavailable", "The input dataset row shape is invalid"
        )
    row = [value.get(column) for column in columns]
    if any(not _valid_cell(cell) for cell in row):
        raise SheetsExtensionError(
            "dataset_input_unavailable", "The input dataset contains an invalid cell"
        )
    return row


def _valid_cell(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, str) and len(value) <= MAX_CELL_TEXT


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


def _quoted_range(title: str, relative_range: str) -> str:
    return f"{_quoted_sheet_title(title)}!{relative_range}"


def _absolute_range(
    title: str, start_column: int, start_row: int, end_column: int, end_row: int
) -> str:
    return (
        f"{_quoted_sheet_title(title)}!{column_name(start_column)}{start_row}:"
        f"{column_name(end_column)}{end_row}"
    )


def _quoted_sheet_title(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _cell_row(row: list[Any]) -> dict[str, Any]:
    return {"values": [{"userEnteredValue": _cell_value(value)} for value in row]}


def _cell_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, (int, float)):
        return {"numberValue": value}
    return {"stringValue": value}


def _marker_rows(value: Mapping[str, Any], *, sheet_id: int) -> tuple[int, int] | None:
    location = value.get("location")
    dimension = location.get("dimensionRange") if isinstance(location, Mapping) else None
    if not isinstance(dimension, Mapping):
        return None
    start = dimension.get("startIndex")
    end = dimension.get("endIndex")
    if (
        dimension.get("sheetId") != sheet_id
        or dimension.get("dimension") != "ROWS"
        or isinstance(start, bool)
        or not isinstance(start, int)
        or start < 0
        or isinstance(end, bool)
        or not isinstance(end, int)
        or end <= start
    ):
        raise SheetsExtensionError(
            "provider_unavailable", "Google Sheets returned invalid operation metadata"
        )
    return start + 1, end


__all__ = [
    "append_google_sheet_rows",
    "validated_append_input",
    "validated_write_input",
    "write_google_sheet",
]
