from __future__ import annotations

from typing import Any

from action_envelope import is_valid_action_envelope

from .artifacts import download_drive_file, validated_download_input
from .config import upload_timing
from .errors import DriveExtensionError
from .search import FOLDER_MIME_TYPE, project_file_metadata, search_drive_files
from .transport import DriveTransport
from .uploads import input_artifact, upload_drive_file, validated_upload_input
from .validation import access_token, validated_search_input

OPERATION_IDS = frozenset({"search_drive_files", "download_drive_file", "upload_drive_file"})
_BROWSE_QUERY_IDS = frozenset({"google_drive_files", "google_drive_folders", "google_spreadsheets"})
_PICKER_VALIDATION_QUERY_MIME_TYPES = {
    "google_drive_file_by_id": None,
    "google_drive_folder_by_id": FOLDER_MIME_TYPE,
    "google_spreadsheet_by_id": "application/vnd.google-apps.spreadsheet",
}
DRIVE_QUERY_IDS = _BROWSE_QUERY_IDS | frozenset(_PICKER_VALIDATION_QUERY_MIME_TYPES)
_QUERY_MIME_TYPES = {
    "google_drive_folders": ["application/vnd.google-apps.folder"],
    "google_spreadsheets": ["application/vnd.google-apps.spreadsheet"],
}


