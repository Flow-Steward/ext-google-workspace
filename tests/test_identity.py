from __future__ import annotations

import json
from contextlib import contextmanager

import pytest


class _Response:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


def test_verify_identity_owns_fixed_google_request_and_projects_closed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import identity

    captured: dict = {}

    @contextmanager
    def open_url(request, *, timeout_seconds, purpose):
        captured.update(
            url=request.full_url,
            authorization=request.headers["Authorization"],
            timeout=timeout_seconds,
            purpose=purpose,
        )
        yield _Response(
            {
                "sub": "google-subject-1",
                "email": "Person@Example.test",
                "email_verified": True,
                "picture": "https://tracking.example.test/canary",
            }
        )

    monkeypatch.setattr(identity, "open_pinned_url", open_url)

    response = identity.handle_verify_identity(
        {
            "mode": "action",
            "action": {
                "action_id": "verify_google_identity",
                "target": {
                    "connection": {
                        "connection_id": "connection-1",
                        "secrets": {"access_token": "access-token-canary"},
                    }
                },
            },
        }
    )

    assert response == {
        "ok": True,
        "result": {
            "subject": "google-subject-1",
            "verified_display_name": "Person@Example.test",
            "verified_email": "Person@Example.test",
            "safe_metadata": {},
            "health": "ok",
        },
    }
    assert captured == {
        "url": "https://openidconnect.googleapis.com/v1/userinfo",
        "authorization": "Bearer access-token-canary",
        "timeout": 15.0,
        "purpose": "Google OpenID userinfo",
    }
    assert "tracking" not in repr(response)


@pytest.mark.parametrize(
    "payload",
    [
        {"sub": "subject", "email": "person@example.test", "email_verified": False},
        {"sub": "", "email": "person@example.test", "email_verified": True},
        {"sub": True, "email": "person@example.test", "email_verified": True},
        {"sub": "subject", "email": "not-an-email", "email_verified": True},
        {"sub": "s" * 513, "email": "person@example.test", "email_verified": True},
    ],
)
def test_verify_identity_rejects_malformed_provider_data(
    monkeypatch: pytest.MonkeyPatch, payload: dict
) -> None:
    import identity

    @contextmanager
    def open_url(*_args, **_kwargs):
        yield _Response(payload)

    monkeypatch.setattr(identity, "open_pinned_url", open_url)

    response = identity.handle_verify_identity(
        {
            "mode": "action",
            "action": {
                "action_id": "verify_google_identity",
                "target": {"connection": {"secrets": {"access_token": "access-token"}}},
            },
        }
    )

    assert response == {
        "ok": False,
        "result": {},
        "error_code": "provider_unavailable",
        "error": "Google account identity is unavailable",
        "errors": [
            {
                "code": "provider_unavailable",
                "message": "Google account identity is unavailable",
            }
        ],
    }


def test_verify_identity_fails_closed_on_extra_or_missing_secrets() -> None:
    import identity

    for secrets in (
        {},
        {"access_token": "token", "refresh_token": "must-not-arrive"},
    ):
        response = identity.handle_verify_identity(
            {
                "mode": "action",
                "action": {
                    "action_id": "verify_google_identity",
                    "target": {"connection": {"secrets": secrets}},
                },
            }
        )
        assert response["ok"] is False
        assert response["error_code"] == "invalid_payload"
        assert "must-not-arrive" not in repr(response)


def test_verify_identity_does_not_read_legacy_top_level_target() -> None:
    import identity

    response = identity.handle_verify_identity(
        {
            "mode": "action",
            "action": {"action_id": "verify_google_identity"},
            "target": {"connection": {"secrets": {"access_token": "legacy-token"}}},
        }
    )

    assert response["ok"] is False
    assert response["error_code"] == "invalid_payload"
    assert "legacy-token" not in repr(response)
