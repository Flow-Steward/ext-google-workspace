from __future__ import annotations

import io
import json
import math
from urllib.error import HTTPError

import pytest


def _rows():
    return [["SKU", "Qty"], ["A", 10], ["B", None]]


def _create_input(**overrides):
    value = {
        "connection_ref": "google-a",
        "mode": "create",
        "inline_rows": _rows(),
        "spreadsheet_title": "Stock feed",
        "destination_folder_id": "folder-1",
        "sheet_title": "Inventory",
        "start_cell": "A1",
    }
    value.update(overrides)
    return value


def _overwrite_input(**overrides):
    value = {
        "connection_ref": "google-a",
        "mode": "overwrite",
        "inline_rows": _rows(),
        "spreadsheet_id": "spreadsheet-1",
        "sheet_id": 123,
        "start_cell": "B2",
    }
    value.update(overrides)
    return value


def _append_input(**overrides):
    value = {
        "connection_ref": "google-a",
        "spreadsheet_id": "spreadsheet-1",
        "sheet_id": 123,
        "table_range": "A1:B20",
        "inline_rows": [["C", 2], ["D", 3]],
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    "value",
    [
        _create_input(dataset_handle="dataset:one"),
        {key: item for key, item in _create_input().items() if key != "inline_rows"},
        _create_input(mode="merge"),
        _create_input(extra="forbidden"),
        _create_input(destination_folder_id=None),
        _create_input(destination_folder_id=""),
        _overwrite_input(sheet_title="Data"),
        {key: item for key, item in _overwrite_input().items() if key != "sheet_id"},
    ],
)
def test_write_contract_is_closed_and_requires_exactly_one_data_plane(value) -> None:
    from sheets.errors import SheetsExtensionError
    from sheets.writes import validated_write_input

    with pytest.raises(SheetsExtensionError) as exc_info:
        validated_write_input(value)
    assert exc_info.value.code == "invalid_payload"


@pytest.mark.parametrize(
    "rows",
    [
        [["x"] * 101],
        [["x"] for _ in range(101)],
        [[object()]],
        [[math.inf]],
        [[math.nan]],
        [["x" * 50_001]],
        "not-rows",
    ],
)
def test_inline_rows_enforce_100_by_100_10k_and_scalar_bounds(rows) -> None:
    from sheets.errors import SheetsExtensionError
    from sheets.writes import validated_write_input

    _raises_input_92_1 = _create_input(inline_rows=rows)
    with pytest.raises(SheetsExtensionError) as exc_info:
        validated_write_input(_raises_input_92_1)
    assert exc_info.value.code == "invalid_payload"


class SheetsMutationTransport:
    def __init__(self) -> None:
        self.updates = []
        self.clears = []
        self.batches = []
        self.marker = None
        self.old_values = [["old", "old"], ["old", "old"]]
        self.metadata = {
            "sheets": [
                {
                    "properties": {
                        "sheetId": 123,
                        "title": "Inventory",
                        "gridProperties": {"rowCount": 100, "columnCount": 10},
                    }
                }
            ]
        }

    def spreadsheet_metadata(self, _spreadsheet_id):
        return self.metadata

    def values_get(self, _spreadsheet_id, _provider_range):
        return {"values": self.old_values}

    def values_clear(self, spreadsheet_id, provider_range):
        self.clears.append((spreadsheet_id, provider_range))

    def values_update(self, spreadsheet_id, provider_range, rows, *, value_input_option):
        self.updates.append((spreadsheet_id, provider_range, rows, value_input_option))

    def find_developer_marker(self, spreadsheet_id, marker):
        return self.marker

    def batch_update(self, spreadsheet_id, requests):
        self.batches.append((spreadsheet_id, requests))
        return {}


class DriveCreateTransport:
    def __init__(self, existing=None) -> None:
        self.existing = existing
        self.searches = []
        self.creates = []

    def find_by_operation_marker(self, operation_id, folder_id):
        self.searches.append((operation_id, folder_id))
        return self.existing

    def create_google_spreadsheet(self, metadata):
        self.creates.append(metadata)
        return {
            "id": "spreadsheet-1",
            "name": metadata["name"],
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "webViewLink": "https://docs.google.com/spreadsheets/d/spreadsheet-1/edit",
        }


