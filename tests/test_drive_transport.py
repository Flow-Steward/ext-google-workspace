from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from urllib.error import HTTPError

import pytest


def test_drive_retry_after_accepts_http_date_and_408() -> None:
    from drive.transport import _retry_after_seconds, _retryable_status

    value = format_datetime(datetime.now(UTC) + timedelta(minutes=10), usegmt=True)

    assert 590 <= (_retry_after_seconds({"Retry-After": value}) or 0) <= 600
    assert _retryable_status(408) is True


class _Response(io.BytesIO):
    def __init__(self, body=b"", *, headers=None, status=200):
        super().__init__(body)
        self.headers = headers or {}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_drive_transport_pins_origin_projection_and_disables_redirects_via_sdk() -> None:
    from drive.transport import DriveTransport

    captured = {}

    def opener(request, **kwargs):
        captured["request"] = request
        captured["kwargs"] = kwargs
        return _Response(b'{"files":[]}')

    payload = DriveTransport("access-token", opener=opener).files_list(
        {
            "q": "trashed = false",
            "pageSize": 50,
            "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,webViewLink)",
            "spaces": "drive",
            "corpora": "user",
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
        }
    )

    assert payload == {"files": []}
    request = captured["request"]
    assert request.full_url.startswith("https://www.googleapis.com/drive/v3/files?")
    assert "attacker" not in request.full_url
    assert request.headers["Authorization"] == "Bearer access-token"
    assert captured["kwargs"] == {
        "timeout_seconds": 15.0,
        "purpose": "Google Drive file search",
    }


def test_drive_transport_reads_in_bounded_chunks_before_json_decode() -> None:
    from drive.errors import DriveExtensionError
    from drive.transport import SEARCH_RESPONSE_LIMIT, DriveTransport

    response = _Response(b"{" + b"x" * SEARCH_RESPONSE_LIMIT + b"}")
    _raises_callable_72_1 = DriveTransport(
        "access-token", opener=lambda *_args, **_kwargs: response
    ).files_list
    with pytest.raises(DriveExtensionError) as exc_info:
        _raises_callable_72_1({})

    assert exc_info.value.code == "response_too_large"


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        (401, "authError", "google_reauthorization_required"),
        (403, "insufficientPermissions", "google_scope_required"),
        (403, "accessNotConfigured", "google_api_disabled"),
        (403, "domainPolicy", "google_workspace_admin_approval_required"),
        (403, "storageQuotaExceeded", "google_quota_exceeded"),
        (429, "rateLimitExceeded", "google_rate_limited"),
        (404, "notFound", "google_file_not_found"),
    ],
)
def test_drive_transport_maps_google_errors_without_raw_body(status, reason, expected) -> None:
    from drive.errors import DriveExtensionError
    from drive.transport import DriveTransport

    raw = json.dumps({"error": {"errors": [{"reason": reason, "message": "PII-canary"}]}}).encode()

    def opener(request, **_kwargs):
        raise HTTPError(request.full_url, status, "raw-canary", {}, io.BytesIO(raw))

    _raises_callable_99_1 = DriveTransport(
        "access-token", opener=opener, sleep=lambda _seconds: None
    ).files_list
    with pytest.raises(DriveExtensionError) as exc_info:
        _raises_callable_99_1({})

    assert exc_info.value.code == expected
    assert "canary" not in str(exc_info.value).lower()


def test_drive_ordinary_read_defers_retry_to_core_orchestrator() -> None:
    from drive.errors import DriveExtensionError
    from drive.transport import DriveTransport

    attempts = []
    waits = []

    def opener(request, **_kwargs):
        attempts.append(request.full_url)
        raise HTTPError(
            request.full_url,
            503,
            "unavailable",
            {"Retry-After": "7"},
            io.BytesIO(b"{}"),
        )

    _raises_callable_123_1 = DriveTransport(
        "access-token", opener=opener, sleep=waits.append, jitter=lambda: 0.0
    ).files_list
    with pytest.raises(DriveExtensionError) as exc_info:
        _raises_callable_123_1({})

    assert len(attempts) == 1
    assert waits == []
    assert exc_info.value.retry_facts() == {
        "failure_class": "provider",
        "retryable": True,
        "retry_after_seconds": 7.0,
        "definitely_no_external_effect": True,
        "external_effect_status": "failed",
        "provider_error_code": "provider_unavailable",
        "http_status": 503,
    }


