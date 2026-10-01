from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import SheetsExtensionError
from .ranges import normalize_relative_a1

READ_FIELDS = frozenset(
    {
        "connection_ref",
        "spreadsheet_id",
        "sheet_id",
        "sheet_title",
        "range",
        "header_mode",
    }
)


def validated_read_input(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - READ_FIELDS:
        raise SheetsExtensionError("invalid_payload", "Google Sheets read input is invalid")
    has_id = value.get("sheet_id") is not None
    has_title = value.get("sheet_title") is not None
    if has_id == has_title:
        raise SheetsExtensionError(
            "invalid_payload", "Exactly one of sheet_id or sheet_title is required"
        )
    result: dict[str, Any] = {
        "connection_ref": identifier(value.get("connection_ref"), "connection_ref"),
        "spreadsheet_id": identifier(value.get("spreadsheet_id"), "spreadsheet_id"),
        "header_mode": enum(value.get("header_mode", "first_row"), {"first_row", "none"}),
    }
    if has_id:
        sheet_id = value.get("sheet_id")
        if isinstance(sheet_id, bool) or not isinstance(sheet_id, int) or sheet_id < 0:
            raise SheetsExtensionError("invalid_payload", "sheet_id is invalid")
        result["sheet_id"] = sheet_id
    else:
        result["sheet_title"] = bounded_text(
            value.get("sheet_title"), field="sheet_title", maximum=100
        )
    if "range" in value:
        result["range"] = normalize_relative_a1(value.get("range"))
    return result


def access_token(payload: Mapping[str, Any], *, connection_ref: str) -> str:
    action = payload.get("action")
    target = action.get("target") if isinstance(action, Mapping) else None
    connection = target.get("connection") if isinstance(target, Mapping) else None
    if not isinstance(connection, Mapping):
        raise SheetsExtensionError("invalid_connection", "Google Sheets connection is invalid")
    if str(connection.get("connection_id") or "") != connection_ref:
        raise SheetsExtensionError(
            "invalid_connection", "Selected Google connection does not match connection_ref"
        )
    connection_type = str(
        connection.get("connection_type_id") or connection.get("connection_type") or ""
    )
    if connection_type and connection_type != "google_sheets_account":
        raise SheetsExtensionError("invalid_connection", "Google Sheets connection is invalid")
    secrets = connection.get("secrets")
    token = str(secrets.get("access_token") or "") if isinstance(secrets, Mapping) else ""
    if not token:
        raise SheetsExtensionError(
            "google_reauthorization_required", "Reconnect this Google account"
        )
    return token


def identifier(value: Any, field: str) -> str:
    text = bounded_text(value, field=field, maximum=512)
    if "://" in text:
        raise SheetsExtensionError("invalid_payload", f"{field} must be a resource ID")
    return text


def bounded_text(value: Any, *, field: str, maximum: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if (
        not text
        or len(text) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        raise SheetsExtensionError("invalid_payload", f"{field} is invalid")
    return text


def enum(value: Any, allowed: set[str]) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    if text not in allowed:
        raise SheetsExtensionError("invalid_payload", "Invalid enum value")
    return text