def _runtime():
    return {"runtime_context": {"external_effect_id": "effect-123"}}


def test_create_uses_one_drive_marker_and_raw_value_batches() -> None:
    from sheets.writes import write_google_sheet

    sheets = SheetsMutationTransport()
    drive = DriveCreateTransport()
    result = write_google_sheet(sheets, drive, _runtime(), _create_input())

    assert drive.searches == [("effect-123", "folder-1")]
    assert drive.creates == [
        {
            "name": "Stock feed",
            "parents": ["folder-1"],
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "appProperties": {"flow_steward_operation_id": "effect-123"},
        }
    ]
    assert sheets.updates == [
        (
            "spreadsheet-1",
            "'Inventory'!A1:B3",
            _rows(),
            "RAW",
        )
    ]
    assert result == {
        "spreadsheet_id": "spreadsheet-1",
        "sheet_id": 123,
        "web_view_link": "https://docs.google.com/spreadsheets/d/spreadsheet-1/edit",
        "row_count": 3,
        "column_count": 2,
    }


def test_create_without_destination_folder_uses_drive_root_alias() -> None:
    from sheets.writes import write_google_sheet

    sheets = SheetsMutationTransport()
    drive = DriveCreateTransport()
    operation_input = {
        key: value for key, value in _create_input().items() if key != "destination_folder_id"
    }

    result = write_google_sheet(sheets, drive, _runtime(), operation_input)

    assert drive.searches == [("effect-123", "root")]
    assert drive.creates == [
        {
            "name": "Stock feed",
            "parents": ["root"],
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "appProperties": {"flow_steward_operation_id": "effect-123"},
        }
    ]
    assert result["spreadsheet_id"] == "spreadsheet-1"


def test_create_retry_resumes_existing_marked_spreadsheet_without_duplicate() -> None:
    from sheets.writes import write_google_sheet

    existing = {
        "id": "spreadsheet-1",
        "name": "Stock feed",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "webViewLink": "https://docs.google.com/spreadsheets/d/spreadsheet-1/edit",
    }
    drive = DriveCreateTransport(existing=existing)
    sheets = SheetsMutationTransport()

    result = write_google_sheet(sheets, drive, _runtime(), _create_input())

    assert result["spreadsheet_id"] == "spreadsheet-1"
    assert drive.creates == []
    assert len(sheets.updates) == 1


def test_overwrite_clears_greater_old_or_incoming_rectangle_then_writes_raw() -> None:
    from sheets.writes import write_google_sheet

    sheets = SheetsMutationTransport()
    sheets.old_values = [["old", "old", "old"], ["old", "old", "old"]]
    result = write_google_sheet(sheets, DriveCreateTransport(), _runtime(), _overwrite_input())

    assert sheets.clears == [("spreadsheet-1", "'Inventory'!B2:D4")]
    assert sheets.updates == [("spreadsheet-1", "'Inventory'!B2:C4", _rows(), "RAW")]
    assert result["spreadsheet_id"] == "spreadsheet-1"
    assert result["sheet_id"] == 123


def test_append_checks_chunk_marker_and_commits_cells_plus_marker_atomically() -> None:
    from sheets.writes import append_google_sheet_rows

    sheets = SheetsMutationTransport()
    result = append_google_sheet_rows(sheets, _runtime(), _append_input())

    assert len(sheets.batches) == 1
    spreadsheet_id, requests = sheets.batches[0]
    assert spreadsheet_id == "spreadsheet-1"
    assert len(requests) == 2
    assert requests[0]["updateCells"]["fields"] == "userEnteredValue"
    assert requests[1]["createDeveloperMetadata"]["developerMetadata"]["metadataValue"] == (
        "effect-123:chunk:0"
    )
    assert result == {
        "spreadsheet_id": "spreadsheet-1",
        "sheet_id": 123,
        "appended_range": "'Inventory'!A3:B4",
        "row_count": 2,
        "column_count": 2,
    }


def test_append_retry_with_existing_marker_never_appends_duplicate() -> None:
    from sheets.writes import append_google_sheet_rows

    sheets = SheetsMutationTransport()
    sheets.marker = {"metadataValue": "effect-123:chunk:0", "location": {"spreadsheet": True}}
    result = append_google_sheet_rows(sheets, _runtime(), _append_input())
    assert result["row_count"] == 2
    assert sheets.batches == []


