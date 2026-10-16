"""The final extension_host_v2 execution scope, end to end through the dispatcher.

Every owner of a step (the workflow root, a Group, a For each) reaches the Drive
artifact operations; an envelope of another contract or with an invalid scope is
refused before OAuth, an artifact or Google is touched.
"""

from __future__ import annotations

import functools
import json
import subprocess
import sys
from typing import Any

import pytest
from conftest import (
    BUNDLE_ROOT,
    FOR_EACH_STEP_SCOPE,
    GROUP_STEP_SCOPE,
    HOST_CONTRACT_VERSION,
    ROOT_STEP_SCOPE,
)
from test_drive_artifacts import DownloadTransport, _writer
from test_drive_uploads import UploadTransport
from test_drive_uploads import _input as _upload_input
from test_drive_uploads import _payload as _upload_grant

SCOPES = pytest.mark.parametrize(
    "scope",
    [ROOT_STEP_SCOPE, GROUP_STEP_SCOPE, FOR_EACH_STEP_SCOPE],
    ids=["root", "group", "for_each"],
)


def _connection() -> dict[str, Any]:
    return {
        "connection": {
            "connection_id": "google-a",
            "connection_type_id": "google_workspace",
            "secrets": {"access_token": "selected-token"},
        }
    }


def _download_request(scope: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": HOST_CONTRACT_VERSION,
        "mode": "action",
        "runtime_context": dict(scope),
        "action": {
            "action_id": "download_drive_file",
            "input": {"connection_ref": "google-a", "file_id": "file-1"},
            "target": _connection(),
        },
    }


def _upload_request(scope: dict[str, Any]) -> dict[str, Any]:
    payload = _upload_grant()
    payload.update(
        {
            "contract_version": HOST_CONTRACT_VERSION,
            "mode": "action",
            "runtime_context": {**scope, "external_effect_id": "effect-123"},
            "action": {
                "action_id": "upload_drive_file",
                "input": _upload_input(),
                "target": _connection(),
            },
        }
    )
    return payload


@SCOPES
def test_a_drive_download_writes_its_artifact_for_every_owner(
    monkeypatch: pytest.MonkeyPatch, scope: dict[str, Any]
) -> None:
    import dispatcher
    from drive import artifacts, operations

    captured: list[bytes] = []
    transport = DownloadTransport(
        {
            "id": "file-1",
            "name": "report.csv",
            "mimeType": "text/csv",
            "size": "7",
            "modifiedTime": "2026-08-25T12:30:00Z",
        },
        [b"one", b",two"],
    )
    monkeypatch.setattr(artifacts, "output_grant_limit", lambda _payload: 1024)
    monkeypatch.setattr(operations, "access_token", lambda *_args, **_kwargs: "selected-token")
    monkeypatch.setattr(operations, "DriveTransport", lambda *_args, **_kwargs: transport)
    monkeypatch.setattr(
        operations,
        "download_drive_file",
        functools.partial(artifacts.download_drive_file, artifact_writer=_writer(captured)),
    )

    response = dispatcher.dispatch_runtime(_download_request(scope))

    assert response["ok"] is True, response
    assert response["result"]["artifact_handle"] == "artifact:download"
    assert captured == [b"one,two"]
    assert transport.media_calls == ["file-1"]


@SCOPES
def test_a_drive_upload_reads_its_artifact_for_every_owner(
    monkeypatch: pytest.MonkeyPatch, scope: dict[str, Any]
) -> None:
    import dispatcher
    from drive import operations, uploads

    transport = UploadTransport()
    reads: list[str | None] = []

    def artifact_streamer(*_args: Any, **_kwargs: Any) -> Any:
        reads.append(scope["step_execution_id"])
        return iter([b"one", b",two"])

    monkeypatch.setattr(operations, "access_token", lambda *_args, **_kwargs: "selected-token")
    monkeypatch.setattr(operations, "DriveTransport", lambda *_args, **_kwargs: transport)
    monkeypatch.setattr(
        operations,
        "upload_drive_file",
        functools.partial(uploads.upload_drive_file, artifact_streamer=artifact_streamer),
    )

    response = dispatcher.dispatch_runtime(_upload_request(scope))

    assert response["ok"] is True, response
    assert response["external_effect_status"] == "succeeded"
    assert response["result"]["file_id"] == "uploaded-1"
    assert reads == [scope["step_execution_id"]]
    assert transport.uploads[0][1:] == (b"one,two", 7)


@pytest.mark.parametrize(
    ("contract_version", "scope", "reason"),
    [
        ("extension_host_v1", FOR_EACH_STEP_SCOPE, "Unsupported host contract"),
        ("", FOR_EACH_STEP_SCOPE, "Unsupported host contract"),
        (
            HOST_CONTRACT_VERSION,
            {k: v for k, v in FOR_EACH_STEP_SCOPE.items() if k != "for_each_id"},
            "misses execution scope",
        ),
        (
            HOST_CONTRACT_VERSION,
            {**FOR_EACH_STEP_SCOPE, "group_id": "prepare_files"},
            "both a Group and a For each",
        ),
        (
            HOST_CONTRACT_VERSION,
            {**ROOT_STEP_SCOPE, "step_execution_id": None},
            "without its logical execution",
        ),
    ],
    ids=["v1", "no_contract", "missing_key", "two_owners", "step_without_execution"],
)
def test_a_foreign_or_incomplete_envelope_is_refused_before_any_effect(
    monkeypatch: pytest.MonkeyPatch,
    contract_version: str,
    scope: dict[str, Any],
    reason: str,
) -> None:
    import dispatcher
    from drive import operations

    touched: list[str] = []

    def forbidden(name: str) -> Any:
        def call(*_args: Any, **_kwargs: Any) -> Any:
            touched.append(name)
            raise AssertionError(f"{name} ran for a refused envelope")

        return call

    monkeypatch.setattr(operations, "access_token", forbidden("oauth"))
    monkeypatch.setattr(operations, "DriveTransport", forbidden("google"))
    monkeypatch.setattr(operations, "input_artifact", forbidden("artifact_grant"))
    monkeypatch.setattr(operations, "upload_drive_file", forbidden("artifact_read"))
    payload = _upload_request(ROOT_STEP_SCOPE)
    payload["contract_version"] = contract_version
    payload["runtime_context"] = {**scope, "external_effect_id": "effect-123"}

    response = dispatcher.dispatch_runtime(payload)

    assert response["ok"] is False
    assert response["error_code"] == "invalid_host_envelope"
    assert reason in response["error"]
    assert response["definitely_no_external_effect"] is True
    assert touched == []


def test_the_entrypoint_refuses_an_envelope_of_another_contract() -> None:
    payload = _download_request(ROOT_STEP_SCOPE)
    payload["contract_version"] = "extension_host_v1"

    process = subprocess.run(
        [sys.executable, "main.py"],
        cwd=str(BUNDLE_ROOT),
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        check=False,
    )
    response = json.loads(process.stdout.decode("utf-8"))

    assert process.returncode == 2
    assert response["error_code"] == "invalid_host_envelope"
    assert response["definitely_no_external_effect"] is True
