from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .errors import GmailExtensionError
from .transport import GmailTransport
from .validation import identifier

_MESSAGE_THE_GMAIL_CURSOR_IS_INVALID = "The Gmail cursor is invalid"

CURSOR_VERSION = 1
CURSOR_DOMAIN = "flowsteward.google-workspace.gmail.cursor.v1"
_CURSOR_HMAC_KEY = b"flowsteward.google-workspace.gmail.cursor.integrity.v1"
SEARCH_FILTER_FIELDS = frozenset(
    {
        "from",
        "to",
        "cc",
        "subject",
        "text",
        "rfc_message_id",
        "since",
        "before",
        "read_state",
        "star_state",
        "attachment_state",
    }
)
MAX_PROVIDER_ID_LENGTH = 512
MAX_PROVIDER_PAGE_TOKEN_LENGTH = 4096


def search_messages(
    transport: GmailTransport, operation_input: Mapping[str, Any]
) -> dict[str, Any]:
    query = validated_search_query(operation_input)
    connection_ref = identifier(operation_input.get("connection_ref"), "connection_ref")
    label_id = identifier(operation_input.get("label_id"), "label_id")
    filters = normalized_filters(operation_input.get("filters"))
    limit = operation_input.get("limit", 50)
    payload = transport.messages_list(query)
    raw_rows = payload.get("messages")
    if raw_rows is None:
        rows = []
    elif isinstance(raw_rows, list):
        rows = raw_rows
    else:
        raise GmailExtensionError("provider_unavailable", "Gmail returned invalid messages")
    if len(rows) > limit:
        raise GmailExtensionError("response_too_large", "Gmail search response is too large")
    messages = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise GmailExtensionError("provider_unavailable", "Gmail returned invalid messages")
        messages.append(
            {
                "message_id": _provider_identifier(row.get("id"), "message id"),
                "thread_id": _provider_identifier(row.get("threadId"), "thread id"),
            }
        )
    page_token = payload.get("nextPageToken")
    if page_token is not None and not isinstance(page_token, str):
        raise GmailExtensionError("provider_unavailable", "Gmail returned an invalid page token")
    if isinstance(page_token, str) and (
        len(page_token) > MAX_PROVIDER_PAGE_TOKEN_LENGTH
        or any(ord(char) < 32 or ord(char) == 127 for char in page_token)
    ):
        code = (
            "response_too_large"
            if len(page_token) > MAX_PROVIDER_PAGE_TOKEN_LENGTH
            else "provider_unavailable"
        )
        raise GmailExtensionError(code, "Gmail returned an invalid page token")
    next_cursor = (
        encode_cursor(
            page_token=str(page_token),
            connection_ref=connection_ref,
            label_id=label_id,
            filters=filters,
            limit=limit,
        )
        if isinstance(page_token, str) and page_token
        else None
    )
    return {"messages": messages, "next_cursor": next_cursor, "truncated": bool(next_cursor)}