def test_append_streams_large_rows_in_distinct_idempotent_chunks() -> None:
    from sheets.writes import append_google_sheet_rows

    sheets = SheetsMutationTransport()
    rows = [["x" * 50_000] for _ in range(6)]
    result = append_google_sheet_rows(
        sheets,
        _runtime(),
        _append_input(inline_rows=rows, table_range="A1:A20"),
    )

    assert result["row_count"] == 6
    assert len(sheets.batches) == 2
    markers = [
        requests[-1]["createDeveloperMetadata"]["developerMetadata"]["metadataValue"]
        for _spreadsheet_id, requests in sheets.batches
    ]
    assert markers == ["effect-123:chunk:0", "effect-123:chunk:1"]
    assert all(
        len(json.dumps({"requests": requests}).encode()) <= 2 * 1024 * 1024
        for _spreadsheet_id, requests in sheets.batches
    )


def test_append_retry_advances_after_committed_chunk_outside_table_range() -> None:
    from sheets.writes import append_google_sheet_rows

    class RetryTransport(SheetsMutationTransport):
        def find_developer_marker(self, _spreadsheet_id, marker):
            if marker == "effect-123:chunk:0":
                return {
                    "metadataValue": marker,
                    "location": {
                        "dimensionRange": {
                            "sheetId": 123,
                            "dimension": "ROWS",
                            "startIndex": 20,
                            "endIndex": 25,
                        }
                    },
                }
            return None

    sheets = RetryTransport()
    rows = [["x" * 50_000] for _ in range(6)]

    result = append_google_sheet_rows(
        sheets,
        _runtime(),
        _append_input(inline_rows=rows, table_range="A1:A20"),
    )

    assert len(sheets.batches) == 1
    update = sheets.batches[0][1][0]["updateCells"]
    assert update["start"]["rowIndex"] == 25
    assert result["appended_range"] == "'Inventory'!A21:A26"


def test_sheets_timeout_response_uses_host_ambiguity_vocabulary() -> None:
    from sheets import operations

    response = operations._error("timeout_unknown", "Outcome is unknown", ambiguous=True)

    assert response["external_effect_status"] == "timeout_unknown"
    assert response["definitely_no_external_effect"] is False


@pytest.mark.parametrize("operation", ["write_google_sheet", "append_google_sheet_rows"])
def test_sheet_mutation_test_mode_suppresses_before_oauth_dataset_or_provider(
    monkeypatch, operation
) -> None:
    from sheets import operations

    monkeypatch.setattr(
        operations,
        "access_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no OAuth")),
    )
    monkeypatch.setattr(
        operations,
        "SheetsTransport",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no provider")),
    )
    input_payload = _create_input() if operation == "write_google_sheet" else _append_input()
    response = operations.handle_runtime(
        {
            "mode": "action",
            "runtime_context": {"test_mode": True, "external_effect_id": "effect-123"},
            "action": {
                "action_id": operation,
                "page_id": "workflow-builder",
                "component_id": "workflow-test-run",
                "context": {"workflow_id": "release-audit-workflow"},
                "input": input_payload,
            },
        }
    )
    assert response["ok"] is True
    assert response["external_effect_status"] == "suppressed"
    assert response["definitely_no_external_effect"] is True


def test_sheets_mutation_transport_uses_raw_fixed_paths_and_atomic_batch() -> None:
    from sheets.transport import SheetsTransport

    requests = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def opener(request, **kwargs):
        requests.append((request, kwargs))
        if "developerMetadata:search" in request.full_url:
            return Response(
                b'{"matchedDeveloperMetadata":[{"developerMetadata":'
                b'{"metadataKey":"flow_steward_operation_id","metadataValue":"effect:chunk:0"}}]}'
            )
        return Response(b"{}")

    transport = SheetsTransport("token", opener=opener)
    transport.values_clear("spreadsheet-1", "'Data'!A1:B2")
    transport.values_update(
        "spreadsheet-1", "'Data'!A1:B2", [["=not-a-formula", 2]], value_input_option="RAW"
    )
    transport.batch_update("spreadsheet-1", [{"updateCells": {"fields": "userEnteredValue"}}])
    marker = transport.find_developer_marker("spreadsheet-1", "effect:chunk:0")

    assert [request.get_method() for request, _kwargs in requests] == [
        "POST",
        "PUT",
        "POST",
        "POST",
    ]
    assert requests[0][0].full_url.endswith("/values/%27Data%27%21A1%3AB2:clear")
    assert requests[1][0].full_url.endswith("/values/%27Data%27%21A1%3AB2?valueInputOption=RAW")
    assert json.loads(requests[1][0].data) == {
        "majorDimension": "ROWS",
        "values": [["=not-a-formula", 2]],
    }
    assert requests[2][0].full_url.endswith("/spreadsheets/spreadsheet-1:batchUpdate")
    assert marker["metadataValue"] == "effect:chunk:0"


