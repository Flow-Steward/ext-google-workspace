from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from .errors import DriveExtensionError
from .transport import DriveTransport
from .validation import validated_search_input

_MESSAGE_THE_GOOGLE_DRIVE_CURSOR_IS_INVALID = "The Google Drive cursor is invalid"

CURSOR_VERSION = 1
CURSOR_DOMAIN = "flowsteward.google-workspace.drive.search.cursor.v1"
_CURSOR_HMAC_KEY = b"flowsteward.google-workspace.drive.search.cursor.integrity.v1"
MAX_PROVIDER_PAGE_TOKEN_LENGTH = 4096
MAX_CURSOR_LENGTH = 8192
FILES_LIST_FIELDS = "nextPageToken,files(id,name,mimeType,size,modifiedTime,webViewLink)"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
_MIME_RE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")
_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z")
_WEB_VIEW_HOSTS = frozenset({"drive.google.com", "docs.google.com"})


def search_drive_files(
    transport: DriveTransport, operation_input: Mapping[str, Any]
) -> dict[str, Any]:
    normalized = validated_search_input(operation_input)
    query = compiled_files_list_query(normalized)
    payload = transport.files_list(query)
    if set(payload) - {"files", "nextPageToken"}:
        raise DriveExtensionError("provider_unavailable", "Google Drive returned unexpected fields")
    raw_files = payload.get("files")
    if raw_files is None:
        raw_files = []
    if not isinstance(raw_files, list):
        raise DriveExtensionError("provider_unavailable", "Google Drive returned invalid files")
    if len(raw_files) > normalized["limit"]:
        raise DriveExtensionError("response_too_large", "Google Drive returned too many files")
    files = [_project_file(row) for row in raw_files]
    page_token = _provider_page_token(payload.get("nextPageToken"))
    binding = {key: value for key, value in normalized.items() if key != "cursor"}
    next_cursor = encode_cursor(page_token, normalized_input=binding) if page_token else None
    return {"files": files, "next_cursor": next_cursor}


def compiled_files_list_query(normalized: Mapping[str, Any]) -> dict[str, Any]:
    binding = {key: value for key, value in normalized.items() if key != "cursor"}
    terms = [f"trashed = {'true' if binding['trashed'] else 'false'}"]
    if "parent_folder_id" in binding:
        terms.append(f"'{_escape(binding['parent_folder_id'])}' in parents")
    if "name" in binding:
        operator = "=" if binding.get("name_match") == "exact" else "contains"
        terms.append(f"name {operator} '{_escape(binding['name'])}'")
    if "mime_types" in binding:
        terms.append(
            "("
            + " or ".join(f"mimeType = '{_escape(value)}'" for value in binding["mime_types"])
            + ")"
        )
    if "modified_after" in binding:
        terms.append(f"modifiedTime > '{binding['modified_after']}'")
    if "modified_before" in binding:
        terms.append(f"modifiedTime < '{binding['modified_before']}'")
    query: dict[str, Any] = {
        "q": " and ".join(terms),
        "pageSize": binding["limit"],
        "fields": FILES_LIST_FIELDS,
        "spaces": "drive",
        "corpora": "user",
        "includeItemsFromAllDrives": "true",
        "supportsAllDrives": "true",
    }
    if binding["sort"] == "name_asc":
        query["orderBy"] = "name"
    elif binding["sort"] == "modified_time_desc":
        query["orderBy"] = "modifiedTime desc"
    if "cursor" in normalized:
        query["pageToken"] = decode_cursor(normalized["cursor"], normalized_input=binding)
    return query


def encode_cursor(page_token: str, *, normalized_input: Mapping[str, Any]) -> str:
    token = _provider_page_token(page_token)
    if not token:
        raise DriveExtensionError("provider_unavailable", "Google Drive page token is invalid")
    payload = {
        "v": CURSOR_VERSION,
        "domain": CURSOR_DOMAIN,
        "binding": dict(normalized_input),
        "page_token": token,
    }
    canonical = _canonical(payload)
    document = {
        "payload": payload,
        "signature": hmac.new(_CURSOR_HMAC_KEY, canonical, hashlib.sha256).hexdigest(),
    }
    cursor = _b64(_canonical(document))
    if len(cursor) > MAX_CURSOR_LENGTH:
        raise DriveExtensionError("response_too_large", "Google Drive cursor is too large")
    return cursor


