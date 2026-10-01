from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .errors import GmailExtensionError

MAX_PARTS = 1000
MAX_DEPTH = 32
MAX_ATTACHMENTS = 100
MAX_TEXT_BYTES = 1024 * 1024
MAX_HEADER_INPUT_BYTES = 256 * 1024
ALLOWED_HEADERS = frozenset({"date", "from", "to", "cc", "reply-to", "subject", "message-id"})


def parse_message(
    message: Mapping[str, Any],
    *,
    include_body: bool = True,
    include_headers: bool = True,
    include_attachment_metadata: bool = True,
    include_raw_html: bool = False,
) -> dict[str, Any]:
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        raise GmailExtensionError("message_too_complex", "Gmail message MIME payload is invalid")
    parts_seen = 0
    plain_text: str | None = None
    raw_html: str | None = None
    attachments: list[dict[str, Any]] = []
    attachments_truncated = False

    def walk(part: Mapping[str, Any], depth: int, *, inside_attachment: bool = False) -> None:
        nonlocal attachments_truncated, parts_seen, plain_text, raw_html
        parts_seen += 1
        if depth > MAX_DEPTH or parts_seen > MAX_PARTS:
            raise GmailExtensionError(
                "message_too_complex", "Gmail message MIME tree is too complex"
            )
        mime_type = str(part.get("mimeType") or "").lower()
        filename = str(part.get("filename") or "")
        raw_body = part.get("body")
        if raw_body is not None and not isinstance(raw_body, Mapping):
            raise GmailExtensionError("provider_unavailable", "Gmail MIME body is invalid")
        body = raw_body if isinstance(raw_body, Mapping) else {}
        part_id = str(part.get("partId") or "")
        is_attachment = bool(filename or body.get("attachmentId"))
        if include_attachment_metadata and filename and part_id:
            raw_size = body.get("size", 0)
            if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size < 0:
                raise GmailExtensionError(
                    "provider_unavailable", "Gmail returned an invalid attachment size"
                )
            if len(attachments) < MAX_ATTACHMENTS:
                attachments.append(
                    {
                        "attachment_id": part_id,
                        "original_filename": filename,
                        "content_type": mime_type or "application/octet-stream",
                        "size_bytes": raw_size,
                    }
                )
            else:
                attachments_truncated = True
        if (
            not inside_attachment
            and not is_attachment
            and include_body
            and (mime_type == "text/plain" or (mime_type == "text/html" and include_raw_html))
            and body.get("data")
        ):
            decoded = decode_base64url(
                str(body["data"]),
                MAX_TEXT_BYTES,
                too_large_code="message_too_large",
            )
            text = decoded.decode("utf-8", errors="replace")
            if mime_type == "text/plain" and plain_text is None:
                plain_text = text
            if mime_type == "text/html" and include_raw_html and raw_html is None:
                raw_html = text
        raw_parts = part.get("parts")
        if raw_parts is not None and not isinstance(raw_parts, list):
            raise GmailExtensionError("provider_unavailable", "Gmail MIME parts are invalid")
        for child in raw_parts or []:
            if not isinstance(child, Mapping):
                raise GmailExtensionError("message_too_complex", "Gmail MIME part is invalid")
            walk(
                child,
                depth + 1,
                inside_attachment=inside_attachment or is_attachment,
            )

    walk(payload, 0)
    raw_headers = payload.get("headers")
    if raw_headers is not None and not isinstance(raw_headers, list):
        raise GmailExtensionError("provider_unavailable", "Gmail headers are invalid")
    headers: dict[str, str] = {}
    header_input_bytes = 0
    for row in raw_headers or []:
        if not isinstance(row, Mapping):
            raise GmailExtensionError("provider_unavailable", "Gmail header is invalid")
        raw_name = row.get("name")
        raw_value = row.get("value")
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise GmailExtensionError("provider_unavailable", "Gmail header is invalid")
        header_input_bytes += len(raw_name.encode("utf-8")) + len(raw_value.encode("utf-8"))
        if header_input_bytes > MAX_HEADER_INPUT_BYTES:
            raise GmailExtensionError("response_too_large", "Gmail headers exceed their safe limit")
        name = raw_name.lower()
        if name in ALLOWED_HEADERS and name not in headers:
            headers[name] = raw_value[:4096]
    normalized_headers = {
        "date": headers.get("date", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "reply_to": headers.get("reply-to", ""),
        "subject": headers.get("subject", ""),
        "rfc_message_id": headers.get("message-id", ""),
    }
    raw_label_ids = message.get("labelIds")
    if raw_label_ids is not None and (
        not isinstance(raw_label_ids, list)
        or any(not isinstance(value, str) for value in raw_label_ids)
    ):
        raise GmailExtensionError("provider_unavailable", "Gmail label IDs are invalid")
    result = {
        "message_id": str(message.get("id") or ""),
        "thread_id": str(message.get("threadId") or ""),
        "label_ids": list(raw_label_ids or []),
        "date": normalized_headers["date"],
        "internal_date": _internal_date(message.get("internalDate")),
        "from": normalized_headers["from"],
        "to": normalized_headers["to"],
        "cc": normalized_headers["cc"],
        "reply_to": normalized_headers["reply_to"],
        "subject": normalized_headers["subject"],
        "rfc_message_id": normalized_headers["rfc_message_id"],
    }
    if include_headers:
        result["headers"] = normalized_headers
    if include_body:
        result["plain_text"] = plain_text
        if include_raw_html:
            result["raw_html"] = raw_html
    if include_attachment_metadata:
        result["attachments"] = attachments
        result["attachments_truncated"] = attachments_truncated
    return result


def _internal_date(value: Any) -> str:
    try:
        milliseconds = int(str(value or ""))
        if milliseconds < 0:
            raise ValueError
        return (
            datetime.fromtimestamp(milliseconds / 1000, tz=UTC).isoformat().replace("+00:00", "Z")
        )
    except (OverflowError, TypeError, ValueError) as exc:
        raise GmailExtensionError(
            "provider_unavailable", "Gmail returned an invalid internal date"
        ) from exc


def decode_base64url(
    value: str,
    max_bytes: int,
    *,
    too_large_code: str = "attachment_too_large",
) -> bytes:
    encoded = str(value or "")
    try:
        decoded = base64.b64decode(
            encoded.replace("-", "+").replace("_", "/") + "=" * (-len(encoded) % 4),
            validate=True,
        )
    except ValueError as exc:
        raise GmailExtensionError(
            "provider_unavailable", "Gmail returned invalid base64 data"
        ) from exc
    if len(decoded) > max_bytes:
        raise GmailExtensionError(too_large_code, "Gmail content exceeds its safe limit")
    return decoded


def find_attachment_part(message: Mapping[str, Any], part_id: str) -> Mapping[str, Any]:
    root = message.get("payload")
    queue = [(root, 0)] if isinstance(root, Mapping) else []
    seen = 0
    while queue:
        part, depth = queue.pop(0)
        seen += 1
        if depth > MAX_DEPTH or seen > MAX_PARTS:
            raise GmailExtensionError(
                "message_too_complex", "Gmail message MIME tree is too complex"
            )
        if str(part.get("partId") or "") == part_id:
            return part
        for child in part.get("parts") or []:
            if not isinstance(child, Mapping):
                raise GmailExtensionError("message_too_complex", "Gmail MIME part is invalid")
            queue.append((child, depth + 1))
    raise GmailExtensionError("attachment_not_found", "The Gmail attachment was not found")
