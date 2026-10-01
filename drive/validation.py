from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from .errors import DriveExtensionError

_MESSAGE_GOOGLE_DRIVE_RETURNED_INVALID_WEB_LINK = "Google Drive returned invalid web link"

SEARCH_FIELDS = frozenset(
    {
        "connection_ref",
        "parent_folder_id",
        "name",
        "name_match",
        "mime_types",
        "modified_after",
        "modified_before",
        "trashed",
        "sort",
        "limit",
        "cursor",
    }
)
_MIME_RE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")
_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z")
_WEB_VIEW_HOSTS = frozenset({"drive.google.com", "docs.google.com"})


def validated_search_input(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - SEARCH_FIELDS:
        raise DriveExtensionError("invalid_payload", "Drive search input is invalid")
    result: dict[str, Any] = {
        "connection_ref": identifier(value.get("connection_ref"), "connection_ref"),
        "trashed": _boolean(value.get("trashed", False), "trashed"),
        "sort": _enum(
            value.get("sort", "provider_order"),
            {"provider_order", "name_asc", "modified_time_desc"},
            "sort",
        ),
        "limit": _integer(value.get("limit", 50), minimum=1, maximum=100, field="limit"),
    }
    if "parent_folder_id" in value:
        result["parent_folder_id"] = identifier(value.get("parent_folder_id"), "parent_folder_id")
    if "name" in value:
        result["name"] = bounded_text(value.get("name"), field="name", maximum=255)
        result["name_match"] = _enum(
            value.get("name_match", "contains"), {"exact", "contains"}, "name_match"
        )
    elif "name_match" in value:
        raise DriveExtensionError("invalid_payload", "name_match requires name")
    if "mime_types" in value:
        raw_mime_types = value.get("mime_types")
        if not isinstance(raw_mime_types, list) or not 1 <= len(raw_mime_types) <= 20:
            raise DriveExtensionError("invalid_payload", "mime_types is invalid")
        mime_types: list[str] = []
        for raw in raw_mime_types:
            mime_type = bounded_text(raw, field="mime_types", maximum=255)
            if _MIME_RE.fullmatch(mime_type) is None or "*" in mime_type:
                raise DriveExtensionError("invalid_payload", "mime_types is invalid")
            if mime_type not in mime_types:
                mime_types.append(mime_type)
        result["mime_types"] = mime_types
    for field in ("modified_after", "modified_before"):
        if field in value:
            result[field] = utc_timestamp(value.get(field), field=field)
    if (
        "modified_after" in result
        and "modified_before" in result
        and _parsed_timestamp(result["modified_after"])
        >= _parsed_timestamp(result["modified_before"])
    ):
        raise DriveExtensionError("invalid_payload", "modified_after must precede modified_before")
    if "cursor" in value:
        result["cursor"] = bounded_text(value.get("cursor"), field="cursor", maximum=8192)
    return result


def validated_envelope(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or payload.get("mode") != "action":
        raise DriveExtensionError("invalid_payload", "Request mode must be action")
    action = payload.get("action")
    if not isinstance(action, Mapping):
        raise DriveExtensionError("invalid_payload", "action must be an object")
    operation_id = action.get("action_id") or action.get("operation_id")
    if operation_id != "search_drive_files" or set(action) - {
        "action_id",
        "operation_id",
        "input",
        "target",
    }:
        raise DriveExtensionError("invalid_payload", "Unknown Drive operation")
    return validated_search_input(action.get("input"))


def access_token(payload: Mapping[str, Any], *, connection_ref: str) -> str:
    action = payload.get("action")
    target = action.get("target") if isinstance(action, Mapping) else None
    connection = target.get("connection") if isinstance(target, Mapping) else None
    if not isinstance(connection, Mapping):
        raise DriveExtensionError("invalid_connection", "Google Drive connection is invalid")
    if str(connection.get("connection_id") or "") != connection_ref:
        raise DriveExtensionError(
            "invalid_connection", "Selected Google connection does not match connection_ref"
        )
    connection_type = str(
        connection.get("connection_type_id") or connection.get("connection_type") or ""
    )
    if connection_type and connection_type != "google_drive_account":
        raise DriveExtensionError("invalid_connection", "Google Drive connection is invalid")
    secrets = connection.get("secrets")
    token = str(secrets.get("access_token") or "") if isinstance(secrets, Mapping) else ""
    if not token:
        raise DriveExtensionError(
            "google_reauthorization_required", "Reconnect this Google account"
        )
    return token


def identifier(value: Any, field: str) -> str:
    text = bounded_text(value, field=field, maximum=512)
    if "://" in text:
        raise DriveExtensionError("invalid_payload", f"{field} must be a resource ID")
    return text


def bounded_text(value: Any, *, field: str, maximum: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if (
        not text
        or len(text) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        raise DriveExtensionError("invalid_payload", f"{field} is invalid")
    return text


def provider_web_view_link(value: Any, *, maximum: int = 4096) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise DriveExtensionError(
            "provider_unavailable", _MESSAGE_GOOGLE_DRIVE_RETURNED_INVALID_WEB_LINK
        )
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise DriveExtensionError(
            "provider_unavailable", _MESSAGE_GOOGLE_DRIVE_RETURNED_INVALID_WEB_LINK
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _WEB_VIEW_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise DriveExtensionError(
            "provider_unavailable", _MESSAGE_GOOGLE_DRIVE_RETURNED_INVALID_WEB_LINK
        )
    return value


def utc_timestamp(value: Any, *, field: str) -> str:
    text = bounded_text(value, field=field, maximum=64)
    if _UTC_RE.fullmatch(text) is None:
        raise DriveExtensionError("invalid_payload", f"{field} must be an RFC 3339 UTC timestamp")
    try:
        _parsed_timestamp(text)
    except ValueError as exc:
        raise DriveExtensionError(
            "invalid_payload", f"{field} must be an RFC 3339 UTC timestamp"
        ) from exc
    return text


def _parsed_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise DriveExtensionError("invalid_payload", f"{field} must be boolean")
    return value


def _integer(value: Any, *, minimum: int, maximum: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise DriveExtensionError(
            "invalid_payload", f"{field} must be between {minimum} and {maximum}"
        )
    return value


def _enum(value: Any, allowed: set[str], field: str) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if normalized not in allowed:
        raise DriveExtensionError("invalid_payload", f"{field} is invalid")
    return normalized
