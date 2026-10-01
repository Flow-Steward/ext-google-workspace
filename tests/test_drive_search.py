from __future__ import annotations

import pytest


def _input(**overrides):
    value = {
        "connection_ref": "connection-1",
        "parent_folder_id": "folder'\\one",
        "name": "Supplier's \\ stock",
        "name_match": "contains",
        "mime_types": ["text/csv", "application/vnd.ms-excel"],
        "modified_after": "2026-08-01T00:00:00Z",
        "modified_before": "2026-08-26T00:00:00Z",
        "trashed": False,
        "sort": "modified_time_desc",
        "limit": 2,
    }
    value.update(overrides)
    return value


class _Transport:
    def __init__(self, payload):
        self.payload = payload
        self.queries = []

    def files_list(self, query):
        self.queries.append(query)
        return self.payload


def test_search_drive_files_compiles_one_closed_request_and_preserves_provider_order() -> None:
    from drive.search import search_drive_files

    transport = _Transport(
        {
            "files": [
                {
                    "id": "file-2",
                    "name": "Second.csv",
                    "mimeType": "text/csv",
                    "size": "42",
                    "modifiedTime": "2026-08-25T12:30:00.123Z",
                    "webViewLink": "https://drive.google.com/file/d/file-2/view",
                },
                {
                    "id": "folder-1",
                    "name": "Folder",
                    "mimeType": "application/vnd.google-apps.folder",
                },
            ],
            "nextPageToken": "provider-page-2",
        }
    )

    result = search_drive_files(transport, _input())

    assert len(transport.queries) == 1
    query = transport.queries[0]
    assert query["pageSize"] == 2
    assert query["orderBy"] == "modifiedTime desc"
    assert query["spaces"] == "drive"
    assert query["corpora"] == "user"
    assert query["includeItemsFromAllDrives"] == "true"
    assert query["supportsAllDrives"] == "true"
    assert query["fields"] == (
        "nextPageToken,files(id,name,mimeType,size,modifiedTime,webViewLink)"
    )
    assert "folder\\'\\\\one" in query["q"]
    assert "Supplier\\'s \\\\ stock" in query["q"]
    assert result["files"][0]["file_id"] == "file-2"
    assert result["files"][0]["size"] == 42
    assert result["files"][1]["is_folder"] is True
    assert result["next_cursor"]


def test_drive_cursor_is_filter_bound_and_never_exposes_provider_token() -> None:
    from drive.errors import DriveExtensionError
    from drive.search import decode_cursor, search_drive_files

    first = search_drive_files(
        _Transport({"files": [], "nextPageToken": "private-provider-token"}),
        _input(),
    )
    cursor = first["next_cursor"]
    assert "private-provider-token" not in cursor
    assert decode_cursor(cursor, normalized_input=_input()) == "private-provider-token"

    _raises_input_90_1 = _input(name="Different")
    with pytest.raises(DriveExtensionError) as exc_info:
        decode_cursor(cursor, normalized_input=_raises_input_90_1)
    assert exc_info.value.code == "invalid_payload"


@pytest.mark.parametrize(
    "payload",
    [
        {"files": "not-a-list"},
        {"files": [{}]},
        {"files": [{"id": 7, "name": "Name", "mimeType": "text/csv"}]},
        {"files": [{"id": "id", "name": "Name", "mimeType": True}]},
        {"files": [{"id": "id", "name": "Name", "mimeType": "text/*"}]},
        {"files": [{"id": "id", "name": "Name", "mimeType": "text/csv", "size": True}]},
        {"files": [{"id": "id", "name": "Name", "mimeType": "text/csv", "size": "-1"}]},
        {"files": [{"id": "id", "name": "Name", "mimeType": "text/csv", "modifiedTime": "today"}]},
        {"files": [], "nextPageToken": 42},
        {"files": [], "nextPageToken": "x" * 4097},
    ],
)
def test_search_drive_files_rejects_malformed_provider_shapes(payload) -> None:
    from drive.errors import DriveExtensionError
    from drive.search import search_drive_files

    _raises_input_114_1 = _Transport(payload)
    _raises_input_114_2 = _input()
    with pytest.raises(DriveExtensionError) as exc_info:
        search_drive_files(_raises_input_114_1, _raises_input_114_2)

    assert exc_info.value.code in {"provider_unavailable", "response_too_large"}