def test_ambiguous_upload_reconciliation_retries_empty_marker_search_without_reupload() -> None:
    from drive.transport import DriveTransport

    attempts = []
    waits = []

    def opener(request, **_kwargs):
        attempts.append(request)
        if len(attempts) == 1:
            return _Response(b'{"files":[]}')
        if request.full_url.startswith("https://www.googleapis.com/drive/v3/files/file-1?"):
            if len(attempts) == 3:
                return _Response(b'{"id":"file-1","name":"stock.csv","mimeType":"text/csv"}')
            return _Response(
                b'{"id":"file-1","name":"stock.csv","mimeType":"text/csv","size":"7",'
                b'"webViewLink":"https://drive.google.com/file/d/file-1/view"}'
            )
        return _Response(
            b'{"files":[{"id":"file-1","name":"stock.csv","mimeType":"text/csv","size":"7"}]}'
        )

    result = DriveTransport(
        "token",
        opener=opener,
        sleep=waits.append,
        jitter=lambda: 0.0,
    ).reconcile_ambiguous_upload("effect-1", "folder-1", expected_size=7)

    assert result["id"] == "file-1"
    assert result["size"] == "7"
    assert len(attempts) == 5
    assert all(request.get_method() == "GET" for request in attempts)
    assert waits == [0.25, 0.5]


def test_file_metadata_and_download_use_fixed_drive_paths() -> None:
    from drive.transport import DriveTransport

    requests = []

    def opener(request, **kwargs):
        requests.append((request, kwargs))
        if "alt=media" in request.full_url:
            return _Response(b"one,two")
        return _Response(
            json.dumps(
                {
                    "id": "file-1",
                    "name": "report.csv",
                    "mimeType": "text/csv",
                    "size": "7",
                }
            ).encode()
        )

    transport = DriveTransport("secret-token", opener=opener)
    metadata = transport.file_metadata("file-1")
    body = b"".join(transport.stream_file_media("file-1", max_bytes=8))

    assert metadata["id"] == "file-1"
    assert body == b"one,two"
    assert requests[0][0].full_url == (
        "https://www.googleapis.com/drive/v3/files/file-1?"
        "fields=id%2Cname%2CmimeType%2Csize%2CmodifiedTime%2CwebViewLink&supportsAllDrives=true"
    )
    assert requests[1][0].full_url == (
        "https://www.googleapis.com/drive/v3/files/file-1?alt=media&supportsAllDrives=true"
    )
    assert all(row[0].get_header("Authorization") == "Bearer secret-token" for row in requests)


def test_export_path_is_fixed_and_stream_is_bounded() -> None:
    from drive.errors import DriveExtensionError
    from drive.transport import DriveTransport

    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        return _Response(b"123456")

    transport = DriveTransport("token", opener=opener)
    _raises_input_226_1 = transport.stream_file_export("file-1", "application/pdf", max_bytes=5)
    with pytest.raises(DriveExtensionError) as exc_info:
        b"".join(_raises_input_226_1)

    assert exc_info.value.code == "drive_file_too_large"
    assert requests[0].full_url == (
        "https://www.googleapis.com/drive/v3/files/file-1/export?mimeType=application%2Fpdf"
    )


