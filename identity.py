"""Extension-owned Google OpenID post-connect identity verification."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.request import Request

from flowsteward_extension_sdk import open_pinned_url

USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
RESPONSE_LIMIT = 16 * 1024
READ_CHUNK_BYTES = 4096
_UNAVAILABLE = "Google account identity is unavailable"


class IdentityError(RuntimeError):
    pass


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "result": {},
        "error_code": code,
        "error": message,
        "errors": [{"code": code, "message": message}],
    }


def _read_bounded(response: Any) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(READ_CHUNK_BYTES)
        if not isinstance(chunk, bytes):
            raise IdentityError(_UNAVAILABLE)
        if not chunk:
            break
        total += len(chunk)
        if total > RESPONSE_LIMIT:
            raise IdentityError(_UNAVAILABLE)
        chunks.append(chunk)
    return b"".join(chunks)


def _access_token(payload: Mapping[str, Any]) -> str:
    action = payload.get("action")
    action = action if isinstance(action, Mapping) else {}
    target = action.get("target")
    target = target if isinstance(target, Mapping) else {}
    connection = target.get("connection")
    connection = connection if isinstance(connection, Mapping) else {}
    secrets = connection.get("secrets")
    if not isinstance(secrets, Mapping) or set(secrets) != {"access_token"}:
        return ""
    token = secrets.get("access_token")
    return token.strip() if isinstance(token, str) else ""


def _fetch_identity(access_token: str) -> dict[str, str]:
    request = Request(
        USERINFO_ENDPOINT,
        headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        with open_pinned_url(
            request,
            timeout_seconds=15.0,
            purpose="Google OpenID userinfo",
        ) as response:
            payload = json.loads(_read_bounded(response).decode("utf-8"))
    except IdentityError:
        raise
    except Exception as exc:
        raise IdentityError(_UNAVAILABLE) from exc
    if not isinstance(payload, Mapping):
        raise IdentityError(_UNAVAILABLE)
    subject = payload.get("sub")
    email = payload.get("email")
    if (
        not isinstance(subject, str)
        or not subject
        or subject.strip() != subject
        or len(subject.encode("utf-8")) > 512
        or not isinstance(email, str)
        or not 3 <= len(email.encode("utf-8")) <= 320
        or email.strip() != email
        or email.count("@") != 1
        or email.startswith("@")
        or email.endswith("@")
        or payload.get("email_verified") is not True
    ):
        raise IdentityError(_UNAVAILABLE)
    return {"subject": subject, "email": email}


def handle_verify_identity(payload: dict[str, Any]) -> dict[str, Any]:
    token = _access_token(payload)
    if not token:
        return _error("invalid_payload", "A single connection access token is required")
    try:
        identity = _fetch_identity(token)
    except IdentityError:
        return _error("provider_unavailable", _UNAVAILABLE)
    email = identity["email"]
    return {
        "ok": True,
        "result": {
            "subject": identity["subject"],
            "verified_display_name": email,
            "verified_email": email,
            "safe_metadata": {},
            "health": "ok",
        },
    }


__all__ = ["handle_verify_identity"]
