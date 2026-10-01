from __future__ import annotations

from typing import Any

from action_envelope import is_valid_action_envelope
from drive.transport import DriveTransport

from .errors import SheetsExtensionError
from .reads import read_google_sheet
from .transport import SheetsTransport
from .validation import access_token, validated_read_input
from .writes import (
    append_google_sheet_rows,
    row_source,
    validated_append_input,
    validated_write_input,
    write_google_sheet,
)

OPERATION_IDS = frozenset({"read_google_sheet", "write_google_sheet", "append_google_sheet_rows"})


def handle_runtime(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        operation_id, raw_input = _operation_envelope(payload)
        if operation_id == "read_google_sheet":
            operation_input = validated_read_input(raw_input)
        elif operation_id == "write_google_sheet":
            operation_input = validated_write_input(raw_input)
        else:
            operation_input = validated_append_input(raw_input)
        if operation_id != "read_google_sheet" and _test_mode(payload):
            with row_source(payload, operation_input) as source:
                source.validate_batches()
            return _suppressed()
        connection_ref = operation_input["connection_ref"]
        token = access_token(payload, connection_ref=connection_ref)
        sheets_transport = SheetsTransport(token)
        if operation_id == "read_google_sheet":
            result = read_google_sheet(sheets_transport, payload, operation_input)
        elif operation_id == "write_google_sheet":
            result = write_google_sheet(
                sheets_transport, DriveTransport(token), payload, operation_input
            )
        else:
            result = append_google_sheet_rows(sheets_transport, payload, operation_input)
        response = {
            "ok": True,
            "result": result,
            "error_code": None,
            "error": None,
            "errors": [],
        }
        if operation_id != "read_google_sheet":
            response["external_effect_status"] = "succeeded"
        return response
    except SheetsExtensionError as exc:
        return _error(
            exc.code,
            exc.message,
            ambiguous=exc.code == "timeout_unknown",
            retry_facts=exc.retry_facts(),
        )
    except Exception:
        return _error("internal_error", "Google Sheets operation failed")


def _operation_envelope(payload: Any) -> tuple[str, Any]:
    if not isinstance(payload, dict) or payload.get("mode") != "action":
        raise SheetsExtensionError("invalid_payload", "Request mode must be action")
    action = payload.get("action")
    if not is_valid_action_envelope(action):
        raise SheetsExtensionError("invalid_payload", "Google Sheets action is invalid")
    operation_id = action.get("action_id") or action.get("operation_id")
    if operation_id not in OPERATION_IDS:
        raise SheetsExtensionError("invalid_payload", "Unknown Google Sheets operation")
    return operation_id, action.get("input")


def _test_mode(payload: dict[str, Any]) -> bool:
    runtime = payload.get("runtime_context")
    if not isinstance(runtime, dict):
        return False
    value = runtime.get("test_mode", False)
    if not isinstance(value, bool):
        raise SheetsExtensionError("invalid_payload", "runtime_context.test_mode must be boolean")
    return value


def _suppressed() -> dict[str, Any]:
    return {
        "ok": True,
        "result": {
            "test_mode_status": "suppressed",
            "external_effect_status": "suppressed",
            "definitely_no_external_effect": True,
        },
        "external_effect_status": "suppressed",
        "definitely_no_external_effect": True,
    }


def _error(
    code: str,
    message: str,
    *,
    ambiguous: bool = False,
    retry_facts: dict[str, object] | None = None,
) -> dict[str, Any]:
    response = {
        "ok": False,
        "result": {},
        "error_code": code,
        "error": message,
        "errors": [{"code": code, "message": message}],
    }
    if ambiguous:
        response.update(
            {
                "external_effect_status": "timeout_unknown",
                "definitely_no_external_effect": False,
            }
        )
    response.update(dict(retry_facts or {}))
    return response


__all__ = ["OPERATION_IDS", "handle_runtime"]