def handle_runtime(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        if payload.get("mode") == "query":
            return _handle_query(payload)
        operation_id, raw_input = _operation_envelope(payload)
        if operation_id == "search_drive_files":
            operation_input = validated_search_input(raw_input)
        elif operation_id == "download_drive_file":
            operation_input = validated_download_input(raw_input)
        else:
            operation_input = validated_upload_input(raw_input)
            test_mode = _test_mode(payload)
            if test_mode and _host_test_mode_suppression(payload):
                return _suppressed()
            input_artifact(payload, operation_input)
            if test_mode:
                return _suppressed()
        connection_ref = operation_input["connection_ref"]
        token = access_token(payload, connection_ref=connection_ref)
        if operation_id == "upload_drive_file":
            operation_timeout, request_timeout = upload_timing()
            transport = DriveTransport(
                token,
                timeout_seconds=request_timeout,
                operation_timeout_seconds=operation_timeout,
            )
        else:
            transport = DriveTransport(token)
        if operation_id == "search_drive_files":
            result = search_drive_files(transport, operation_input)
        elif operation_id == "download_drive_file":
            result = download_drive_file(transport, payload, operation_input)
        else:
            result = upload_drive_file(transport, payload, operation_input)
        response = {
            "ok": True,
            "result": result,
            "error_code": None,
            "error": None,
            "errors": [],
        }
        if operation_id == "upload_drive_file":
            response["external_effect_status"] = "succeeded"
        return response
    except DriveExtensionError as exc:
        return _error(
            exc.code,
            exc.message,
            ambiguous=exc.code == "timeout_unknown",
            retry_facts=exc.retry_facts(),
        )
    except Exception:
        return _error("internal_error", "Google Drive operation failed")


def _handle_query(payload: dict[str, Any]) -> dict[str, Any]:
    query = payload.get("query")
    if not isinstance(query, dict):
        raise DriveExtensionError("invalid_payload", "Google Drive resource query is invalid")
    query_id = query.get("query_id")
    if query_id not in DRIVE_QUERY_IDS:
        raise DriveExtensionError("invalid_payload", "Unknown Google Drive resource query")
    context = query.get("context")
    params = query.get("params")
    if not isinstance(context, dict) or not isinstance(params, dict):
        raise DriveExtensionError("invalid_payload", "Google Drive resource query is invalid")
    connection_ref = context.get("connection_id")
    if query_id in _PICKER_VALIDATION_QUERY_MIME_TYPES:
        normalized_connection_ref = validated_search_input(
            {
                "connection_ref": connection_ref,
                "trashed": False,
                "sort": "provider_order",
                "limit": 1,
            }
        )["connection_ref"]
        selected_id = str(params.get("search") or "").strip()
        if not selected_id or len(selected_id) > 512 or "://" in selected_id:
            raise DriveExtensionError("invalid_payload", "Picker resource ID is invalid")
        token = access_token(
            {"action": {"target": payload.get("target")}},
            connection_ref=normalized_connection_ref,
        )
        projected = project_file_metadata(DriveTransport(token).file_metadata(selected_id))
        if projected["file_id"] != selected_id:
            raise DriveExtensionError(
                "provider_unavailable", "Google Drive returned the wrong file metadata"
            )
        expected_mime = _PICKER_VALIDATION_QUERY_MIME_TYPES[str(query_id)]
        is_allowed = (
            projected["mime_type"] != FOLDER_MIME_TYPE
            if expected_mime is None
            else projected["mime_type"] == expected_mime
        )
        items = (
            [
                {
                    "id": projected["file_id"],
                    "label": projected["name"],
                    "mime_type": projected["mime_type"],
                }
            ]
            if is_allowed
            else []
        )
        return {
            "ok": True,
            "result": {"items": items, "next_cursor": "", "total": len(items)},
        }
    search_input: dict[str, Any] = {
        "connection_ref": connection_ref,
        "trashed": False,
        "sort": "provider_order",
        "limit": params.get("limit", 50),
    }
    search = params.get("search")
    if isinstance(search, str) and search.strip():
        search_input.update({"name": search.strip(), "name_match": "contains"})
    cursor = params.get("cursor")
    if isinstance(cursor, str) and cursor:
        search_input["cursor"] = cursor
    mime_types = _QUERY_MIME_TYPES.get(str(query_id))
    if mime_types:
        search_input["mime_types"] = mime_types
    normalized = validated_search_input(search_input)
    token = access_token(
        {"action": {"target": payload.get("target")}},
        connection_ref=normalized["connection_ref"],
    )
    result = search_drive_files(DriveTransport(token), normalized)
    files = result.get("files")
    if not isinstance(files, list):
        raise DriveExtensionError("provider_unavailable", "Google Drive returned invalid files")
    items = [
        {"id": row["file_id"], "label": row["name"], "mime_type": row["mime_type"]}
        for row in files
        if isinstance(row, dict)
    ]
    return {
        "ok": True,
        "result": {
            "items": items,
            "next_cursor": result.get("next_cursor") or "",
            "total": len(items),
        },
    }


def _operation_envelope(payload: Any) -> tuple[str, Any]:
    if not isinstance(payload, dict) or payload.get("mode") != "action":
        raise DriveExtensionError("invalid_payload", "Request mode must be action")
    action = payload.get("action")
    if not is_valid_action_envelope(action):
        raise DriveExtensionError("invalid_payload", "Drive action is invalid")
    operation_id = action.get("action_id") or action.get("operation_id")
    if operation_id not in OPERATION_IDS:
        raise DriveExtensionError("invalid_payload", "Unknown Drive operation")
    return operation_id, action.get("input")


def _test_mode(payload: dict[str, Any]) -> bool:
    runtime = payload.get("runtime_context")
    if not isinstance(runtime, dict):
        return False
    value = runtime.get("test_mode", False)
    if not isinstance(value, bool):
        raise DriveExtensionError("invalid_payload", "runtime_context.test_mode must be boolean")
    return value


def _host_test_mode_suppression(payload: dict[str, Any]) -> bool:
    runtime = payload.get("runtime_context")
    if not isinstance(runtime, dict):
        return False
    value = runtime.get("_host_test_mode_suppression", False)
    if not isinstance(value, bool):
        raise DriveExtensionError(
            "invalid_payload",
            "runtime_context._host_test_mode_suppression must be boolean",
        )
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


__all__ = ["DRIVE_QUERY_IDS", "OPERATION_IDS", "handle_runtime"]
