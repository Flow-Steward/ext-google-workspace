from __future__ import annotations

import json
import math
import random
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlsplit
from urllib.request import Request

from flowsteward_extension_sdk import PinnedPeerError, open_pinned_url, parse_retry_after

from .config import (
    DEFAULT_UPLOAD_OPERATION_TIMEOUT_SECONDS,
    MAX_UPLOAD_OPERATION_TIMEOUT_SECONDS,
    MIN_UPLOAD_OPERATION_TIMEOUT_SECONDS,
)
from .errors import DriveExtensionError
from .validation import provider_web_view_link

_MESSAGE_GOOGLE_DRIVE_UPLOAD_OUTCOME_IS_UNKNOWN = "Google Drive upload outcome is unknown"
_TOKEN_WWW_GOOGLEAPIS_COM = "www.googleapis.com"
_TOKEN_APPLICATION_JSON = "application/json"
_MESSAGE_GOOGLE_DRIVE_UPLOAD_EXCEEDED_ITS_OPERATION_DEADLINE = (
    "Google Drive upload exceeded its operation deadline"
)
_MESSAGE_GOOGLE_DRIVE_IS_TEMPORARILY_UNAVAILABLE = "Google Drive is temporarily unavailable"

DRIVE_API_ORIGIN = "https://www.googleapis.com"
DRIVE_API_PREFIX = "/drive/v3"
SEARCH_RESPONSE_LIMIT = 2 * 1024 * 1024
METADATA_RESPONSE_LIMIT = 256 * 1024
UPLOAD_RESPONSE_LIMIT = 256 * 1024
ERROR_RESPONSE_LIMIT = 64 * 1024
CHUNK_BYTES = 64 * 1024
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
UPLOAD_ATTEMPTS = 3
AMBIGUOUS_RECONCILIATION_ATTEMPTS = 8
MAX_REQUEST_TIMEOUT_SECONDS = 30
_FILES_LIST_QUERY_FIELDS = frozenset(
    {
        "q",
        "pageSize",
        "pageToken",
        "orderBy",
        "fields",
        "spaces",
        "corpora",
        "includeItemsFromAllDrives",
        "supportsAllDrives",
    }
)
_RESUMABLE_QUERY_FIELDS = frozenset({"fields", "session_crd", "uploadType", "upload_id"})
_RESUMABLE_RESPONSE_FIELDS = "id,name,mimeType,size,webViewLink"
_RESUMABLE_SESSION_VALUE_LIMIT = 4096
_RESUMABLE_RANGE_RE = re.compile(r"bytes=0-(0|[1-9](?a:\d)*)\Z")
_MIME_TYPE_RE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")


class _AmbiguousUploadError(Exception):
    pass


class _OperationDeadlineError(Exception):
    pass


