from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request

from flowsteward_extension_sdk import PinnedPeerError, open_pinned_url, parse_retry_after

from .errors import GmailExtensionError

GMAIL_API_ORIGIN = "https://gmail.googleapis.com"
PROFILE_RESPONSE_LIMIT = 64 * 1024
LABELS_RESPONSE_LIMIT = 1024 * 1024
SEARCH_RESPONSE_LIMIT = 2 * 1024 * 1024
MESSAGE_RESPONSE_LIMIT = 5 * 1024 * 1024
MUTATION_RESPONSE_LIMIT = 64 * 1024
TRASH_RESPONSE_LIMIT = 64 * 1024
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENT_BASE64_BYTES = ((MAX_ATTACHMENT_BYTES + 2) // 3) * 4
FIXED_JSON_OVERHEAD = 4096
ATTACHMENT_RESPONSE_LIMIT = MAX_ATTACHMENT_BASE64_BYTES + FIXED_JSON_OVERHEAD
CHUNK_BYTES = 64 * 1024
ERROR_RESPONSE_LIMIT = 64 * 1024


class GmailTransport:
    def __init__(
        self,
        access_token: str,
        *,
        opener: Callable[..., Any] = open_pinned_url,
        timeout_seconds: float = 15.0,
    ) -> None:
        token = str(access_token or "").strip()
        if not token:
            raise GmailExtensionError("authorization_required", "Gmail authorization is required")
        self._access_token = token
        self._opener = opener
        self._timeout_seconds = max(1.0, float(timeout_seconds))

    def request_json(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        response_limit: int,
        purpose: str,
    ) -> dict[str, Any]:
        normalized_path = str(path or "")
        if not normalized_path.startswith("/gmail/v1/users/me/"):
            raise GmailExtensionError("invalid_payload", "Invalid Gmail API path")
        url = f"{GMAIL_API_ORIGIN}{normalized_path}"
        if query:
            pairs = [(str(key), str(value)) for key, value in query.items() if value is not None]
            if pairs:
                url = f"{url}?{urlencode(pairs)}"
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "gmail.googleapis.com":
            raise GmailExtensionError("invalid_payload", "Invalid Gmail API origin")
        encoded = (
            json.dumps(dict(body), separators=(",", ":")).encode("utf-8")
            if body is not None
            else None
        )
        request = Request(
            url,
            data=encoded,
            method=str(method or "GET").upper(),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._access_token}",
                **({"Content-Type": "application/json"} if encoded is not None else {}),
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
            raise _http_error(
                exc.code,
                error_body=error_body,
                method=request.method,
                purpose=purpose,
                retry_after_seconds=_retry_after_seconds(exc.headers),
            ) from exc
        except TimeoutError as exc:
            raise GmailExtensionError(
                "timeout_unknown" if request.method != "GET" else "network_failure",
                "The Gmail request timed out",
                ambiguous=request.method != "GET",
                **_transport_failure_facts(method=request.method),
            ) from exc
        except PinnedPeerError as exc:
            raise GmailExtensionError(
                "network_failure",
                "The Gmail request failed",
                **_transport_failure_facts(method=request.method),
            ) from exc
        except OSError as exc:
            ambiguous = request.method != "GET"
            raise GmailExtensionError(
                "timeout_unknown" if ambiguous else "network_failure",
                "The Gmail request failed",
                ambiguous=ambiguous,
                **_transport_failure_facts(method=request.method),
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GmailExtensionError(
                "provider_unavailable", "Gmail returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise GmailExtensionError("provider_unavailable", "Gmail returned an invalid response")
        return payload

    def profile(self) -> dict[str, Any]:
        return self.request_json(
            "GET",
            "/gmail/v1/users/me/profile",
            response_limit=PROFILE_RESPONSE_LIMIT,
            purpose="Gmail profile",
        )

    def labels(self) -> dict[str, Any]:
        return self.request_json(
            "GET",
            "/gmail/v1/users/me/labels",
            response_limit=LABELS_RESPONSE_LIMIT,
            purpose="Gmail labels",
        )

    def messages_list(self, query: Mapping[str, Any]) -> dict[str, Any]:
        return self.request_json(
            "GET",
            "/gmail/v1/users/me/messages",
            query=query,
            response_limit=SEARCH_RESPONSE_LIMIT,
            purpose="Gmail message search",
        )

    def message(self, message_id: str) -> dict[str, Any]:
        return self.request_json(
            "GET",
            f"/gmail/v1/users/me/messages/{quote(message_id, safe='')}",
            query={"format": "full"},
            response_limit=MESSAGE_RESPONSE_LIMIT,
            purpose="Gmail message read",
        )

    def attachment(self, message_id: str, attachment_id: str) -> dict[str, Any]:
        return self.request_json(
            "GET",
            f"/gmail/v1/users/me/messages/{quote(message_id, safe='')}/attachments/{quote(attachment_id, safe='')}",
            response_limit=ATTACHMENT_RESPONSE_LIMIT,
            purpose="Gmail attachment read",
        )

    def batch_modify(self, message_ids: list[str], add: list[str], remove: list[str]) -> None:
        self.request_json(
            "POST",
            "/gmail/v1/users/me/messages/batchModify",
            body={"ids": message_ids, "addLabelIds": add, "removeLabelIds": remove},
            response_limit=MUTATION_RESPONSE_LIMIT,
            purpose="Gmail batch modify",
        )

    def trash(self, message_id: str) -> None:
        self.request_json(
            "POST",
            f"/gmail/v1/users/me/messages/{quote(message_id, safe='')}/trash",
            query={"fields": "id"},
            response_limit=TRASH_RESPONSE_LIMIT,
            purpose="Gmail trash message",
        )


def _read_bounded(response: Any, limit: int) -> bytes:
    buffer = bytearray()
    while True:
        chunk = response.read(min(CHUNK_BYTES, limit + 1 - len(buffer)))
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise GmailExtensionError("response_too_large", "Gmail response exceeds its safe limit")
    return bytes(buffer)


def _http_error(
    status: int,
    *,
    error_body: bytes = b"",
    method: str = "GET",
    purpose: str = "",
    retry_after_seconds: float | None = None,
) -> GmailExtensionError:
    reasons = _safe_error_reasons(error_body)
    if status == 401 or reasons & {"autherror", "insufficientpermissions"}:
        code = "authorization_required"
    elif reasons & {"failedprecondition", "mailservicenotenabled"}:
        code = "mailbox_unusable"
    elif reasons & {
        "ratelimitexceeded",
        "userratelimitexceeded",
        "quotaexceeded",
        "dailylimitexceeded",
    }:
        code = "rate_limited"
    elif reasons & {"accessnotconfigured", "servicedisabled", "apidisabled"}:
        code = "gmail_api_disabled"
    elif status == 404:
        code = "attachment_not_found" if "attachment" in purpose.lower() else "message_not_found"
    elif status == 429:
        code = "rate_limited"
    elif (status == 408 and method != "GET") or (status >= 500 and method != "GET"):
        return GmailExtensionError(
            "timeout_unknown",
            "The Gmail mutation result is unknown",
            ambiguous=True,
            failure_class="provider",
            retryable=False,
            definitely_no_external_effect=False,
            external_effect_status="timeout_unknown",
            http_status=status,
        )
    else:
        code = "provider_unavailable"
    if code == "gmail_api_disabled":
        return GmailExtensionError(
            code,
            "Enable the Gmail API in the Google Cloud project that owns this OAuth Client ID, "
            "wait several minutes, then retry.",
            failure_class="provider",
            retryable=False,
            definitely_no_external_effect=True,
            external_effect_status="failed",
            http_status=status,
        )
    retryable = status in {408, 429, 500, 502, 503, 504}
    return GmailExtensionError(
        code,
        "The Gmail API rejected the request",
        failure_class="provider",
        retryable=retryable,
        retry_after_seconds=retry_after_seconds,
        definitely_no_external_effect=True,
        external_effect_status="failed",
        http_status=status,
    )


def _retry_after_seconds(headers: Any) -> float | None:
    try:
        raw = str(headers.get("Retry-After") or "").strip()
    except (AttributeError, TypeError, ValueError):
        return None
    return parse_retry_after(raw)


def _transport_failure_facts(*, method: str) -> dict[str, object]:
    safe_read = str(method).upper() == "GET"
    return {
        "failure_class": "transient",
        "retryable": safe_read,
        "definitely_no_external_effect": safe_read,
        "external_effect_status": "failed" if safe_read else "timeout_unknown",
    }


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