def decode_cursor(value: Any, *, normalized_input: Mapping[str, Any]) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_CURSOR_LENGTH:
        raise DriveExtensionError("invalid_payload", _MESSAGE_THE_GOOGLE_DRIVE_CURSOR_IS_INVALID)
    try:
        document = json.loads(_unb64(value))
        payload = document["payload"]
        signature = document["signature"]
        expected = hmac.new(_CURSOR_HMAC_KEY, _canonical(payload), hashlib.sha256).hexdigest()
    except Exception as exc:
        raise DriveExtensionError(
            "invalid_payload", _MESSAGE_THE_GOOGLE_DRIVE_CURSOR_IS_INVALID
        ) from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(signature, str)
        or not hmac.compare_digest(signature, expected)
        or payload.get("v") != CURSOR_VERSION
        or payload.get("domain") != CURSOR_DOMAIN
        or payload.get("binding") != dict(normalized_input)
    ):
        raise DriveExtensionError(
            "invalid_payload", "The Google Drive cursor does not match this search"
        )
    token = _provider_page_token(payload.get("page_token"))
    if not token:
        raise DriveExtensionError("invalid_payload", _MESSAGE_THE_GOOGLE_DRIVE_CURSOR_IS_INVALID)
    return token


def _project_file(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - {
        "id",
        "name",
        "mimeType",
        "size",
        "modifiedTime",
        "webViewLink",
    }:
        raise DriveExtensionError("provider_unavailable", "Google Drive returned invalid files")
    file_id = _provider_text(value.get("id"), maximum=512, field="file id")
    name = _provider_text(value.get("name"), maximum=255, field="file name")
    mime_type = _provider_text(value.get("mimeType"), maximum=255, field="MIME type")
    if _MIME_RE.fullmatch(mime_type) is None or "*" in mime_type:
        raise DriveExtensionError("provider_unavailable", "Google Drive returned invalid MIME type")
    size = _provider_size(value.get("size"))
    modified_time = _provider_timestamp(value.get("modifiedTime"))
    web_view_link = _provider_web_view_link(value.get("webViewLink"))
    return {
        "file_id": file_id,
        "name": name,
        "mime_type": mime_type,
        "size": size,
        "modified_time": modified_time,
        "web_view_link": web_view_link,
        "is_folder": mime_type == FOLDER_MIME_TYPE,
    }


def project_file_metadata(value: Any) -> dict[str, Any]:
    """Validate and project one exact Drive files.get response."""
    return _project_file(value)


def _provider_text(value: Any, *, maximum: int, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        code = (
            "response_too_large"
            if isinstance(value, str) and len(value) > maximum
            else "provider_unavailable"
        )
        raise DriveExtensionError(code, f"Google Drive returned invalid {field}")
    return value


def _provider_size(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.isdigit() or len(value) > 20:
        raise DriveExtensionError("provider_unavailable", "Google Drive returned invalid size")
    parsed = int(value)
    if parsed > (2**63 - 1):
        raise DriveExtensionError("response_too_large", "Google Drive returned oversized size")
    return parsed


def _provider_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64 or _UTC_RE.fullmatch(value) is None:
        raise DriveExtensionError(
            "provider_unavailable", "Google Drive returned invalid modified time"
        )
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise DriveExtensionError(
            "provider_unavailable", "Google Drive returned invalid modified time"
        ) from exc
    return value


def _provider_web_view_link(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 4096:
        raise DriveExtensionError("provider_unavailable", "Google Drive returned invalid web link")
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in _WEB_VIEW_HOSTS:
        raise DriveExtensionError("provider_unavailable", "Google Drive returned invalid web link")
    return value


def _provider_page_token(value: Any) -> str:
    if value is None:
        return ""
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise DriveExtensionError(
            "provider_unavailable", "Google Drive returned invalid page token"
        )
    if len(value) > MAX_PROVIDER_PAGE_TOKEN_LENGTH:
        raise DriveExtensionError("response_too_large", "Google Drive page token is too large")
    return value


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