class DriveTransport:
    def __init__(
        self,
        access_token: str,
        *,
        opener: Callable[..., Any] = open_pinned_url,
        timeout_seconds: float = 15.0,
        operation_timeout_seconds: float = DEFAULT_UPLOAD_OPERATION_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        token = str(access_token or "").strip()
        if not token:
            raise DriveExtensionError(
                "google_reauthorization_required", "Reconnect this Google account"
            )
        self._access_token = token
        self._opener = opener
        self._timeout_seconds = _bounded_timeout(
            timeout_seconds, default=15.0, minimum=1.0, maximum=MAX_REQUEST_TIMEOUT_SECONDS
        )
        self._operation_timeout_seconds = _bounded_timeout(
            operation_timeout_seconds,
            default=DEFAULT_UPLOAD_OPERATION_TIMEOUT_SECONDS,
            minimum=MIN_UPLOAD_OPERATION_TIMEOUT_SECONDS,
            maximum=MAX_UPLOAD_OPERATION_TIMEOUT_SECONDS,
        )
        self._sleep = sleep
        self._jitter = jitter
        self._monotonic = monotonic
        self._upload_deadline: float | None = None

    def begin_upload_operation(self) -> None:
        self._upload_deadline = self._monotonic() + self._operation_timeout_seconds

    def files_list(self, query: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(query, Mapping) or set(query) - _FILES_LIST_QUERY_FIELDS:
            raise DriveExtensionError("invalid_payload", "Invalid Drive files.list query")
        return self._request_json(
            "/files",
            query=query,
            response_limit=SEARCH_RESPONSE_LIMIT,
            purpose="Google Drive file search",
        )

    def file_metadata(self, file_id: str) -> dict[str, Any]:
        safe_id = _resource_id(file_id)
        return self._request_json(
            f"/files/{quote(safe_id, safe='')}",
            query={
                "fields": "id,name,mimeType,size,modifiedTime,webViewLink",
                "supportsAllDrives": "true",
            },
            response_limit=METADATA_RESPONSE_LIMIT,
            purpose="Google Drive file metadata",
        )

    def stream_file_media(self, file_id: str, *, max_bytes: int) -> Iterator[bytes]:
        safe_id = _resource_id(file_id)
        yield from self._stream_get(
            f"/files/{quote(safe_id, safe='')}",
            query={"alt": "media", "supportsAllDrives": "true"},
            max_bytes=max_bytes,
            purpose="Google Drive file download",
        )

    def stream_file_export(
        self, file_id: str, mime_type: str, *, max_bytes: int
    ) -> Iterator[bytes]:
        safe_id = _resource_id(file_id)
        safe_mime = str(mime_type or "").strip()
        if not safe_mime or len(safe_mime) > 255:
            raise DriveExtensionError("invalid_payload", "Invalid Drive export MIME type")
        yield from self._stream_get(
            f"/files/{quote(safe_id, safe='')}/export",
            query={"mimeType": safe_mime},
            max_bytes=max_bytes,
            purpose="Google Drive file export",
        )

    def find_by_operation_marker(self, operation_id: str, folder_id: str) -> dict[str, Any] | None:
        marker = _drive_query_literal(operation_id)
        folder = _drive_query_literal(_resource_id(folder_id))
        payload = self.files_list(
            {
                "q": (
                    "appProperties has { key='flow_steward_operation_id' "
                    f"and value='{marker}' }} and '{folder}' in parents and trashed = false"
                ),
                "pageSize": 2,
                "fields": "files(id,name,mimeType,size,webViewLink)",
                "spaces": "drive",
                "corpora": "user",
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
            }
        )
        rows = payload.get("files")
        if rows is None:
            return None
        if (
            not isinstance(rows, list)
            or len(rows) > 1
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise DriveExtensionError(
                "provider_unavailable", "Google Drive returned invalid reconciliation metadata"
            )
        return rows[0] if rows else None

    def reconcile_ambiguous_upload(
        self,
        operation_id: str,
        folder_id: str,
        *,
        expected_size: int,
    ) -> dict[str, Any] | None:
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
        ):
            raise DriveExtensionError("invalid_payload", "Invalid Drive upload size")
        for attempt in range(AMBIGUOUS_RECONCILIATION_ATTEMPTS):
            result = self.find_by_operation_marker(operation_id, folder_id)
            if result is not None:
                try:
                    file_id = _resource_id(result.get("id"))
                    metadata = self.file_metadata(file_id)
                    return _validated_completed_upload_metadata(
                        metadata,
                        file_id=file_id,
                        total_size=expected_size,
                    )
                except DriveExtensionError as exc:
                    if exc.code not in {"google_file_not_found", "timeout_unknown"}:
                        raise
            if attempt < AMBIGUOUS_RECONCILIATION_ATTEMPTS - 1:
                self._sleep_for_request_retry(_retry_wait(attempt, None, self._jitter()))
        return None

    def start_resumable_upload(self, metadata: Mapping[str, Any], mime_type: str, size: int) -> str:
        if not _valid_upload_metadata(metadata):
            raise DriveExtensionError("invalid_payload", "Invalid Drive upload metadata")
        if (
            not isinstance(mime_type, str)
            or not mime_type
            or len(mime_type) > 255
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise DriveExtensionError("invalid_payload", "Invalid Drive upload grant metadata")
        body = json.dumps(metadata, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{DRIVE_API_ORIGIN}/upload{DRIVE_API_PREFIX}/files?"
            "uploadType=resumable&fields=id%2Cname%2CmimeType%2Csize%2CwebViewLink",
            method="POST",
            data=body,
            headers={
                "Accept": _TOKEN_APPLICATION_JSON,
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json; charset=utf-8",
                "X-Goog-Upload-Content-Length": str(size),
                "X-Goog-Upload-Content-Type": mime_type,
            },
        )
        try:
            timeout_seconds = self._request_timeout_for_upload_operation()
            with self._opener(
                request,
                timeout_seconds=timeout_seconds,
                purpose="Google Drive resumable upload initiation",
            ) as response:
                location = str(response.headers.get("Location") or "").strip()
        except _OperationDeadlineError as exc:
            raise DriveExtensionError(
                "network_failure", _MESSAGE_GOOGLE_DRIVE_UPLOAD_EXCEEDED_ITS_OPERATION_DEADLINE
            ) from exc
        except HTTPError as exc:
            _raise_mutation_http_error(exc)
        except (PinnedPeerError, OSError) as exc:
            raise DriveExtensionError(
                "timeout_unknown", "Google Drive upload initiation outcome is unknown"
            ) from exc
        _validate_resumable_url(location)
        return location

    def create_google_spreadsheet(self, metadata: Mapping[str, Any]) -> dict[str, Any]:
        if not _valid_google_spreadsheet_metadata(metadata):
            raise DriveExtensionError("invalid_payload", "Invalid Google spreadsheet metadata")
        url = (
            f"{DRIVE_API_ORIGIN}{DRIVE_API_PREFIX}/files?"
            "fields=id%2Cname%2CmimeType%2CwebViewLink&supportsAllDrives=true"
        )
        body = json.dumps(metadata, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            method="POST",
            data=body,
            headers={
                "Accept": _TOKEN_APPLICATION_JSON,
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with self._opener(
                request,
                timeout_seconds=self._timeout_seconds,
                purpose="Google Drive spreadsheet creation",
            ) as response:
                raw = _read_bounded(response, METADATA_RESPONSE_LIMIT)
        except HTTPError as exc:
            _raise_mutation_http_error(exc)
        except (PinnedPeerError, OSError) as exc:
            raise DriveExtensionError(
                "timeout_unknown", "Google spreadsheet creation outcome is unknown"
            ) from exc
        return _decode_json_object(raw, purpose="spreadsheet creation")

    def upload_resumable(
        self,
        session_url: str,
        chunks: Iterable[bytes],
        *,
        size: int,
        mime_type: str,
    ) -> dict[str, Any]:
        _validate_resumable_url(session_url)
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(mime_type, str)
            or not mime_type
            or len(mime_type) > 255
        ):
            raise DriveExtensionError("invalid_payload", "Invalid Drive upload grant metadata")
        if self._upload_deadline is None:
            self.begin_upload_operation()
        deadline = float(self._upload_deadline)
        try:
            upload_chunks = _iter_exact_upload_chunks(
                chunks,
                size=size,
                check_deadline=lambda: self._remaining_upload_seconds(deadline),
            )
            for offset, chunk, final in upload_chunks:
                result = self._upload_chunk(
                    session_url,
                    chunk,
                    offset=offset,
                    total_size=size,
                    mime_type=mime_type,
                    final=final,
                    deadline=deadline,
                )
                if result is not None:
                    return result
        except _OperationDeadlineError as exc:
            raise DriveExtensionError(
                "network_failure", _MESSAGE_GOOGLE_DRIVE_UPLOAD_EXCEEDED_ITS_OPERATION_DEADLINE
            ) from exc
        raise DriveExtensionError(
            "provider_unavailable", "Google Drive did not finalize the upload"
        )

    def _upload_chunk(
        self,
        session_url: str,
        chunk: bytes,
        *,
        offset: int,
        total_size: int,
        mime_type: str,
        final: bool,
        deadline: float,
    ) -> dict[str, Any] | None:
        end = offset + len(chunk) - 1
        next_offset = offset
        uncertain = False
        saw_ambiguous_failure = False

        for attempt in range(UPLOAD_ATTEMPTS):
            try:
                remaining_seconds = self._remaining_upload_seconds(deadline)
            except _OperationDeadlineError as exc:
                if saw_ambiguous_failure or uncertain:
                    raise DriveExtensionError(
                        "timeout_unknown", _MESSAGE_GOOGLE_DRIVE_UPLOAD_OUTCOME_IS_UNKNOWN
                    ) from exc
                raise
            status_query = uncertain
            if status_query:
                body = b""
                content_range = f"bytes */{total_size}"
            else:
                body = chunk[next_offset - offset :]
                content_range = (
                    f"bytes {next_offset}-{end}/{total_size}" if body else f"bytes */{total_size}"
                )
            request = Request(
                session_url,
                method="PUT",
                data=body,
                headers={
                    "Accept": _TOKEN_APPLICATION_JSON,
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Length": str(len(body)),
                    "Content-Range": content_range,
                    "Content-Type": mime_type,
                },
            )
            try:
                status, headers, raw = self._open_upload_request(
                    request, timeout_seconds=min(self._timeout_seconds, remaining_seconds)
                )
            except _AmbiguousUploadError:
                saw_ambiguous_failure = True
                uncertain = True
                if attempt < UPLOAD_ATTEMPTS - 1:
                    try:
                        remaining_seconds = self._remaining_upload_seconds(deadline)
                    except _OperationDeadlineError:
                        continue
                    self._sleep(
                        min(
                            _retry_wait(attempt, None, self._jitter()),
                            remaining_seconds,
                        )
                    )
                continue
            except DriveExtensionError as exc:
                if status_query and saw_ambiguous_failure:
                    raise DriveExtensionError(
                        "timeout_unknown", _MESSAGE_GOOGLE_DRIVE_UPLOAD_OUTCOME_IS_UNKNOWN
                    ) from exc
                raise

            if status in {200, 201}:
                if not final:
                    raise DriveExtensionError(
                        "provider_unavailable",
                        "Google Drive finalized an incomplete upload",
                    )
                return self._refresh_completed_upload_metadata(
                    raw,
                    total_size=total_size,
                )
            if status != 308:
                raise DriveExtensionError(
                    "provider_unavailable", "Google Drive returned an invalid upload response"
                )

            try:
                acknowledged = _resumable_acknowledged_offset(headers)
            except DriveExtensionError as exc:
                if status_query and saw_ambiguous_failure:
                    raise DriveExtensionError(
                        "timeout_unknown", _MESSAGE_GOOGLE_DRIVE_UPLOAD_OUTCOME_IS_UNKNOWN
                    ) from exc
                raise
            if acknowledged < next_offset - 1 or acknowledged > end:
                if status_query and saw_ambiguous_failure:
                    raise DriveExtensionError(
                        "timeout_unknown", _MESSAGE_GOOGLE_DRIVE_UPLOAD_OUTCOME_IS_UNKNOWN
                    )
                raise DriveExtensionError(
                    "provider_unavailable", "Google Drive returned an invalid upload offset"
                )
            next_offset = acknowledged + 1
            uncertain = False
            if next_offset > end:
                if not final:
                    return None
                uncertain = True

        code = "timeout_unknown" if saw_ambiguous_failure or uncertain else "provider_unavailable"
        message = (
            _MESSAGE_GOOGLE_DRIVE_UPLOAD_OUTCOME_IS_UNKNOWN
            if code == "timeout_unknown"
            else "Google Drive did not accept the upload chunk"
        )
        raise DriveExtensionError(code, message)

    def _refresh_completed_upload_metadata(
        self,
        raw: bytes,
        *,
        total_size: int,
    ) -> dict[str, Any]:
        try:
            completion = _decode_json_object(raw, purpose="upload")
            file_id = _resource_id(completion.get("id"))
            metadata = self.file_metadata(file_id)
            return _validated_completed_upload_metadata(
                metadata,
                file_id=file_id,
                total_size=total_size,
            )
        except DriveExtensionError as exc:
            raise DriveExtensionError(
                "timeout_unknown", _MESSAGE_GOOGLE_DRIVE_UPLOAD_OUTCOME_IS_UNKNOWN
            ) from exc

    def _remaining_upload_seconds(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise _OperationDeadlineError
        return remaining

    def _request_timeout_for_upload_operation(self) -> float:
        if self._upload_deadline is None:
            return self._timeout_seconds
        return min(
            self._timeout_seconds,
            self._remaining_upload_seconds(self._upload_deadline),
        )

    def _open_upload_request(
        self, request: Request, *, timeout_seconds: float
    ) -> tuple[int, Any, bytes]:
        try:
            with self._opener(
                request,
                timeout_seconds=timeout_seconds,
                purpose="Google Drive resumable upload",
            ) as response:
                status = int(getattr(response, "status", 200))
                raw = b"" if status == 308 else _read_bounded(response, UPLOAD_RESPONSE_LIMIT)
                return status, response.headers, raw
        except HTTPError as exc:
            if exc.code == 308:
                headers = exc.headers
                exc.close()
                return 308, headers, b""
            if exc.code in {408, 429, 500, 502, 503, 504}:
                raise _AmbiguousUploadError from exc
            _raise_mutation_http_error(exc)
            raise AssertionError("unreachable") from exc  # pragma: no cover
        except (PinnedPeerError, OSError) as exc:
            raise _AmbiguousUploadError from exc

    def _request_json(
        self,
        path: str,
        *,
        query: Mapping[str, Any],
        response_limit: int,
        purpose: str,
        _allow_local_reconciliation: bool = True,
    ) -> dict[str, Any]:
        if self._upload_deadline is not None and _allow_local_reconciliation:
            last_error: DriveExtensionError | None = None
            for attempt in range(UPLOAD_ATTEMPTS):
                try:
                    return self._request_json(
                        path,
                        query=query,
                        response_limit=response_limit,
                        purpose=purpose,
                        _allow_local_reconciliation=False,
                    )
                except DriveExtensionError as exc:
                    last_error = exc
                    if exc.retry_facts().get("retryable") is not True:
                        raise
                    if attempt < UPLOAD_ATTEMPTS - 1:
                        self._sleep_for_request_retry(
                            _retry_wait(attempt, exc.retry_after_seconds, self._jitter())
                        )
            assert last_error is not None
            raise last_error
        if path != "/files" and not path.startswith("/files/"):
            raise DriveExtensionError("invalid_payload", "Invalid Drive API path")
        url = f"{DRIVE_API_ORIGIN}{DRIVE_API_PREFIX}{path}"
        pairs = [(str(key), str(value)) for key, value in query.items() if value is not None]
        if pairs:
            url = f"{url}?{urlencode(pairs)}"
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != _TOKEN_WWW_GOOGLEAPIS_COM
            or not parsed.path.startswith(f"{DRIVE_API_PREFIX}/")
        ):
            raise DriveExtensionError("invalid_payload", "Invalid Drive API origin")
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": _TOKEN_APPLICATION_JSON,
                "Authorization": f"Bearer {self._access_token}",
            },
        )
        try:
            timeout_seconds = self._request_timeout_for_upload_operation()
            with self._opener(
                request,
                timeout_seconds=timeout_seconds,
                purpose=purpose,
            ) as response:
                raw = _read_bounded(response, response_limit)
        except HTTPError as exc:
            try:
                error_body = _read_bounded(exc, ERROR_RESPONSE_LIMIT)
            except Exception:
                error_body = b""
            error = _http_error(exc.code, error_body)
            error.failure_class = "provider"
            error.retryable = _retryable_status(exc.code) and error.code != "google_quota_exceeded"
            error.retry_after_seconds = _retry_after_seconds(exc.headers)
            error.definitely_no_external_effect = True
            error.external_effect_status = "failed"
            error.http_status = int(exc.code)
            raise error from exc
        except TimeoutError as exc:
            raise DriveExtensionError(
                "network_failure",
                _MESSAGE_GOOGLE_DRIVE_IS_TEMPORARILY_UNAVAILABLE,
                failure_class="transient",
                retryable=True,
                definitely_no_external_effect=True,
                external_effect_status="failed",
            ) from exc
        except _OperationDeadlineError as exc:
            raise DriveExtensionError(
                "network_failure", _MESSAGE_GOOGLE_DRIVE_UPLOAD_EXCEEDED_ITS_OPERATION_DEADLINE
            ) from exc
        except (PinnedPeerError, OSError) as exc:
            raise DriveExtensionError(
                "network_failure",
                _MESSAGE_GOOGLE_DRIVE_IS_TEMPORARILY_UNAVAILABLE,
                failure_class="transient",
                retryable=True,
                definitely_no_external_effect=True,
                external_effect_status="failed",
            ) from exc
        return _decode_json_object(raw, purpose="request")

    def _sleep_for_request_retry(self, delay_seconds: float) -> None:
        if self._upload_deadline is None:
            self._sleep(delay_seconds)
            return
        try:
            remaining = self._remaining_upload_seconds(self._upload_deadline)
        except _OperationDeadlineError as exc:
            raise DriveExtensionError(
                "network_failure", _MESSAGE_GOOGLE_DRIVE_UPLOAD_EXCEEDED_ITS_OPERATION_DEADLINE
            ) from exc
        self._sleep(min(delay_seconds, remaining))

    def _stream_get(
        self,
        path: str,
        *,
        query: Mapping[str, Any],
        max_bytes: int,
        purpose: str,
    ) -> Iterator[bytes]:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise DriveExtensionError("invalid_payload", "Invalid Drive stream limit")
        url = f"{DRIVE_API_ORIGIN}{DRIVE_API_PREFIX}{path}?{urlencode(query)}"
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != _TOKEN_WWW_GOOGLEAPIS_COM
            or not parsed.path.startswith(f"{DRIVE_API_PREFIX}/files/")
        ):
            raise DriveExtensionError("invalid_payload", "Invalid Drive API origin")
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "application/octet-stream",
                "Authorization": f"Bearer {self._access_token}",
            },
        )
        try:
            with self._opener(
                request,
                timeout_seconds=self._timeout_seconds,
                purpose=purpose,
            ) as response:
                total = 0
                while True:
                    chunk = response.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise DriveExtensionError(
                            "drive_file_too_large", "The Drive file is too large"
                        )
                    yield bytes(chunk)
        except DriveExtensionError:
            raise
        except HTTPError as exc:
            try:
                raw = _read_bounded(exc, ERROR_RESPONSE_LIMIT)
            except Exception:
                raw = b""
            raise _http_error(exc.code, raw) from exc
        except (PinnedPeerError, OSError) as exc:
            raise DriveExtensionError(
                "network_failure", _MESSAGE_GOOGLE_DRIVE_IS_TEMPORARILY_UNAVAILABLE
            ) from exc


