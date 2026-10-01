from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from urllib.error import HTTPError

import pytest


def test_sheets_retry_after_accepts_http_date_and_408() -> None:
    from sheets.transport import _retry_after_seconds, _retryable_status

    value = format_datetime(datetime.now(UTC) + timedelta(minutes=10), usegmt=True)

    assert 590 <= (_retry_after_seconds({"Retry-After": value}) or 0) <= 600
    assert _retryable_status(408) is True


class SheetsTransport:
    def __init__(self, metadata: dict, pages: list[dict]) -> None:
        self.metadata = metadata
        self.pages = list(pages)
        self.calls: list[tuple[str, str]] = []

    def spreadsheet_metadata(self, spreadsheet_id: str):
        assert spreadsheet_id == "spreadsheet-1"
        return self.metadata

    def values_get(self, spreadsheet_id: str, provider_range: str):
        self.calls.append((spreadsheet_id, provider_range))
        return self.pages.pop(0) if self.pages else {"range": provider_range}


def _metadata(*properties):
    return {"sheets": [{"properties": value} for value in properties]}


def _input(**overrides):
    value = {
        "connection_ref": "google-a",
        "spreadsheet_id": "spreadsheet-1",
        "sheet_id": 123,
        "header_mode": "first_row",
    }
    value.update(overrides)
    return value


def _writer(captured: list[bytes]):
    def write(_payload, chunks, **kwargs):
        body = b"".join(chunks)
        captured.append(body)
        return {
            "dataset_handle": "dataset:sheet-read",
            "size_bytes": len(body),
            "content_type": kwargs["content_type"],
        }

    return write


def test_read_sheet_streams_ndjson_and_returns_only_bounded_preview(monkeypatch) -> None:
    from sheets import reads

    monkeypatch.setattr(reads, "dataset_grant_limit", lambda _payload: 1024 * 1024)
    captured: list[bytes] = []
    transport = SheetsTransport(
        _metadata(
            {"sheetId": 123, "title": "Data", "gridProperties": {"rowCount": 3, "columnCount": 3}}
        ),
        [
            {
                "range": "'Data'!A1:C3",
                "majorDimension": "ROWS",
                "values": [["SKU", "", "SKU"], ["A", "10"], ["B", "", "tail"]],
            }
        ],
    )

    result = reads.read_google_sheet(
        transport,
        {},
        _input(),
        dataset_writer=_writer(captured),
    )

    assert transport.calls == [("spreadsheet-1", "'Data'!A1:C3")]
    assert [json.loads(line) for line in captured[0].splitlines()] == [
        {"SKU": "A", "column_2": "10", "SKU_2": None},
        {"SKU": "B", "column_2": None, "SKU_2": "tail"},
    ]
    assert result == {
        "dataset_handle": "dataset:sheet-read",
        "row_count": 2,
        "column_count": 3,
        "schema": [
            {"name": "SKU", "type": "string", "nullable": True},
            {"name": "column_2", "type": "string", "nullable": True},
            {"name": "SKU_2", "type": "string", "nullable": True},
        ],
        "preview": [
            {"SKU": "A", "column_2": "10", "SKU_2": None},
            {"SKU": "B", "column_2": None, "SKU_2": "tail"},
        ],
        "spreadsheet_id": "spreadsheet-1",
        "sheet_id": 123,
    }
    assert "values" not in result


def test_read_by_title_rejects_duplicate_or_malformed_provider_sheets(monkeypatch) -> None:
    from sheets import reads
    from sheets.errors import SheetsExtensionError

    monkeypatch.setattr(reads, "dataset_grant_limit", lambda _payload: 1024)
    duplicate = SheetsTransport(
        _metadata(
            {"sheetId": 1, "title": "Data", "gridProperties": {"rowCount": 1, "columnCount": 1}},
            {"sheetId": 2, "title": "Data", "gridProperties": {"rowCount": 1, "columnCount": 1}},
        ),
        [],
    )
    _raises_input_125_1 = _input(sheet_id=None, sheet_title="Data")
    _raises_input_125_2 = _writer([])
    with pytest.raises(SheetsExtensionError) as exc_info:
        reads.read_google_sheet(
            duplicate, {}, _raises_input_125_1, dataset_writer=_raises_input_125_2
        )
    assert exc_info.value.code == "google_sheet_ambiguous"

    malformed = SheetsTransport({"sheets": "bad"}, [])
    _raises_input_135_1 = _input()
    _raises_input_135_2 = _writer([])
    with pytest.raises(SheetsExtensionError) as exc_info:
        reads.read_google_sheet(
            malformed, {}, _raises_input_135_1, dataset_writer=_raises_input_135_2
        )
    assert exc_info.value.code == "provider_unavailable"