def test_resumable_upload_chunks_large_stream_and_keeps_session_private() -> None:
    from drive.transport import UPLOAD_CHUNK_BYTES, DriveTransport

    session = "https://www.googleapis.com/upload/drive/v3/files?upload_id=private-session"
    requests = []
    size = (UPLOAD_CHUNK_BYTES * 2) + 17
    source_exhausted = False

    def chunks():
        nonlocal source_exhausted
        yield b"a" * (UPLOAD_CHUNK_BYTES - 3)
        yield b"b" * (UPLOAD_CHUNK_BYTES + 11)
        yield b"c" * 9
        source_exhausted = True

    def opener(request, **kwargs):
        requests.append((request, kwargs))
        if request.get_method() == "POST":
            return _Response(headers={"Location": session})
        if request.get_method() == "GET":
            return _Response(
                b'{"id":"file-1","name":"stock.csv","mimeType":"text/csv",'
                + f'"size":"{size}",'.encode()
                + b'"webViewLink":"https://drive.google.com/file/d/file-1/view"}'
            )
        assert request.full_url == session
        content_range = request.get_header("Content-range")
        if content_range == f"bytes 0-{UPLOAD_CHUNK_BYTES - 1}/{size}":
            assert request.data == (b"a" * (UPLOAD_CHUNK_BYTES - 3)) + b"bbb"
            raise HTTPError(
                request.full_url,
                308,
                "Resume Incomplete",
                {"Range": f"bytes=0-{UPLOAD_CHUNK_BYTES - 1}"},
                io.BytesIO(b""),
            )
        if content_range == (f"bytes {UPLOAD_CHUNK_BYTES}-{(UPLOAD_CHUNK_BYTES * 2) - 1}/{size}"):
            assert len(request.data) == UPLOAD_CHUNK_BYTES
            return _Response(
                headers={"Range": f"bytes=0-{(UPLOAD_CHUNK_BYTES * 2) - 1}"},
                status=308,
            )
        assert content_range == f"bytes {UPLOAD_CHUNK_BYTES * 2}-{size - 1}/{size}"
        assert source_exhausted is True
        assert request.data == (b"b" * 8) + (b"c" * 9)
        return _Response(
            b'{"id":"file-1","name":"stock.csv","mimeType":"text/csv",'
            + f'"size":"{size}",'.encode()
            + b'"webViewLink":"https://drive.google.com/file/d/file-1/view"}'
        )

    transport = DriveTransport("token", opener=opener)
    location = transport.start_resumable_upload(
        {
            "name": "stock.csv",
            "parents": ["folder-1"],
            "appProperties": {"flow_steward_operation_id": "effect-1"},
        },
        "text/csv",
        size,
    )
    result = transport.upload_resumable(location, chunks(), size=size, mime_type="text/csv")

    assert result["id"] == "file-1"
    assert len(requests) == 5
    assert requests[0][0].full_url.startswith(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"
    )
    assert requests[0][0].get_header("X-goog-upload-content-length") == str(size)
    upload_requests = [row[0] for row in requests if row[0].get_method() == "PUT"]
    assert all(request.get_header("Content-type") == "text/csv" for request in upload_requests)
    assert all(len(request.data) <= UPLOAD_CHUNK_BYTES for request in upload_requests)
    assert "private-session" not in repr(result)


def test_resumable_upload_accepts_google_generated_session_parameters() -> None:
    from drive.transport import DriveTransport

    session_credential = "test-" + ("x" * 78)
    session = (
        "https://www.googleapis.com/upload/drive/v3/files?"
        "fields=id%2Cname%2CmimeType%2Csize%2CwebViewLink"
        f"&session_crd={session_credential}"
        "&uploadType=resumable"
        "&upload_id=fake-session"
    )
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        if request.get_method() == "POST":
            return _Response(headers={"Location": session})
        if request.get_method() == "GET":
            return _Response(
                b'{"id":"file-1","name":"stock.csv","mimeType":"text/csv",'
                b'"size":"1","webViewLink":"https://drive.google.com/file/d/file-1/view"}'
            )
        assert request.full_url == session
        assert isinstance(request.data, bytes)
        assert request.data == b"x"
        return _Response(
            b'{"id":"file-1","name":"stock.csv","mimeType":"text/csv",'
            b'"size":"1","webViewLink":"https://drive.google.com/file/d/file-1/view"}'
        )

    transport = DriveTransport("token", opener=opener)
    location = transport.start_resumable_upload(
        {
            "name": "stock.csv",
            "parents": ["folder-1"],
            "appProperties": {"flow_steward_operation_id": "effect-1"},
        },
        "text/csv",
        1,
    )
    result = transport.upload_resumable(
        location,
        iter([b"x"]),
        size=1,
        mime_type="text/csv",
    )

    assert result["id"] == "file-1"
    assert len(requests) == 3