def _read_bounded(response: Any, limit: int) -> bytes:
    buffer = bytearray()
    while True:
        chunk = response.read(min(CHUNK_BYTES, limit + 1 - len(buffer)))
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise DriveExtensionError(
                "response_too_large", "Google Drive response exceeds its safe limit"
            )
    return bytes(buffer)


def _validated_completed_upload_metadata(
    metadata: Mapping[str, Any],
    *,
    file_id: str,
    total_size: int,
) -> dict[str, Any]:
    projected = {
        key: metadata[key]
        for key in ("id", "name", "mimeType", "size", "webViewLink")
        if key in metadata
    }
    name = projected.get("name")
    provider_mime_type = projected.get("mimeType")
    size = projected.get("size")
    try:
        provider_web_view_link(projected.get("webViewLink"), maximum=2048)
    except DriveExtensionError as exc:
        raise DriveExtensionError(
            "timeout_unknown", _MESSAGE_GOOGLE_DRIVE_UPLOAD_OUTCOME_IS_UNKNOWN
        ) from exc
    if (
        projected.get("id") != file_id
        or not isinstance(name, str)
        or not name
        or len(name) > 255
        or not isinstance(provider_mime_type, str)
        or len(provider_mime_type) > 255
        or _MIME_TYPE_RE.fullmatch(provider_mime_type) is None
        or not isinstance(size, str)
        or not size.isdigit()
        or int(size) != total_size
    ):
        raise DriveExtensionError(
            "timeout_unknown", _MESSAGE_GOOGLE_DRIVE_UPLOAD_OUTCOME_IS_UNKNOWN
        )
    return projected


