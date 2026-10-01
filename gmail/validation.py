from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from action_envelope import is_valid_action_envelope

from .errors import GmailExtensionError

OPERATION_FIELDS = {
    "test_connection": frozenset({"connection_ref"}),
    "list_labels": frozenset({"connection_ref"}),
    "search_messages": frozenset({"connection_ref", "label_id", "filters", "limit", "cursor"}),
    "get_message": frozenset(
        {
            "connection_ref",
            "message_id",
            "include_body",
            "include_headers",
            "include_attachment_metadata",
            "include_raw_html",
        }
    ),
    "get_attachment": frozenset({"connection_ref", "message_id", "attachment_id"}),
    "set_message_flags": frozenset({"connection_ref", "message_ids", "seen", "flagged"}),
    "move_messages": frozenset(
        {"connection_ref", "message_ids", "source_label_id", "target_label_id"}
    ),
    "delete_messages": frozenset({"connection_ref", "message_ids"}),
}
REQUIRED_FIELDS = {
    "test_connection": frozenset({"connection_ref"}),
    "list_labels": frozenset({"connection_ref"}),
    "search_messages": frozenset({"connection_ref", "label_id"}),
    "get_message": frozenset({"connection_ref", "message_id"}),
    "get_attachment": frozenset({"connection_ref", "message_id", "attachment_id"}),
    "set_message_flags": frozenset({"connection_ref", "message_ids"}),
    "move_messages": frozenset(
        {"connection_ref", "message_ids", "source_label_id", "target_label_id"}
    ),
    "delete_messages": frozenset({"connection_ref", "message_ids"}),
}
MUTATIONS = frozenset({"set_message_flags", "move_messages", "delete_messages"})


def validated_envelope(payload: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("mode") != "action":
        raise GmailExtensionError("invalid_payload", "Request mode must be action")
    action = payload.get("action")
    if not is_valid_action_envelope(action):
        raise GmailExtensionError("invalid_payload", "Gmail action is invalid")
    operation_id = action.get("action_id") or action.get("operation_id")
    if not isinstance(operation_id, str) or operation_id not in OPERATION_FIELDS:
        raise GmailExtensionError("invalid_payload", "Unknown Gmail operation")
    operation_input = action.get("input")
    if not isinstance(operation_input, Mapping):
        raise GmailExtensionError("invalid_payload", "action.input must be an object")
    result = dict(operation_input)
    if set(result) - OPERATION_FIELDS[operation_id] or not REQUIRED_FIELDS[operation_id].issubset(
        result
    ):
        raise GmailExtensionError("invalid_payload", "Operation input contract is invalid")
    if operation_id == "get_message":
        for field in (
            "include_body",
            "include_headers",
            "include_attachment_metadata",
            "include_raw_html",
        ):
            if field in result and not isinstance(result[field], bool):
                raise GmailExtensionError("invalid_payload", f"{field} must be a boolean")
    identifier(result.get("connection_ref"), "connection_ref")
    runtime = payload.get("runtime_context")
    if runtime is not None and not isinstance(runtime, Mapping):
        raise GmailExtensionError("invalid_payload", "runtime_context must be an object")
    if (
        isinstance(runtime, Mapping)
        and "test_mode" in runtime
        and not isinstance(runtime["test_mode"], bool)
    ):
        raise GmailExtensionError("invalid_payload", "runtime_context.test_mode must be boolean")
    return operation_id, result


def access_token(payload: Mapping[str, Any], *, connection_ref: str) -> str:
    connection = connection_payload(payload)
    if str(connection.get("connection_id") or "") != connection_ref:
        raise GmailExtensionError(
            "invalid_connection", "Selected Gmail connection does not match connection_ref"
        )
    secrets = mapping(connection.get("secrets"))
    token = str(secrets.get("access_token") or "")
    if not token:
        raise GmailExtensionError("authorization_required", "Gmail authorization is required")
    return token


def connection_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    action = mapping(payload.get("action"))
    target = mapping(action.get("target"))
    connection = mapping(target.get("connection"))
    if connection:
        connection_type = str(
            connection.get("connection_type_id") or connection.get("connection_type") or ""
        )
        if connection_type and connection_type != "google_gmail_account":
            raise GmailExtensionError("invalid_connection", "Gmail connection is invalid")
        return connection
    raise GmailExtensionError("invalid_connection", "Gmail connection is invalid")


def identifier(value: Any, field: str) -> str:
    text = str(value or "").strip() if isinstance(value, str) else ""
    if not text or len(text) > 512 or any(ord(char) < 32 for char in text):
        raise GmailExtensionError("invalid_payload", f"{field} is invalid")
    return text


def mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}