def test_resumable_upload_refreshes_partial_final_metadata_from_fixed_drive_path() -> None:
    from drive.transport import DriveTransport

    session = "https://www.googleapis.com/upload/drive/v3/files?upload_id=private-session"
    requests = []

    def opener(request, **kwargs):
        requests.append((request, kwargs))
        if request.get_method() == "PUT":
            assert request.full_url == session
            return _Response(
                b'{"kind":"drive#file","id":"file-1","name":"stock.csv","mimeType":"text/csv"}'
            )
        assert request.get_method() == "GET"
        return _Response(
            b'{"id":"file-1","name":"stock.csv","mimeType":"text/csv",'
            b'"size":"1","modifiedTime":"2026-09-08T12:00:00.000Z",'
            b'"webViewLink":"https://drive.google.com/file/d/file-1/view"}'
        )

    result = DriveTransport("token", opener=opener).upload_resumable(
        session,
        iter([b"x"]),
        size=1,
        mime_type="application/octet-stream",
    )

    assert result == {
        "id": "file-1",
        "name": "stock.csv",
        "mimeType": "text/csv",
        "size": "1",
        "webViewLink": "https://drive.google.com/file/d/file-1/view",
    }
    assert len(requests) == 2
    assert requests[1][0].full_url == (
        "https://www.googleapis.com/drive/v3/files/file-1?"
        "fields=id%2Cname%2CmimeType%2Csize%2CmodifiedTime%2CwebViewLink&supportsAllDrives=true"
    )
    assert requests[1][1]["purpose"] == "Google Drive file metadata"
    assert "private-session" not in repr(result)


def test_resumable_upload_metadata_refresh_failure_remains_ambiguous() -> None:
    from drive.errors import DriveExtensionError
    from drive.transport import DriveTransport

    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        if request.get_method() == "PUT":
            return _Response(b'{"id":"file-1"}')
        raise HTTPError(
            request.full_url,
            503,
            "private provider detail",
            {},
            io.BytesIO(b'{"error":"PII-canary"}'),
        )

    _raises_callable_422_1 = DriveTransport(
        "token", opener=opener, sleep=lambda _seconds: None, jitter=lambda: 0.0
    ).upload_resumable
    _raises_input_422_2 = iter([b"x"])
    with pytest.raises(DriveExtensionError) as exc_info:
        _raises_callable_422_1(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=private-session",
            _raises_input_422_2,
            size=1,
            mime_type="text/csv",
        )

    assert exc_info.value.code == "timeout_unknown"
    assert "canary" not in str(exc_info.value).lower()
    assert "private-session" not in str(exc_info.value)
    assert len(requests) == 4


@pytest.mark.parametrize(
    "web_view_link",
    ["", "http://drive.google.com/file/d/file-1/view", "https://example.test/file-1"],
)
def test_resumable_upload_rejects_untrusted_web_view_link_as_ambiguous(
    web_view_link: str,
) -> None:
    from drive.errors import DriveExtensionError
    from drive.transport import DriveTransport

    def opener(request, **_kwargs):
        if request.get_method() == "PUT":
            return _Response(b'{"id":"file-1"}')
        return _Response(
            json.dumps(
                {
                    "id": "file-1",
                    "name": "stock.csv",
                    "mimeType": "text/csv",
                    "size": "1",
                    "webViewLink": web_view_link,
                }
            ).encode()
        )

    _raises_callable_466_1 = DriveTransport("token", opener=opener).upload_resumable
    _raises_input_466_2 = iter([b"x"])
    with pytest.raises(DriveExtensionError) as exc_info:
        _raises_callable_466_1(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=private-session",
            _raises_input_466_2,
            size=1,
            mime_type="text/csv",
        )

    assert exc_info.value.code == "timeout_unknown"