def _iter_exact_upload_chunks(
    chunks: Iterable[bytes],
    *,
    size: int,
    check_deadline: Callable[[], float] | None = None,
) -> Iterator[tuple[int, bytes, bool]]:
    iterator = iter(chunks)
    buffered = bytearray()
    offset = 0

    while offset < size:
        target_size = min(UPLOAD_CHUNK_BYTES, size - offset)
        while len(buffered) < target_size:
            if check_deadline is not None:
                check_deadline()
            try:
                item = next(iterator)
            except StopIteration as exc:
                raise DriveExtensionError(
                    "artifact_input_unavailable",
                    "The artifact stream ended before its signed size",
                ) from exc
            if not isinstance(item, (bytes, bytearray, memoryview)):
                raise DriveExtensionError(
                    "artifact_input_unavailable", "The artifact stream returned invalid bytes"
                )
            buffered.extend(item)
            if check_deadline is not None:
                check_deadline()

        chunk = bytes(buffered[:target_size])
        del buffered[:target_size]
        next_offset = offset + target_size
        final = next_offset == size
        if final:
            _assert_upload_source_exhausted(iterator, buffered)
        yield offset, chunk, final
        offset = next_offset

    if size == 0:
        if check_deadline is not None:
            check_deadline()
        _assert_upload_source_exhausted(iterator, buffered)
        yield 0, b"", True