def validated_search_query(operation_input: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and compile search input without accessing the Gmail provider."""
    connection_ref = identifier(operation_input.get("connection_ref"), "connection_ref")
    label_id = identifier(operation_input.get("label_id"), "label_id")
    filters = normalized_filters(operation_input.get("filters"))
    limit = operation_input.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise GmailExtensionError("invalid_payload", "limit must be between 1 and 200")
    query: dict[str, Any] = {"labelIds": label_id, "maxResults": limit}
    compiled = compile_query(filters)
    if compiled:
        query["q"] = compiled
    if label_id in {"SPAM", "TRASH"}:
        query["includeSpamTrash"] = "true"
    cursor = operation_input.get("cursor")
    if cursor is not None:
        query["pageToken"] = decode_cursor(
            cursor,
            connection_ref=connection_ref,
            label_id=label_id,
            filters=filters,
            limit=limit,
        )
    return query


def _provider_identifier(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise GmailExtensionError("provider_unavailable", f"Gmail returned an invalid {field}")
    if len(value) > MAX_PROVIDER_ID_LENGTH:
        raise GmailExtensionError("response_too_large", f"Gmail returned an oversized {field}")
    return value


def normalized_filters(value: Any) -> dict[str, Any]:
    if value is None:
        return {"read_state": "any", "star_state": "any", "attachment_state": "any"}
    if not isinstance(value, Mapping):
        raise GmailExtensionError("invalid_payload", "filters must be an object")
    if set(value) - SEARCH_FILTER_FIELDS:
        raise GmailExtensionError("invalid_payload", "filters contains an unsupported field")
    result: dict[str, Any] = {}
    enum_values = {
        "read_state": {"any", "read", "unread"},
        "star_state": {"any", "starred", "unstarred"},
        "attachment_state": {"any", "with_attachments", "without_attachments"},
    }
    for key, raw in value.items():
        if key in enum_values:
            normalized = str(raw or "").strip().lower() if isinstance(raw, str) else ""
            if normalized not in enum_values[key]:
                raise GmailExtensionError("invalid_payload", f"filters.{key} is invalid")
            result[key] = normalized
        else:
            result[key] = _filter_text(raw, key)
    for key in enum_values:
        result.setdefault(key, "any")
    if (
        "since" in result
        and "before" in result
        and _date_epoch(result["since"]) >= _date_epoch(result["before"])
    ):
        raise GmailExtensionError("invalid_payload", "filters.since must precede before")
    return result


def compile_query(filters: Mapping[str, Any]) -> str:
    terms: list[str] = []
    for key in ("from", "to", "cc", "subject"):
        if key in filters:
            terms.append(f'{key}:"{_escape(filters[key])}"')
    if "text" in filters:
        terms.append(f'"{_escape(filters["text"])}"')
    if "rfc_message_id" in filters:
        terms.append(f'rfc822msgid:"{_escape(filters["rfc_message_id"])}"')
    if "since" in filters:
        terms.append(f"after:{_date_epoch(filters['since'])}")
    if "before" in filters:
        terms.append(f"before:{_date_epoch(filters['before'])}")
    if filters.get("read_state") != "any":
        terms.append("is:read" if filters["read_state"] == "read" else "is:unread")
    if filters.get("star_state") != "any":
        terms.append("is:starred" if filters["star_state"] == "starred" else "-is:starred")
    if filters.get("attachment_state") != "any":
        terms.append(
            "has:attachment"
            if filters["attachment_state"] == "with_attachments"
            else "-has:attachment"
        )
    return " ".join(terms)


def encode_cursor(
    *,
    page_token: str,
    connection_ref: str,
    label_id: str,
    filters: Mapping[str, Any],
    limit: int,
) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "domain": CURSOR_DOMAIN,
        "page_token": str(page_token),
        "connection_ref": str(connection_ref),
        "label_id": str(label_id),
        "filters": dict(filters),
        "limit": int(limit),
    }
    canonical = _canonical(payload)
    document = {
        "payload": payload,
        "signature": hmac.new(_CURSOR_HMAC_KEY, canonical, hashlib.sha256).hexdigest(),
    }
    return _b64(_canonical(document))


def decode_cursor(
    value: Any,
    *,
    connection_ref: str,
    label_id: str,
    filters: Mapping[str, Any],
    limit: int,
) -> str:
    try:
        document = json.loads(_unb64(value))
        payload = document["payload"]
        signature = document["signature"]
        expected = hmac.new(_CURSOR_HMAC_KEY, _canonical(payload), hashlib.sha256).hexdigest()
    except Exception as exc:
        raise GmailExtensionError("invalid_cursor", _MESSAGE_THE_GMAIL_CURSOR_IS_INVALID) from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(signature, str)
        or not hmac.compare_digest(signature, expected)
    ):
        raise GmailExtensionError("invalid_cursor", _MESSAGE_THE_GMAIL_CURSOR_IS_INVALID)
    expected_binding = {
        "v": CURSOR_VERSION,
        "domain": CURSOR_DOMAIN,
        "connection_ref": str(connection_ref),
        "label_id": str(label_id),
        "filters": dict(filters),
        "limit": int(limit),
    }
    if any(payload.get(key) != expected_value for key, expected_value in expected_binding.items()):
        raise GmailExtensionError("invalid_cursor", "The Gmail cursor does not match this search")
    page_token = payload.get("page_token")
    if not isinstance(page_token, str) or not page_token:
        raise GmailExtensionError("invalid_cursor", _MESSAGE_THE_GMAIL_CURSOR_IS_INVALID)
    return page_token


def _filter_text(value: Any, field: str) -> str:
    text = str(value) if isinstance(value, str) else ""
    if not text or len(text) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise GmailExtensionError("invalid_payload", f"filters.{field} is invalid")
    if field in {"since", "before"}:
        _date_epoch(text)
    return text


def _date_epoch(value: str) -> int:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise GmailExtensionError("invalid_payload", "Date filters must use YYYY-MM-DD") from exc
    return int(parsed.timestamp())


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: Any) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("cursor")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