@pytest.mark.parametrize(
    "location",
    [
        "http://www.googleapis.com/upload/drive/v3/files?upload_id=x",
        "https://evil.example/upload/drive/v3/files?upload_id=x",
        "https://www.googleapis.com:8443/upload/drive/v3/files?upload_id=x",
        "https://attacker@www.googleapis.com/upload/drive/v3/files?upload_id=x",
        "https://www.googleapis.com/drive/v3/files?upload_id=x",
        "https://www.googleapis.com/upload/drive/v3/files;unexpected?upload_id=x",
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=x#fragment",
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=x#",
        "https://www.googleapis.com/upload/drive/v3/files?redirect=https://evil.example",
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=x&fields=id%2Cname%2Cowners",
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=x&session_crd=",
        (
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=x"
            "&session_crd=one&session_crd=two"
        ),
    ],
)
def test_resumable_upload_rejects_untrusted_provider_session_url(location) -> None:
    from drive.errors import DriveExtensionError
    from drive.transport import DriveTransport

    transport = DriveTransport(
        "token", opener=lambda *_args, **_kwargs: _Response(headers={"Location": location})
    )
    with pytest.raises(DriveExtensionError) as exc_info:
        transport.start_resumable_upload(
            {
                "name": "a.csv",
                "parents": ["folder-1"],
                "appProperties": {"flow_steward_operation_id": "effect-1"},
            },
            "text/csv",
            1,
        )
    assert exc_info.value.code == "provider_unavailable"


def test_mutation_timeout_uses_status_queries_and_never_blindly_replays() -> None:
    from drive.errors import DriveExtensionError
    from drive.transport import DriveTransport

    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        raise TimeoutError("private provider detail")

    transport = DriveTransport("token", opener=opener)
    _raises_input_528_1 = iter([b"x"])
    with pytest.raises(DriveExtensionError) as exc_info:
        transport.upload_resumable(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=private",
            _raises_input_528_1,
            size=1,
            mime_type="text/plain",
        )
    assert exc_info.value.code == "timeout_unknown"
    assert exc_info.value.retry_facts() == {
        "provider_error_code": "timeout_unknown",
        "failure_class": "transient",
        "retryable": False,
        "definitely_no_external_effect": False,
        "external_effect_status": "timeout_unknown",
    }
    assert len(requests) == 3
    assert requests[0].data == b"x"
    assert requests[0].get_header("Content-range") == "bytes 0-0/1"
    assert all(request.data == b"" for request in requests[1:])
    assert all(request.get_header("Content-range") == "bytes */1" for request in requests[1:])
    assert "private" not in exc_info.value.message


def test_resumable_upload_recovers_confirmed_partial_chunk_after_timeout() -> None:
    from drive.transport import DriveTransport

    session = "https://www.googleapis.com/upload/drive/v3/files?upload_id=private"
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        if request.get_method() == "GET":
            return _Response(b'{"id":"file-1","name":"abcdef","mimeType":"text/plain","size":"6"}')
        if len(requests) == 1:
            raise TimeoutError("ambiguous")
        if len(requests) == 2:
            assert request.data == b""
            assert request.get_header("Content-range") == "bytes */6"
            return _Response(headers={"Range": "bytes=0-2"}, status=308)
        assert request.data == b"def"
        assert request.get_header("Content-range") == "bytes 3-5/6"
        return _Response(b'{"id":"file-1","name":"abcdef","size":"6"}')

    result = DriveTransport("token", opener=opener, sleep=lambda _seconds: None).upload_resumable(
        session, iter([b"abcdef"]), size=6, mime_type="text/plain"
    )

    assert result["id"] == "file-1"
    assert len(requests) == 4