def _bounded_timeout(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = default
    if not math.isfinite(timeout):
        timeout = default
    return max(minimum, min(timeout, maximum))


def _assert_upload_source_exhausted(iterator: Iterator[bytes], buffered: bytearray) -> None:
    if buffered:
        raise DriveExtensionError(
            "artifact_input_unavailable", "The artifact stream exceeded its signed size"
        )
    for item in iterator:
        if not isinstance(item, (bytes, bytearray, memoryview)):
            raise DriveExtensionError(
                "artifact_input_unavailable", "The artifact stream returned invalid bytes"
            )
        if item:
            raise DriveExtensionError(
                "artifact_input_unavailable", "The artifact stream exceeded its signed size"
            )


def _resumable_acknowledged_offset(headers: Any) -> int:
    try:
        value = str(headers.get("Range") or "").strip() if headers is not None else ""
    except Exception as exc:
        raise DriveExtensionError(
            "provider_unavailable", "Google Drive returned invalid upload headers"
        ) from exc
    if not value:
        return -1
    match = _RESUMABLE_RANGE_RE.fullmatch(value)
    if match is None:
        raise DriveExtensionError(
            "provider_unavailable", "Google Drive returned an invalid upload offset"
        )
    return int(match.group(1))


def _decode_json_object(raw: bytes, *, purpose: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DriveExtensionError(
            "provider_unavailable", f"Google Drive returned invalid {purpose} JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise DriveExtensionError(
            "provider_unavailable", "Google Drive returned an invalid response"
        )
    return payload


def _resource_id(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if (
        not text
        or len(text) > 512
        or "://" in text
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        raise DriveExtensionError("invalid_payload", "Invalid Drive resource ID")
    return text


def _drive_query_literal(value: Any) -> str:
    return _resource_id(value).replace("\\", "\\\\").replace("'", "\\'")


def _valid_upload_metadata(value: Mapping[str, Any]) -> bool:
    if set(value) != {"name", "parents", "appProperties"}:
        return False
    name = value.get("name")
    parents = value.get("parents")
    properties = value.get("appProperties")
    marker = (
        properties.get("flow_steward_operation_id") if isinstance(properties, Mapping) else None
    )
    return bool(
        isinstance(name, str)
        and 1 <= len(name.encode("utf-8")) <= 255
        and isinstance(parents, list)
        and len(parents) == 1
        and isinstance(parents[0], str)
        and parents[0]
        and isinstance(properties, Mapping)
        and set(properties) == {"flow_steward_operation_id"}
        and isinstance(marker, str)
        and 1 <= len(marker) <= 128
    )


def _valid_google_spreadsheet_metadata(value: Mapping[str, Any]) -> bool:
    if set(value) != {"name", "parents", "mimeType", "appProperties"}:
        return False
    projected = {key: item for key, item in value.items() if key != "mimeType"}
    return bool(
        _valid_upload_metadata(projected)
        and value.get("mimeType") == "application/vnd.google-apps.spreadsheet"
    )


def _validate_resumable_url(value: Any) -> None:
    text = value.strip() if isinstance(value, str) else ""
    parsed = urlsplit(text)
    try:
        port = parsed.port
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise DriveExtensionError(
            "provider_unavailable", "Google Drive returned an invalid upload session"
        ) from exc
    upload_ids = query.get("upload_id", [])
    session_credentials = query.get("session_crd", [])
    if (
        parsed.scheme != "https"
        or parsed.hostname != _TOKEN_WWW_GOOGLEAPIS_COM
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path != "/upload/drive/v3/files"
        or "#" in text
        or parsed.fragment
        or set(query) - _RESUMABLE_QUERY_FIELDS
        or len(upload_ids) != 1
        or not upload_ids[0]
        or len(upload_ids[0]) > _RESUMABLE_SESSION_VALUE_LIMIT
        or ("uploadType" in query and query["uploadType"] != ["resumable"])
        or ("fields" in query and query["fields"] != [_RESUMABLE_RESPONSE_FIELDS])
        or (
            "session_crd" in query
            and (
                len(session_credentials) != 1
                or not session_credentials[0]
                or len(session_credentials[0]) > _RESUMABLE_SESSION_VALUE_LIMIT
            )
        )
    ):
        raise DriveExtensionError(
            "provider_unavailable", "Google Drive returned an invalid upload session"
        )


def _raise_mutation_http_error(exc: HTTPError) -> None:
    try:
        raw = _read_bounded(exc, ERROR_RESPONSE_LIMIT)
    except Exception:
        raw = b""
    if exc.code in {408, 429, 500, 502, 503, 504}:
        raise DriveExtensionError(
            "timeout_unknown",
            _MESSAGE_GOOGLE_DRIVE_UPLOAD_OUTCOME_IS_UNKNOWN,
            failure_class="provider",
            retryable=False,
            retry_after_seconds=_retry_after_seconds(exc.headers),
            definitely_no_external_effect=False,
            external_effect_status="timeout_unknown",
            http_status=int(exc.code),
        ) from exc
    raise _http_error(exc.code, raw) from exc


def _retryable_status(status: int) -> bool:
    return status in {408, 429, 500, 502, 503, 504}


def _retry_wait(attempt: int, headers: Any, jitter: float) -> float:
    retry_after = ""
    if headers is not None:
        try:
            retry_after = str(headers.get("Retry-After") or "").strip()
        except Exception:
            retry_after = ""
    if retry_after.isdigit():
        return min(2.0, max(0.0, float(retry_after)))
    return min(2.0, (0.25 * (2**attempt)) + max(0.0, min(float(jitter), 1.0)) * 0.1)


def _retry_after_seconds(headers: Any) -> float | None:
    try:
        raw = str(headers.get("Retry-After") or "").strip()
    except Exception:
        return None
    return parse_retry_after(raw)


def _http_error(status: int, raw: bytes) -> DriveExtensionError:
    reasons = _safe_error_reasons(raw)
    if status == 401 or reasons & {"autherror", "invalidcredentials"}:
        code = "google_reauthorization_required"
    elif reasons & {"insufficientpermissions", "insufficientauthenticationscopes"}:
        code = "google_scope_required"
    elif reasons & {"accessnotconfigured", "servicedisabled", "apidisabled"}:
        code = "google_api_disabled"
    elif reasons & {"domainpolicy", "appnotconfiguredforuser", "accessdenied"}:
        code = "google_workspace_admin_approval_required"
    elif reasons & {"storagequotaexceeded", "dailylimitexceeded", "quotaexceeded"}:
        code = "google_quota_exceeded"
    elif status == 429 or reasons & {"ratelimitexceeded", "userratelimitexceeded"}:
        code = "google_rate_limited"
    elif status == 404:
        code = "google_file_not_found"
    else:
        code = "provider_unavailable"
    return DriveExtensionError(code, "Google Drive rejected the request")


def _safe_error_reasons(raw: bytes) -> set[str]:
    if not raw or len(raw) > ERROR_RESPONSE_LIMIT:
        return set()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return set()
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return set()
    reasons: set[str] = set()
    status = error.get("status")
    if isinstance(status, str):
        reasons.add(status.replace("_", "").lower())
    rows = error.get("errors")
    if isinstance(rows, list):
        for row in rows[:20]:
            reason = row.get("reason") if isinstance(row, dict) else None
            if isinstance(reason, str):
                reasons.add(reason.replace("_", "").lower())
    return reasons