def test_sheets_mutation_timeout_is_unknown_and_not_replayed() -> None:
    from sheets.errors import SheetsExtensionError
    from sheets.transport import SheetsTransport

    calls = 0

    def opener(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise HTTPError("url", 503, "private-canary", {}, io.BytesIO(b"{}"))

    _raises_callable_439_1 = SheetsTransport("token", opener=opener).values_clear
    with pytest.raises(SheetsExtensionError) as exc_info:
        _raises_callable_439_1("spreadsheet-1", "'Data'!A1:B2")
    assert exc_info.value.code == "timeout_unknown"
    assert exc_info.value.retry_facts() == {
        "provider_error_code": "timeout_unknown",
        "failure_class": "provider",
        "retryable": False,
        "definitely_no_external_effect": False,
        "external_effect_status": "timeout_unknown",
        "http_status": 503,
    }
    assert calls == 1
    assert "canary" not in str(exc_info.value).lower()


def test_dataset_rows_stream_from_owned_grant_in_schema_order(monkeypatch) -> None:
    from sheets import writes

    descriptor = {
        "dataset_id": "one",
        "dataset_handle": "dataset:one",
        "role": "input",
        "binding_key": "google_sheet_input_dataset",
        "content_type": "application/x-ndjson",
        "size_bytes": 48,
        "row_count": 2,
        "schema": [{"name": "sku"}, {"name": "qty"}],
        "scope": {"account_id": "account-a", "project_id": "project-a"},
        "access": {
            "transport": "presigned_url",
            "mode": "read",
            "max_size_bytes": 48,
        },
    }
    monkeypatch.setattr(writes, "find_dataset_descriptor", lambda *_args, **_kwargs: descriptor)
    monkeypatch.setattr(
        writes,
        "stream_dataset_bytes",
        lambda *_args, **_kwargs: iter([b'{"qty":2,"sku":"A"}\n{"sku":"B",', b'"qty":null}\n']),
    )
    payload = {
        **_runtime(),
        "host": {"scope": {"account_id": "account-a", "project_id": "project-a"}},
    }
    input_payload = _create_input()
    input_payload.pop("inline_rows")
    input_payload["dataset_handle"] = "dataset:one"
    sheets = SheetsMutationTransport()

    result = writes.write_google_sheet(sheets, DriveCreateTransport(), payload, input_payload)

    assert result["row_count"] == 2
    assert sheets.updates[0][2] == [["A", 2], ["B", None]]


def test_dataset_input_limit_uses_operator_configuration(monkeypatch) -> None:
    from sheets import writes

    configured = 300 * 1024 * 1024
    declared_size = 200 * 1024 * 1024 + 1
    monkeypatch.setenv("FS_EXTENSION_ARTIFACT_MAX_BYTES", str(configured))
    descriptor = {
        "dataset_id": "one",
        "dataset_handle": "dataset:one",
        "role": "input",
        "binding_key": "google_sheet_input_dataset",
        "content_type": "application/x-ndjson",
        "size_bytes": declared_size,
        "row_count": 1,
        "schema": {"columns": [{"name": "sku", "type": "String"}]},
        "scope": {"account_id": "account-a", "project_id": "project-a"},
        "access": {
            "transport": "presigned_url",
            "mode": "read",
            "max_size_bytes": declared_size,
        },
    }
    monkeypatch.setattr(writes, "find_dataset_descriptor", lambda *_args, **_kwargs: descriptor)
    monkeypatch.setattr(
        writes,
        "stream_dataset_bytes",
        lambda *_args, **_kwargs: iter([b'{"sku":"A"}\n']),
    )
    payload = {
        **_runtime(),
        "host": {"scope": {"account_id": "account-a", "project_id": "project-a"}},
    }
    input_payload = _create_input()
    input_payload.pop("inline_rows")
    input_payload["dataset_handle"] = "dataset:one"
    sheets = SheetsMutationTransport()

    result = writes.write_google_sheet(sheets, DriveCreateTransport(), payload, input_payload)

    assert result["row_count"] == 1
    assert sheets.updates[0][2] == [["A"]]


def test_read_dataset_output_is_accepted_as_canonical_write_input(monkeypatch) -> None:
    from sheets import reads, writes

    monkeypatch.setattr(reads, "dataset_grant_limit", lambda _payload: 1024)
    captured: list[bytes] = []

    def writer(_payload, chunks, **_kwargs):
        body = b"".join(chunks)
        captured.append(body)
        return {"dataset_handle": "dataset:read-output", "size_bytes": len(body)}

    read_transport = type(
        "ReadTransport",
        (),
        {
            "spreadsheet_metadata": lambda self, _spreadsheet_id: {
                "sheets": [
                    {
                        "properties": {
                            "sheetId": 123,
                            "title": "Inventory",
                            "gridProperties": {"rowCount": 3, "columnCount": 2},
                        }
                    }
                ]
            },
            "values_get": lambda self, _spreadsheet_id, _provider_range: {
                "values": [["sku", "qty"], ["A", "2"], ["B", "3"]]
            },
        },
    )()
    read_result = reads.read_google_sheet(
        read_transport,
        {},
        {
            "connection_ref": "google-a",
            "spreadsheet_id": "spreadsheet-1",
            "sheet_id": 123,
            "header_mode": "first_row",
        },
        dataset_writer=writer,
    )
    descriptor = {
        "dataset_id": "read-output",
        "dataset_handle": read_result["dataset_handle"],
        "role": "input",
        "binding_key": "google_sheet_input_dataset",
        "content_type": "application/x-ndjson",
        "size_bytes": len(captured[0]),
        "row_count": read_result["row_count"],
        "schema": {
            "columns": [
                {"name": column["name"], "type": "text"} for column in read_result["schema"]
            ]
        },
        "scope": {"account_id": "account-a", "project_id": "project-a"},
        "access": {
            "transport": "presigned_url",
            "mode": "read",
            "max_size_bytes": len(captured[0]),
        },
    }
    monkeypatch.setattr(writes, "find_dataset_descriptor", lambda *_args, **_kwargs: descriptor)
    monkeypatch.setattr(
        writes,
        "stream_dataset_bytes",
        lambda *_args, **_kwargs: iter([captured[0]]),
    )
    write_input = _create_input()
    write_input.pop("inline_rows")
    write_input["dataset_handle"] = read_result["dataset_handle"]
    sheets = SheetsMutationTransport()

    result = writes.write_google_sheet(
        sheets,
        DriveCreateTransport(),
        {
            **_runtime(),
            "host": {"scope": {"account_id": "account-a", "project_id": "project-a"}},
        },
        write_input,
    )

    assert result["row_count"] == 2
    assert sheets.updates[0][2] == [["A", "2"], ["B", "3"]]


@pytest.mark.parametrize("operation", ["create", "overwrite", "append"])
def test_late_dataset_failure_happens_before_any_google_mutation(monkeypatch, operation) -> None:
    from flowsteward_extension_sdk import ArtifactAccessError
    from sheets import writes
    from sheets.errors import SheetsExtensionError

    descriptor = {
        "dataset_id": "one",
        "dataset_handle": "dataset:one",
        "role": "input",
        "binding_key": "google_sheet_input_dataset",
        "content_type": "application/x-ndjson",
        "size_bytes": 48,
        "row_count": 2,
        "schema": {"columns": [{"name": "sku", "type": "text"}]},
        "scope": {"account_id": "account-a", "project_id": "project-a"},
        "access": {"transport": "presigned_url", "mode": "read", "max_size_bytes": 48},
    }

    def failing_stream(*_args, **_kwargs):
        yield b'{"sku":"A"}\n'
        raise ArtifactAccessError("checksum mismatch private canary")

    monkeypatch.setattr(writes, "find_dataset_descriptor", lambda *_args, **_kwargs: descriptor)
    monkeypatch.setattr(writes, "stream_dataset_bytes", failing_stream)
    input_payload = (
        _append_input()
        if operation == "append"
        else (_overwrite_input() if operation == "overwrite" else _create_input())
    )
    input_payload.pop("inline_rows")
    input_payload["dataset_handle"] = "dataset:one"
    sheets = SheetsMutationTransport()
    drive = DriveCreateTransport()
    payload = {
        **_runtime(),
        "host": {"scope": {"account_id": "account-a", "project_id": "project-a"}},
    }

    def _raises_operation_665():
        if operation == "append":
            writes.append_google_sheet_rows(sheets, payload, input_payload)
        else:
            writes.write_google_sheet(sheets, drive, payload, input_payload)

    with pytest.raises(SheetsExtensionError) as exc_info:
        _raises_operation_665()

    assert exc_info.value.code == "dataset_input_unavailable"
    assert drive.searches == []
    assert drive.creates == []
    assert sheets.clears == []
    assert sheets.updates == []
    assert sheets.batches == []


def test_test_mode_consumes_and_rejects_late_dataset_failure_before_oauth(monkeypatch) -> None:
    from sheets import operations, writes

    descriptor = {
        "dataset_id": "one",
        "dataset_handle": "dataset:one",
        "role": "input",
        "binding_key": "google_sheet_input_dataset",
        "content_type": "application/x-ndjson",
        "size_bytes": 32,
        "row_count": 2,
        "schema": {"columns": [{"name": "sku", "type": "text"}]},
        "scope": {"account_id": "account-a", "project_id": "project-a"},
        "access": {"transport": "presigned_url", "mode": "read", "max_size_bytes": 32},
    }
    monkeypatch.setattr(writes, "find_dataset_descriptor", lambda *_args, **_kwargs: descriptor)
    monkeypatch.setattr(
        writes,
        "stream_dataset_bytes",
        lambda *_args, **_kwargs: iter([b'{"sku":"A"}\nnot-json\n']),
    )
    monkeypatch.setattr(
        operations,
        "access_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no OAuth")),
    )
    input_payload = _append_input()
    input_payload.pop("inline_rows")
    input_payload["dataset_handle"] = "dataset:one"

    response = operations.handle_runtime(
        {
            "mode": "action",
            "runtime_context": {"test_mode": True, "external_effect_id": "effect-123"},
            "host": {"scope": {"account_id": "account-a", "project_id": "project-a"}},
            "action": {"action_id": "append_google_sheet_rows", "input": input_payload},
        }
    )

    assert response["ok"] is False
    assert response["error_code"] == "dataset_input_unavailable"