def test_resumable_upload_rejects_malformed_provider_range() -> None:
    from drive.errors import DriveExtensionError
    from drive.transport import DriveTransport

    def opener(_request, **_kwargs):
        return _Response(headers={"Range": "bytes=4-5"}, status=308)

    _raises_callable_586_1 = DriveTransport("token", opener=opener).upload_resumable
    _raises_input_586_2 = iter([b"abcdef"])
    with pytest.raises(DriveExtensionError) as exc_info:
        _raises_callable_586_1(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=private",
            _raises_input_586_2,
            size=6,
            mime_type="text/plain",
        )

    assert exc_info.value.code == "provider_unavailable"


def test_resumable_upload_rejects_regressing_provider_range() -> None:
    from drive.errors import DriveExtensionError
    from drive.transport import DriveTransport

    responses = iter(
        [
            _Response(headers={"Range": "bytes=0-2"}, status=308),
            _Response(headers={"Range": "bytes=0-1"}, status=308),
        ]
    )
    _raises_callable_607_1 = DriveTransport(
        "token", opener=lambda *_args, **_kwargs: next(responses)
    ).upload_resumable
    _raises_input_607_2 = iter([b"abcdef"])
    with pytest.raises(DriveExtensionError) as exc_info:
        _raises_callable_607_1(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=private",
            _raises_input_607_2,
            size=6,
            mime_type="text/plain",
        )

    assert exc_info.value.code == "provider_unavailable"


