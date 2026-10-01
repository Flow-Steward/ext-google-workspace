"""Closed service dispatcher for the Google Workspace extension."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from drive.operations import DRIVE_QUERY_IDS
from drive.operations import OPERATION_IDS as DRIVE_OPERATION_IDS
from drive.operations import handle_runtime as handle_drive_runtime
from gmail.operations import LABEL_QUERY_IDS
from gmail.operations import handle_runtime as handle_gmail_runtime
from gmail.validation import OPERATION_FIELDS
from identity import handle_verify_identity
from picker import handle_picker_bootstrap
from sheets.operations import OPERATION_IDS as SHEETS_OPERATION_IDS
from sheets.operations import handle_runtime as handle_sheets_runtime

RuntimeHandler = Callable[[dict[str, Any]], dict[str, Any]]

OPERATION_REGISTRY = {
    "verify_google_identity": "identity",
    "get_picker_bootstrap": "picker",
    **dict.fromkeys(OPERATION_FIELDS, "gmail"),
    **dict.fromkeys(LABEL_QUERY_IDS, "gmail"),
    **dict.fromkeys(DRIVE_OPERATION_IDS, "drive"),
    **dict.fromkeys(DRIVE_QUERY_IDS, "drive"),
    **dict.fromkeys(SHEETS_OPERATION_IDS, "sheets"),
}
SERVICE_HANDLERS: dict[str, RuntimeHandler] = {
    "identity": handle_verify_identity,
    "picker": handle_picker_bootstrap,
    "gmail": handle_gmail_runtime,
    "drive": handle_drive_runtime,
    "sheets": handle_sheets_runtime,
}


def dispatch_runtime(payload: dict[str, Any]) -> dict[str, Any]:
    operation_id = _operation_id(payload)
    service = OPERATION_REGISTRY.get(operation_id)
    handler = SERVICE_HANDLERS.get(service or "")
    if handler is None:
        return _error("invalid_payload", "Unknown Google Workspace operation")
    return handler(payload)


def _operation_id(payload: Mapping[str, Any]) -> str:
    mode = payload.get("mode")
    envelope_key = ""
    if mode == "action":
        envelope_key = "action"
    elif mode == "query":
        envelope_key = "query"
    envelope = payload.get(envelope_key)
    if not isinstance(envelope, Mapping):
        return ""
    field = "action_id" if envelope_key == "action" else "query_id"
    value = envelope.get(field)
    return value.strip() if isinstance(value, str) else ""


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "result": {},
        "error_code": code,
        "error": message,
        "errors": [{"code": code, "message": message}],
    }


__all__ = ["OPERATION_REGISTRY", "dispatch_runtime"]