@pytest.mark.parametrize(
    "page",
    [
        {"values": "bad"},
        {"values": [[True]]},
        {"values": [[1]]},
        {"values": [["x"], "bad-row"]},
        {"values": [["x"]], "majorDimension": "COLUMNS"},
        {"values": [["x"], [{}]]},
    ],
)
def test_read_rejects_non_formatted_or_malformed_provider_values(monkeypatch, page) -> None:
    from sheets import reads
    from sheets.errors import SheetsExtensionError

    monkeypatch.setattr(reads, "dataset_grant_limit", lambda _payload: 1024)
    transport = SheetsTransport(
        _metadata(
            {"sheetId": 123, "title": "Data", "gridProperties": {"rowCount": 2, "columnCount": 2}}
        ),
        [page],
    )
    _raises_input_162_1 = _input()
    _raises_input_162_2 = _writer([])
    with pytest.raises(SheetsExtensionError) as exc_info:
        reads.read_google_sheet(
            transport, {}, _raises_input_162_1, dataset_writer=_raises_input_162_2
        )
    assert exc_info.value.code == "provider_unavailable"


def test_read_enforces_row_cell_and_grant_byte_limits_without_partial_success(monkeypatch) -> None:
    from sheets import reads
    from sheets.errors import SheetsExtensionError

    monkeypatch.setattr(reads, "MAX_ROWS", 2)
    monkeypatch.setattr(reads, "dataset_grant_limit", lambda _payload: 20)
    transport = SheetsTransport(
        _metadata(
            {"sheetId": 123, "title": "Data", "gridProperties": {"rowCount": 4, "columnCount": 2}}
        ),
        [{"values": [["h1", "h2"], ["a", "b"], ["c", "d"], ["e", "f"]]}],
    )
    _raises_input_179_1 = _input()
    _raises_input_179_2 = _writer([])
    with pytest.raises(SheetsExtensionError) as exc_info:
        reads.read_google_sheet(
            transport, {}, _raises_input_179_1, dataset_writer=_raises_input_179_2
        )
    assert exc_info.value.code in {"sheet_too_large", "dataset_output_unavailable"}


def test_sparse_huge_grid_is_rejected_before_values_request_fanout(monkeypatch) -> None:
    from sheets import reads
    from sheets.errors import SheetsExtensionError

    monkeypatch.setattr(reads, "dataset_grant_limit", lambda _payload: 1024 * 1024)
    transport = SheetsTransport(
        _metadata(
            {
                "sheetId": 123,
                "title": "Sparse",
                "gridProperties": {"rowCount": 2_000_000, "columnCount": 1},
            }
        ),
        [],
    )

    _raises_input_200_1 = _input()

    def _raises_input_200_2(*_args, **_kwargs):
        return (_ for _ in ()).throw(AssertionError("dataset writer must not run"))

    with pytest.raises(SheetsExtensionError) as exc_info:
        reads.read_google_sheet(
            transport, {}, _raises_input_200_1, dataset_writer=_raises_input_200_2
        )

    assert exc_info.value.code == "sheet_too_large"
    assert transport.calls == []


def test_maximum_sparse_span_uses_small_fixed_provider_request_budget(monkeypatch) -> None:
    from sheets import reads

    monkeypatch.setattr(reads, "dataset_grant_limit", lambda _payload: 1024 * 1024)
    transport = SheetsTransport(
        _metadata(
            {
                "sheetId": 123,
                "title": "Sparse",
                "gridProperties": {"rowCount": 100_001, "columnCount": 1},
            }
        ),
        [],
    )
    captured: list[bytes] = []

    result = reads.read_google_sheet(
        transport,
        {},
        _input(),
        dataset_writer=_writer(captured),
    )

    assert result["row_count"] == 0
    assert len(transport.calls) == reads.MAX_VALUES_GET_CALLS == 11
    assert transport.calls[0][1] == "'Sparse'!A1:A10000"
    assert transport.calls[-1][1] == "'Sparse'!A100001:A100001"


