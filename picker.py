"""Extension-owned bounded Google Picker bootstrap projection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

PICKER_SCOPE = "https://www.googleapis.com/auth/drive.file"


def _error(message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "result": {},
        "error_code": "invalid_payload",
        "error": message,
        "errors": [{"code": "invalid_payload", "message": message}],
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, maximum: int) -> str:
    if not isinstance(value, str) or value.strip() != value or not value:
        return ""
    return value if len(value.encode("utf-8")) <= maximum else ""


def handle_picker_bootstrap(payload: dict[str, Any]) -> dict[str, Any]:
    action = _mapping(payload.get("action"))
    connection = _mapping(_mapping(action.get("target")).get("connection"))
    configuration = _mapping(connection.get("oauth_configuration"))
    client_id = _text(configuration.get("client_id"), 512)
    developer_key = _text(configuration.get("picker_api_key"), 512)
    app_id = _text(configuration.get("picker_app_id"), 64)
    origin = _text(configuration.get("picker_origin"), 2048)
    connection_config = _mapping(connection.get("config"))
    oauth_state = _mapping(connection_config.get("oauth"))
    email_hint = _text(oauth_state.get("verified_email"), 320)
    try:
        parsed = urlsplit(origin)
        valid_origin = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        valid_origin = False
    if not all((client_id, developer_key, app_id, valid_origin)) or not app_id.isdigit():
        return _error("Google Picker configuration is unavailable")
    return {
        "ok": True,
        "result": {
            "client_id": client_id,
            "developer_key": developer_key,
            "app_id": app_id,
            "origin": origin,
            "email_hint": email_hint,
            "scope": PICKER_SCOPE,
        },
    }


__all__ = ["handle_picker_bootstrap"]
