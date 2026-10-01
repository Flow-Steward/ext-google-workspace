from __future__ import annotations

import json
import subprocess
import sys

from conftest import BUNDLE_ROOT


def test_picker_consumes_canonical_action_target_through_real_entrypoint() -> None:
    payload = {
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
                    },
                }
            },
        },
    }

    completed = subprocess.run(
        [sys.executable, "main.py"],
        cwd=BUNDLE_ROOT,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "ok": True,
        "result": {
            "app_id": "123456789",
            "client_id": "picker-client.apps.googleusercontent.com",
            "developer_key": "AIza-picker-browser-key",
            "email_hint": "person@example.test",
            "origin": "https://flow-steward.example.test",
            "scope": "https://www.googleapis.com/auth/drive.file",
        },
    }
