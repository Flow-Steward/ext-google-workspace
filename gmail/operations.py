from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .artifacts import get_attachment
from .errors import SAFE_ERROR_CODES, GmailExtensionError
from .messages import parse_message
from .mutations import delete_messages, move_messages, set_message_flags
from .search import compile_query, normalized_filters, search_messages
from .transport import GmailTransport
from .validation import (
    MUTATIONS,
    access_token,
    identifier,
    mapping,
    validated_envelope,
)

_MESSAGE_GMAIL_RETURNED_INVALID_LABELS = "Gmail returned invalid labels"

LABEL_QUERY_IDS = frozenset({"gmail_labels", "gmail_move_labels"})
MAX_LABEL_ROWS = 1000


def handle_runtime(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("mode") == "action":
        return handle_payload(payload)
    if payload.get("mode") != "query":
        return _error("invalid_payload", "Request mode must be action or query")
    try:
        query = mapping(payload.get("query"))
        query_id = str(query.get("query_id") or "").strip()
        if query_id not in LABEL_QUERY_IDS:
            raise GmailExtensionError("invalid_payload", "Unknown Gmail resource query")
        context = mapping(query.get("context"))
        connection_id = str(context.get("connection_id") or "").strip()
        target = mapping(payload.get("target"))
        if not connection_id or not target:
            raise GmailExtensionError(
                "invalid_connection", "Gmail resource query requires a selected connection"
            )
        action_response = handle_payload(
            {
                "mode": "action",
                "action": {
                    "action_id": "list_labels",
                    "input": {"connection_ref": connection_id},
                    "target": target,
                },
            }
        )
        if action_response.get("ok") is not True:
            return action_response
        labels = mapping(action_response.get("result")).get("labels")
        if not isinstance(labels, list):
            raise GmailExtensionError(
                "provider_unavailable", _MESSAGE_GMAIL_RETURNED_INVALID_LABELS
            )
        items = [
            dict(row)
            for row in labels
            if isinstance(row, Mapping)
            and (
                query_id == "gmail_labels"
                or str(row.get("label_id") or "") == "INBOX"
                or str(row.get("label_type") or "").lower() == "user"
            )
        ]
        return {"ok": True, "result": {"items": items, "next_cursor": "", "total": len(items)}}
    except GmailExtensionError as exc:
        response = _error(exc.code, exc.message)
        response.update(exc.retry_facts())
        return response
    except Exception:
        return _error("internal_error", "The Gmail resource query failed")


def handle_payload(
    payload: dict[str, Any],
    *,
    transport_factory: Callable[[str], GmailTransport] = GmailTransport,
) -> dict[str, Any]:
    operation_id = ""
    try:
        operation_id, operation_input = validated_envelope(payload)
        test_mode = bool(mapping(payload.get("runtime_context")).get("test_mode"))
        if operation_id in MUTATIONS and test_mode:
            result = _dispatch_mutation(None, operation_id, operation_input, test_mode=True)
        else:
            token = access_token(payload, connection_ref=str(operation_input["connection_ref"]))
            transport = transport_factory(token)
            result = _dispatch(payload, transport, operation_id, operation_input, test_mode)
        response: dict[str, Any] = {"ok": True, "result": result}
        if operation_id in MUTATIONS:
            response["external_effect_status"] = result["external_effect_status"]
            response["definitely_no_external_effect"] = result["definitely_no_external_effect"]
        return response
    except GmailExtensionError as exc:
        code = exc.code if exc.code in SAFE_ERROR_CODES else "internal_error"
        result: dict[str, Any] = {}
        response = {
            "ok": False,
            "result": result,
            "error_code": code,
            "error": exc.message,
            "errors": [{"code": code, "message": exc.message}],
        }
        if operation_id in MUTATIONS:
            response.update(
                {
                    "external_effect_status": "timeout_unknown" if exc.ambiguous else "failed",
                    "definitely_no_external_effect": not exc.ambiguous,
                }
            )
        response.update(exc.retry_facts())
        return response
    except Exception:
        return _error("internal_error", "The Gmail operation failed")


def _dispatch(
    root: dict[str, Any],
    transport: GmailTransport,
    operation_id: str,
    operation_input: dict[str, Any],
    test_mode: bool,
) -> dict[str, Any]:
    if operation_id == "test_connection":
        profile = transport.profile()
        result = {
            "email_address": str(profile.get("emailAddress") or ""),
            "messages_total": profile.get("messagesTotal"),
            "threads_total": profile.get("threadsTotal"),
            "history_id": str(profile.get("historyId") or ""),
        }
        if (
            "@" not in result["email_address"]
            or isinstance(result["messages_total"], bool)
            or not isinstance(result["messages_total"], int)
            or isinstance(result["threads_total"], bool)
            or not isinstance(result["threads_total"], int)
            or not result["history_id"]
        ):
            raise GmailExtensionError(
                "mailbox_unusable", "The Google account has no usable Gmail mailbox"
            )
        return result
    if operation_id == "list_labels":
        return _list_labels(transport)
    if operation_id == "search_messages":
        return search_messages(transport, operation_input)
    if operation_id == "get_message":
        message_id = identifier(operation_input.get("message_id"), "message_id")
        return {
            "message": parse_message(
                transport.message(message_id),
                include_body=operation_input.get("include_body") is not False,
                include_headers=operation_input.get("include_headers") is not False,
                include_attachment_metadata=operation_input.get("include_attachment_metadata")
                is not False,
                include_raw_html=operation_input.get("include_raw_html") is True,
            )
        }
    if operation_id == "get_attachment":
        return get_attachment(transport, root, operation_input)
    return _dispatch_mutation(transport, operation_id, operation_input, test_mode=test_mode)


def _dispatch_mutation(
    transport: GmailTransport | None,
    operation_id: str,
    operation_input: dict[str, Any],
    *,
    test_mode: bool,
) -> dict[str, Any]:
    if operation_id == "set_message_flags":
        return set_message_flags(transport, operation_input, test_mode=test_mode)  # type: ignore[arg-type]
    if operation_id == "move_messages":
        return move_messages(transport, operation_input, test_mode=test_mode)  # type: ignore[arg-type]
    if operation_id == "delete_messages":
        return delete_messages(transport, operation_input, test_mode=test_mode)  # type: ignore[arg-type]
    raise GmailExtensionError("invalid_payload", "Unknown Gmail operation")


def _list_labels(transport: GmailTransport) -> dict[str, Any]:
    rows = transport.labels().get("labels")
    if not isinstance(rows, list):
        raise GmailExtensionError("provider_unavailable", _MESSAGE_GMAIL_RETURNED_INVALID_LABELS)
    if len(rows) > MAX_LABEL_ROWS:
        raise GmailExtensionError("label_list_too_large", "Gmail returned too many labels")
    labels = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise GmailExtensionError(
                "provider_unavailable", _MESSAGE_GMAIL_RETURNED_INVALID_LABELS
            )
        label_id = str(row.get("id") or "")
        name = str(row.get("name") or "")
        label_type = str(row.get("type") or "").lower()
        if not label_id or not name or label_type not in {"system", "user"}:
            raise GmailExtensionError(
                "provider_unavailable", _MESSAGE_GMAIL_RETURNED_INVALID_LABELS
            )
        labels.append({"label_id": label_id, "name": name, "label_type": label_type})
    return {"labels": labels}


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "result": {},
        "error_code": code,
        "error": message,
        "errors": [{"code": code, "message": message}],
    }


# Compatibility exports for existing contract tests; implementations live in search.py.
_compile_query = compile_query
_normalized_filters = normalized_filters
