from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request

from flowsteward_extension_sdk import PinnedPeerError, open_pinned_url, parse_retry_after

from .errors import SheetsExtensionError

SHEETS_API_ORIGIN = "https://sheets.googleapis.com"
SHEETS_API_PREFIX = "/v4"
METADATA_RESPONSE_LIMIT = 512 * 1024
VALUES_RESPONSE_LIMIT = 16 * 1024 * 1024
MUTATION_RESPONSE_LIMIT = 512 * 1024
MAX_REQUEST_BYTES = 2 * 1024 * 1024
ERROR_RESPONSE_LIMIT = 64 * 1024
CHUNK_BYTES = 64 * 1024


class SheetsTransport:
    def __init__(
        self,
        access_token: str,
        *,
        opener: Callable[..., Any] = open_pinned_url,
        timeout_seconds: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        token = str(access_token or "").strip()
        if not token:
            raise SheetsExtensionError(
                "google_reauthorization_required", "Reconnect this Google account"
            )
        self._access_token = token
        self._opener = opener
        self._timeout_seconds = max(1.0, min(float(timeout_seconds), 30.0))
        self._sleep = sleep
        self._jitter = jitter

    def spreadsheet_metadata(self, spreadsheet_id: str) -> dict[str, Any]:
        safe_id = _resource_id(spreadsheet_id)
        return self._request_json(
            f"/spreadsheets/{quote(safe_id, safe='')}",
            query={
                "fields": "sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"
            },
            response_limit=METADATA_RESPONSE_LIMIT,
            purpose="Google Sheets spreadsheet metadata",
        )

    def values_get(self, spreadsheet_id: str, provider_range: str) -> dict[str, Any]:
        safe_id = _resource_id(spreadsheet_id)
        if (
            not isinstance(provider_range, str)
            or not provider_range
            or len(provider_range) > 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in provider_range)
        ):
            raise SheetsExtensionError("invalid_payload", "Invalid Google Sheets provider range")
        return self._request_json(
            f"/spreadsheets/{quote(safe_id, safe='')}/values/{quote(provider_range, safe='')}",
            query={"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"},
            response_limit=VALUES_RESPONSE_LIMIT,
            purpose="Google Sheets values read",
        )

    def values_clear(self, spreadsheet_id: str, provider_range: str) -> dict[str, Any]:
        safe_id = _resource_id(spreadsheet_id)
        safe_range = _provider_range(provider_range)
        return self._mutation_json(
            f"/spreadsheets/{quote(safe_id, safe='')}/values/{quote(safe_range, safe='')}:clear",
            method="POST",
            query={},
            body={},
            purpose="Google Sheets values clear",
        )

    def values_update(
        self,
        spreadsheet_id: str,
        provider_range: str,
        rows: list[list[Any]],
        *,
        value_input_option: str,
    ) -> dict[str, Any]:
        if value_input_option != "RAW" or not isinstance(rows, list):
            raise SheetsExtensionError("invalid_payload", "Invalid Google Sheets values update")
        safe_id = _resource_id(spreadsheet_id)
        safe_range = _provider_range(provider_range)
        return self._mutation_json(
            f"/spreadsheets/{quote(safe_id, safe='')}/values/{quote(safe_range, safe='')}",
            method="PUT",
            query={"valueInputOption": "RAW"},
            body={"majorDimension": "ROWS", "values": rows},
            purpose="Google Sheets values update",
        )

    def batch_update(self, spreadsheet_id: str, requests: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(requests, list) or not requests:
            raise SheetsExtensionError("invalid_payload", "Invalid Google Sheets batch update")
        safe_id = _resource_id(spreadsheet_id)
        return self._mutation_json(
            f"/spreadsheets/{quote(safe_id, safe='')}:batchUpdate",
            method="POST",
            query={},
            body={"requests": requests},
            purpose="Google Sheets batch update",
        )

    def rename_sheet(self, spreadsheet_id: str, sheet_id: int, title: str) -> None:
        if (
            isinstance(sheet_id, bool)
            or not isinstance(sheet_id, int)
            or sheet_id < 0
            or not isinstance(title, str)
            or not 1 <= len(title) <= 100
        ):
            raise SheetsExtensionError("invalid_payload", "Invalid Google sheet rename")
        self.batch_update(
            spreadsheet_id,
            [
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": sheet_id, "title": title},
                        "fields": "title",
                    }
                }
            ],
        )

    def find_developer_marker(self, spreadsheet_id: str, marker: str) -> dict[str, Any] | None:
        if (
            not isinstance(marker, str)
            or not marker
            or len(marker) > 160
            or any(ord(char) < 32 or ord(char) == 127 for char in marker)
        ):
            raise SheetsExtensionError("invalid_payload", "Invalid Sheets operation marker")
        safe_id = _resource_id(spreadsheet_id)
        payload = self._mutation_json(
            f"/spreadsheets/{quote(safe_id, safe='')}/developerMetadata:search",
            method="POST",
            query={},
            body={
                "dataFilters": [
                    {
                        "developerMetadataLookup": {
                            "metadataKey": "flow_steward_operation_id",
                            "metadataValue": marker,
                            "visibility": "DOCUMENT",
                        }
                    }
                ]
            },
            purpose="Google Sheets operation marker lookup",
            ambiguous=False,
        )
        rows = payload.get("matchedDeveloperMetadata", [])
        if not isinstance(rows, list) or len(rows) > 1:
            raise SheetsExtensionError(
                "provider_unavailable", "Google Sheets returned invalid operation metadata"
            )
        if not rows:
            return None
        row = rows[0]
        metadata = row.get("developerMetadata") if isinstance(row, dict) else None
        if not isinstance(metadata, dict) or metadata.get("metadataValue") != marker:
            raise SheetsExtensionError(
                "provider_unavailable", "Google Sheets returned invalid operation metadata"
            )
        return metadata

    def _mutation_json(
        self,
        path: str,
        *,
        method: str,
        query: dict[str, str],
        body: dict[str, Any],
        purpose: str,
        ambiguous: bool = True,
    ) -> dict[str, Any]:
        url = f"{SHEETS_API_ORIGIN}{SHEETS_API_PREFIX}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "sheets.googleapis.com"
            or not parsed.path.startswith(f"{SHEETS_API_PREFIX}/spreadsheets/")
        ):
            raise SheetsExtensionError("invalid_payload", "Invalid Google Sheets API origin")
        encoded = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise SheetsExtensionError("sheet_too_large", "Google Sheets request is too large")
        request = Request(
            url,
            method=method,
            data=encoded,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with self._opener(
                request,
                timeout_seconds=self._timeout_seconds,
                purpose=purpose,
            ) as response:
                raw = _read_bounded(response, MUTATION_RESPONSE_LIMIT)
        except HTTPError as exc:
            _raise_write_error(exc, ambiguous=ambiguous)
        except (PinnedPeerError, OSError) as exc:
            code = "timeout_unknown" if ambiguous else "network_failure"
            raise SheetsExtensionError(code, "Google Sheets is temporarily unavailable") from exc
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SheetsExtensionError(
                "provider_unavailable", "Google Sheets returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise SheetsExtensionError(
                "provider_unavailable", "Google Sheets returned an invalid response"
            )
        return payload

    def _request_json(
        self,
        path: str,
        *,
        query: dict[str, str],
        response_limit: int,
        purpose: str,
    ) -> dict[str, Any]:
        url = f"{SHEETS_API_ORIGIN}{SHEETS_API_PREFIX}{path}?{urlencode(query)}"
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "sheets.googleapis.com"
            or not parsed.path.startswith(f"{SHEETS_API_PREFIX}/spreadsheets/")
        ):
            raise SheetsExtensionError("invalid_payload", "Invalid Google Sheets API origin")
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._access_token}",
            },
        )
        try:
            with self._opener(
                request,
                timeout_seconds=self._timeout_seconds,
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
        except (PinnedPeerError, OSError) as exc:
            raise SheetsExtensionError(
                "network_failure",
                "Google Sheets is temporarily unavailable",
                failure_class="transient",
                retryable=True,
                definitely_no_external_effect=True,
                external_effect_status="failed",
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SheetsExtensionError(
                "provider_unavailable", "Google Sheets returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise SheetsExtensionError(
                "provider_unavailable", "Google Sheets returned an invalid response"
            )
        return payload


def _read_bounded(response: Any, limit: int) -> bytes:
    buffer = bytearray()
    while True:
        chunk = response.read(min(CHUNK_BYTES, limit + 1 - len(buffer)))
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise SheetsExtensionError(
                "response_too_large", "Google Sheets response exceeds its safe limit"
            )
    return bytes(buffer)


def _resource_id(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if (
        not text
        or len(text) > 512
        or "://" in text
        or any(ord(char) < 32 or ord(char) == 127 for char in text)
    ):
        raise SheetsExtensionError("invalid_payload", "Invalid Google Sheets resource ID")
    return text


def _provider_range(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > 1024 or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise SheetsExtensionError("invalid_payload", "Invalid Google Sheets provider range")
    return text


def _raise_write_error(exc: HTTPError, *, ambiguous: bool) -> None:
    try:
        raw = _read_bounded(exc, ERROR_RESPONSE_LIMIT)
    except Exception:
        raw = b""
    if ambiguous and exc.code in {408, 429, 500, 502, 503, 504}:
        raise SheetsExtensionError(
            "timeout_unknown",
            "Google Sheets write outcome is unknown",
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


def _http_error(status: int, raw: bytes) -> SheetsExtensionError:
    reasons = _safe_error_reasons(raw)
    if status == 401 or reasons & {"autherror", "invalidcredentials"}:
        code = "google_reauthorization_required"
    elif reasons & {"insufficientpermissions", "insufficientauthenticationscopes"}:
        code = "google_scope_required"
    elif reasons & {"accessnotconfigured", "servicedisabled", "apidisabled"}:
        code = "google_api_disabled"
    elif reasons & {"domainpolicy", "appnotconfiguredforuser", "accessdenied"}:
        code = "google_workspace_admin_approval_required"
    elif reasons & {"dailylimitexceeded", "quotaexceeded"}:
        code = "google_quota_exceeded"
    elif status == 429 or reasons & {"ratelimitexceeded", "userratelimitexceeded"}:
        code = "google_rate_limited"
    elif status == 404:
        code = "google_file_not_found"
    else:
        code = "provider_unavailable"
    return SheetsExtensionError(code, "Google Sheets rejected the request")


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