def test_preview_is_capped_and_full_rows_never_enter_result_json(monkeypatch) -> None:
    from sheets import reads

    monkeypatch.setattr(reads, "dataset_grant_limit", lambda _payload: 1024 * 1024)
    canary = "private-row-150"
    values = [["value"], *[[canary if index == 150 else f"row-{index}"] for index in range(160)]]
    transport = SheetsTransport(
        _metadata(
            {"sheetId": 123, "title": "Data", "gridProperties": {"rowCount": 161, "columnCount": 1}}
        ),
        [{"values": values}],
    )
    result = reads.read_google_sheet(transport, {}, _input(), dataset_writer=_writer([]))
    assert result["row_count"] == 160
    assert len(result["preview"]) == 100
    assert canary not in repr(result)


def test_sheets_transport_uses_fixed_v4_origin_projection_and_formatted_rows() -> None:
    from sheets.transport import SheetsTransport

    captured = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def opener(request, **kwargs):
        captured.append((request, kwargs))
        return Response(b'{"values":[["A"]]}')

    payload = SheetsTransport("secret-token", opener=opener).values_get(
        "spreadsheet-1", "'Data'!A1:C100"
    )

    assert payload == {"values": [["A"]]}
    request = captured[0][0]
    assert request.full_url == (
        "https://sheets.googleapis.com/v4/spreadsheets/spreadsheet-1/values/"
        "%27Data%27%21A1%3AC100?majorDimension=ROWS&valueRenderOption=FORMATTED_VALUE"
    )
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert captured[0][1]["purpose"] == "Google Sheets values read"


def test_sheets_ordinary_read_defers_retry_to_core_orchestrator() -> None:
    from sheets.errors import SheetsExtensionError
    from sheets.transport import SheetsTransport

    calls = 0
    waits = []

    def opener(request, **_kwargs):
        nonlocal calls
        calls += 1
        raise HTTPError(
            request.full_url,
            503,
            "private-canary",
            {"Retry-After": "7"},
            io.BytesIO(b"{}"),
        )

    _raises_callable_309_1 = SheetsTransport(
        "token", opener=opener, sleep=waits.append, jitter=lambda: 0.0
    ).values_get
    with pytest.raises(SheetsExtensionError) as exc_info:
        _raises_callable_309_1("spreadsheet-1", "'Data'!A1")

    assert exc_info.value.code == "provider_unavailable"
    assert calls == 1
    assert waits == []
    assert exc_info.value.retry_facts()["retry_after_seconds"] == 7.0
    assert exc_info.value.retry_facts()["retryable"] is True
    assert "canary" not in str(exc_info.value).lower()


def test_cross_project_dataset_grant_fails_before_sheets_network(monkeypatch) -> None:
    from sheets import reads
    from sheets.errors import SheetsExtensionError

    monkeypatch.setattr(reads, "verified_platform_grant_signature", lambda *_args: "sig")
    payload = {
        "host": {"scope": {"account_id": "account-a", "project_id": "project-a"}},
        "datasets": {
            "outputs": [
                {
                    "dataset_id": "out",
                    "dataset_handle": "dataset:out",
                    "role": "output",
                    "binding_key": "google_sheet_dataset",
                    "content_type": "application/x-ndjson",
                    "scope": {"account_id": "account-a", "project_id": "project-b"},
                    "access": {
                        "transport": "presigned_url",
                        "mode": "write",
                        "max_size_bytes": 1024,
                        "single_use": True,
                        "expires_at": "2999-01-01T00:00:00Z",
                        "platform_grant": {"version": 1, "signature": "sig"},
                    },
                }
            ]
        },
    }
    transport = SheetsTransport({}, [])

    _raises_input_352_1 = _input()
    with pytest.raises(SheetsExtensionError) as exc_info:
        reads.read_google_sheet(transport, payload, _raises_input_352_1)

    assert exc_info.value.code == "dataset_output_unavailable"
    assert transport.calls == []