def test_dataset_scope_mismatch_in_test_mode_fails_before_oauth_or_provider(monkeypatch) -> None:
    from sheets import operations, writes

    descriptor = {
        "dataset_id": "one",
        "dataset_handle": "dataset:one",
        "role": "input",
        "binding_key": "google_sheet_input_dataset",
        "content_type": "application/x-ndjson",
        "size_bytes": 10,
        "row_count": 1,
        "schema": [{"name": "sku"}],
        "scope": {"account_id": "account-a", "project_id": "other-project"},
        "access": {"transport": "presigned_url", "mode": "read", "max_size_bytes": 10},
    }
    monkeypatch.setattr(writes, "find_dataset_descriptor", lambda *_args, **_kwargs: descriptor)
    monkeypatch.setattr(
        operations,
        "access_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no OAuth")),
    )
    input_payload = _create_input()
    input_payload.pop("inline_rows")
    input_payload["dataset_handle"] = "dataset:one"
    response = operations.handle_runtime(
        {
            "mode": "action",
            "runtime_context": {"test_mode": True, "external_effect_id": "effect-123"},
            "host": {"scope": {"account_id": "account-a", "project_id": "project-a"}},
            "action": {"action_id": "write_google_sheet", "input": input_payload},
        }
    )
    assert response["ok"] is False
    assert response["error_code"] == "dataset_input_unavailable"