def test_invalid_status_probe_after_timeout_remains_ambiguous() -> None:
    from drive.errors import DriveExtensionError
    from drive.transport import DriveTransport

    calls = 0

    def opener(_request, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("ambiguous")
        return _Response(headers={"Range": "invalid"}, status=308)

    _raises_callable_631_1 = DriveTransport(
        "token", opener=opener, sleep=lambda _seconds: None
    ).upload_resumable
    _raises_input_631_2 = iter([b"abcdef"])
    with pytest.raises(DriveExtensionError) as exc_info:
        _raises_callable_631_1(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=private",
            _raises_input_631_2,
            size=6,
            mime_type="text/plain",
        )

    assert exc_info.value.code == "timeout_unknown"


def test_resumable_upload_rejects_extra_source_bytes_before_final_request() -> None:
    from drive.errors import DriveExtensionError
    from drive.transport import DriveTransport

    requests = []
    _raises_callable_647_1 = DriveTransport(
        "token", opener=lambda request, **_kwargs: requests.append(request)
    ).upload_resumable
    _raises_input_647_2 = iter([b"abcdef", b"extra"])
    with pytest.raises(DriveExtensionError) as exc_info:
        _raises_callable_647_1(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=private",
            _raises_input_647_2,
            size=6,
            mime_type="text/plain",
        )

    assert exc_info.value.code == "artifact_input_unavailable"
    assert requests == []


def test_upload_operation_deadline_bounds_requests_and_stops_before_next_chunk() -> None:
    from drive.errors import DriveExtensionError
    from drive.transport import (
        DEFAULT_UPLOAD_OPERATION_TIMEOUT_SECONDS,
        UPLOAD_CHUNK_BYTES,
        DriveTransport,
    )

    now = [100.0]
    requests = []

    def opener(request, **kwargs):
        requests.append((request, kwargs))
        now[0] += 30.0
        return _Response(headers={"Range": f"bytes=0-{UPLOAD_CHUNK_BYTES - 1}"}, status=308)

    transport = DriveTransport(
        "token",
        opener=opener,
        timeout_seconds=60,
        operation_timeout_seconds=30,
        monotonic=lambda: now[0],
    )
    _raises_input_684_1 = iter([b"a" * UPLOAD_CHUNK_BYTES, b"b"])
    with pytest.raises(DriveExtensionError) as exc_info:
        transport.upload_resumable(
            "https://www.googleapis.com/upload/drive/v3/files?upload_id=private",
            _raises_input_684_1,
            size=UPLOAD_CHUNK_BYTES + 1,
            mime_type="text/plain",
        )

    assert DEFAULT_UPLOAD_OPERATION_TIMEOUT_SECONDS == 300
    assert exc_info.value.code == "network_failure"
    assert len(requests) == 1
    assert requests[0][1]["timeout_seconds"] == 30.0


def test_upload_deadline_covers_reconciliation_and_session_initiation() -> None:
    from drive.errors import DriveExtensionError
    from drive.transport import DriveTransport

    now = [100.0]
    requests = []

    def opener(request, **kwargs):
        requests.append((request, kwargs))
        now[0] += 30.0
        return _Response(b'{"files":[]}')

    transport = DriveTransport(
        "token",
        opener=opener,
        timeout_seconds=30,
        operation_timeout_seconds=30,
        monotonic=lambda: now[0],
    )
    transport.begin_upload_operation()
    assert transport.find_by_operation_marker("effect-1", "folder-1") is None

    with pytest.raises(DriveExtensionError) as exc_info:
        transport.start_resumable_upload(
            {
                "name": "stock.csv",
                "parents": ["folder-1"],
                "appProperties": {"flow_steward_operation_id": "effect-1"},
            },
            "text/csv",
            1,
        )

    assert exc_info.value.code == "network_failure"
    assert len(requests) == 1


def test_upload_deadline_bounds_reconciliation_retry_sleep() -> None:
    from drive.errors import DriveExtensionError
    from drive.transport import DriveTransport

    now = [100.0]
    requests = []
    waits = []

    def opener(request, **_kwargs):
        requests.append(request)
        now[0] += 29.8
        raise TimeoutError("request timed out")

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    transport = DriveTransport(
        "token",
        opener=opener,
        timeout_seconds=30,
        operation_timeout_seconds=30,
        monotonic=lambda: now[0],
        sleep=sleep,
        jitter=lambda: 1.0,
    )
    transport.begin_upload_operation()

    with pytest.raises(DriveExtensionError) as exc_info:
        transport.find_by_operation_marker("effect-1", "folder-1")

    assert exc_info.value.code == "network_failure"
    assert len(requests) == 1
    assert waits == pytest.approx([0.2])


def test_zero_byte_resumable_upload_finalizes_with_empty_bounded_put() -> None:
    from drive.transport import DriveTransport

    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        if request.get_method() == "GET":
            return _Response(
                b'{"id":"empty-1","name":"empty.csv","mimeType":"text/csv","size":"0"}'
            )
        assert request.data == b""
        assert request.get_header("Content-length") == "0"
        assert request.get_header("Content-range") == "bytes */0"
        return _Response(b'{"id":"empty-1","name":"empty.csv","size":"0"}', status=201)

    result = DriveTransport("token", opener=opener).upload_resumable(
        "https://www.googleapis.com/upload/drive/v3/files?upload_id=private",
        iter([]),
        size=0,
        mime_type="text/csv",
    )

    assert result["id"] == "empty-1"
    assert len(requests) == 2


def test_create_google_spreadsheet_uses_fixed_drive_origin_and_private_marker() -> None:
    from drive.transport import DriveTransport

    captured = {}

    def opener(request, **kwargs):
        captured["request"] = request
        captured["kwargs"] = kwargs
        return _Response(
            b'{"id":"sheet-1","name":"Stock","mimeType":"application/vnd.google-apps.spreadsheet"}'
        )

    result = DriveTransport("token", opener=opener).create_google_spreadsheet(
        {
            "name": "Stock",
            "parents": ["folder-1"],
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "appProperties": {"flow_steward_operation_id": "effect-1"},
        }
    )

    request = captured["request"]
    assert request.get_method() == "POST"
    assert request.full_url == (
        "https://www.googleapis.com/drive/v3/files?"
        "fields=id%2Cname%2CmimeType%2CwebViewLink&supportsAllDrives=true"
    )
    assert json.loads(request.data) == {
        "name": "Stock",
        "parents": ["folder-1"],
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "appProperties": {"flow_steward_operation_id": "effect-1"},
    }
    assert result["id"] == "sheet-1"
