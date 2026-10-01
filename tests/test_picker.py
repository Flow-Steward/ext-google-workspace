from __future__ import annotations

import pytest


def _payload(**configuration):
    return {
        "mode": "action",
        "action": {
            "action_id": "get_picker_bootstrap",
            "target": {
                "connection": {
                    "config": {
                        "oauth": {
                            "lifecycle_status": "connected",
                            "verified_email": "person@example.test",
                        }
                    },
                    "oauth_configuration": {
                        "client_id": "picker-client.apps.googleusercontent.com",
                        "picker_api_key": "AIza-picker-browser-key",
                        "picker_app_id": "123456789",
                        "picker_origin": "https://flow-steward.example.test",
                        **configuration,
                    },
                },
            },
        },
    }


def test_picker_bootstrap_projects_only_declared_browser_configuration() -> None:
    import picker

    payload = _payload()
    payload["action"]["target"]["connection"]["secrets"] = {
        "access_token": "must-not-leak",
        "refresh_token": "must-not-leak",
        "client_secret": "must-not-leak",
    }

    response = picker.handle_picker_bootstrap(payload)

    assert response == {
        "ok": True,
        "result": {
            "client_id": "picker-client.apps.googleusercontent.com",
            "developer_key": "AIza-picker-browser-key",
            "app_id": "123456789",
            "origin": "https://flow-steward.example.test",
            "email_hint": "person@example.test",
            "scope": "https://www.googleapis.com/auth/drive.file",
        },
    }
    assert "must-not-leak" not in repr(response)


@pytest.mark.parametrize(
    "configuration",
    [
        {"client_id": ""},
        {"picker_api_key": ""},
        {"picker_app_id": "not-numeric"},
        {"picker_origin": "http://flow-steward.example.test"},
        {"picker_origin": "https://user@example.test"},
        {"picker_origin": "https://example.test/path"},
        {"picker_origin": "https://example.test?query=1"},
        {"picker_origin": "https://[invalid"},
    ],
)
def test_picker_bootstrap_fails_closed_for_invalid_configuration(configuration) -> None:
    import picker

    response = picker.handle_picker_bootstrap(_payload(**configuration))

    assert response == {
        "ok": False,
        "result": {},
        "error_code": "invalid_payload",
        "error": "Google Picker configuration is unavailable",
        "errors": [
            {
                "code": "invalid_payload",
                "message": "Google Picker configuration is unavailable",
            }
        ],
    }


def test_picker_bootstrap_does_not_read_undeclared_legacy_locations() -> None:
    import picker

    response = picker.handle_picker_bootstrap(
        {
            "mode": "action",
            "action": {
                "action_id": "get_picker_bootstrap",
                "target": {
                    "connection": {
                        "verified_email": "person@example.test",
                        "config": {"oauth": {"connected_email": "legacy@example.test"}},
                        "oauth_metadata": {
                            "client_id": "legacy-client",
                            "picker_api_key": "legacy-key",
                            "picker_app_id": "123",
                            "picker_origin": "https://legacy.example.test",
                        },
                    },
                },
            },
        }
    )

    assert response["ok"] is False
    assert "legacy-client" not in repr(response)


def test_picker_bootstrap_does_not_read_legacy_top_level_target() -> None:
    import picker

    payload = _payload()
    payload["target"] = payload["action"].pop("target")

    response = picker.handle_picker_bootstrap(payload)

    assert response["ok"] is False
    assert response["error_code"] == "invalid_payload"
    assert "picker-client" not in repr(response)
