from __future__ import annotations

import pytest


class UploadTransport:
    def __init__(self, *, existing: dict | None = None, ambiguous: bool = False) -> None:
        self.existing = existing
        self.ambiguous = ambiguous
        self.marker_queries: list[tuple[str, str]] = []
        self.starts: list[tuple[dict, str, int]] = []
        self.uploads: list[tuple[str, bytes, int]] = []

    def find_by_operation_marker(self, operation_id: str, folder_id: str):
        self.marker_queries.append((operation_id, folder_id))
        return self.existing

    def start_resumable_upload(self, metadata: dict, mime_type: str, size: int) -> str:
        self.starts.append((metadata, mime_type, size))
        return "https://www.googleapis.com/upload/drive/v3/files?upload_id=session-private"

    def upload_resumable(self, session_url: str, chunks, *, size: int, mime_type: str):
        assert mime_type == "text/csv"
        body = b"".join(chunks)
        self.uploads.append((session_url, body, size))
        if self.ambiguous:
            from drive.errors import DriveExtensionError

            raise DriveExtensionError("timeout_unknown", "Google Drive upload outcome is unknown")
        return {
            "id": "uploaded-1",
            "name": "stock.csv",
            "mimeType": "text/csv",
            "size": str(len(body)),
            "webViewLink": "https://drive.google.com/file/d/uploaded-1/view",
        }


def _input(**overrides):
    value = {
        "connection_ref": "google-a",
        "artifact_handle": "artifact:source",
        "destination_folder_id": "folder-1",
        "filename": "stock.csv",
    }
    value.update(overrides)
    return value


def _payload(*, size: int = 7, mime_type: str = "text/csv") -> dict:
    return {
        "host": {"scope": {"account_id": "account-a", "project_id": "project-a"}},
        "runtime_context": {"external_effect_id": "effect-123"},
        "artifacts": {
            "inputs": [
                {
                    "artifact_id": "source",
                    "artifact_handle": "artifact:source",
                    "role": "input",
                    "binding_key": "drive_upload_artifact",
                    "content_type": mime_type,
                    "size_bytes": size,
                    "scope": {"account_id": "account-a", "project_id": "project-a"},
                    "access": {
                        "transport": "presigned_url",
                        "mode": "read",
                        "download_url": "https://artifacts.example.test/source",
                        "max_size_bytes": size,
                    },
                }
            ]
        },
    }


def test_resumable_upload_uses_grant_metadata_stream_and_stable_marker() -> None:
    from drive.uploads import upload_drive_file

    transport = UploadTransport()
    result = upload_drive_file(
        transport,
        _payload(),
        _input(),
        artifact_streamer=lambda *_args, **_kwargs: iter([b"one", b",two"]),
    )

    assert transport.marker_queries == [("effect-123", "folder-1")]
    assert transport.starts == [
        (
            {
                "name": "stock.csv",
                "parents": ["folder-1"],
                "appProperties": {"flow_steward_operation_id": "effect-123"},
            },
            "text/csv",
            7,
        )
    ]
    assert transport.uploads[0][1:] == (b"one,two", 7)
    assert result == {
        "file_id": "uploaded-1",
        "name": "stock.csv",
        "mime_type": "text/csv",
        "size": 7,
        "web_view_link": "https://drive.google.com/file/d/uploaded-1/view",
    }


@pytest.mark.parametrize(
    "filename", ["../stock.csv", "a/b.csv", r"a\b.csv", ".", "..", "bad\nname"]
)
def test_upload_rejects_unsafe_filename_before_grant_or_provider(filename) -> None:
    from drive.errors import DriveExtensionError
    from drive.uploads import upload_drive_file

    transport = UploadTransport()
    streamed = False

    def stream(*_args, **_kwargs):
        nonlocal streamed
        streamed = True
        return iter([])

    _raises_input_124_1 = _payload()
    _raises_input_124_2 = _input(filename=filename)
    with pytest.raises(DriveExtensionError) as exc_info:
        upload_drive_file(
            transport, _raises_input_124_1, _raises_input_124_2, artifact_streamer=stream
        )

    assert exc_info.value.code == "invalid_payload"
    assert not streamed
    assert transport.marker_queries == []


def test_upload_grant_is_authoritative_and_cross_project_fails_before_provider() -> None:
    from drive.errors import DriveExtensionError
    from drive.uploads import upload_drive_file

    payload = _payload(size=8)
    payload["artifacts"]["inputs"][0]["scope"]["project_id"] = "other-project"
    transport = UploadTransport()

    _raises_input_142_1 = _input()
    with pytest.raises(DriveExtensionError) as exc_info:
        upload_drive_file(transport, payload, _raises_input_142_1)

    assert exc_info.value.code == "artifact_input_unavailable"
    assert transport.marker_queries == []


@pytest.mark.parametrize("size", [True, -1, "7", 200 * 1024 * 1024 + 1])
def test_upload_rejects_malformed_or_oversized_grant_before_provider(size, monkeypatch) -> None:
    monkeypatch.delenv("FS_EXTENSION_ARTIFACT_MAX_BYTES", raising=False)
    from drive.errors import DriveExtensionError
    from drive.uploads import upload_drive_file

    payload = _payload()
    payload["artifacts"]["inputs"][0]["size_bytes"] = size
    transport = UploadTransport()
    _raises_input_158_1 = _input()
    with pytest.raises(DriveExtensionError) as exc_info:
        upload_drive_file(transport, payload, _raises_input_158_1)
    assert exc_info.value.code in {"artifact_input_unavailable", "drive_file_too_large"}
    assert transport.marker_queries == []


def test_upload_limit_is_operator_configurable_with_200_mib_default(monkeypatch) -> None:
    from drive.config import EXTENSION_ARTIFACT_MAX_BYTES_ENV, artifact_max_bytes
    from drive.uploads import input_artifact

    monkeypatch.delenv(EXTENSION_ARTIFACT_MAX_BYTES_ENV, raising=False)
    assert artifact_max_bytes() == 200 * 1024 * 1024

    configured = 2 * 1024 * 1024 * 1024
    monkeypatch.setenv(EXTENSION_ARTIFACT_MAX_BYTES_ENV, str(configured))
    payload = _payload(size=configured)
    assert input_artifact(payload, _input())["size"] == configured


def test_upload_timing_is_operator_configurable_and_bounded(monkeypatch) -> None:
    from drive.config import (
        UPLOAD_OPERATION_TIMEOUT_ENV,
        upload_timing,
    )

    monkeypatch.delenv(UPLOAD_OPERATION_TIMEOUT_ENV, raising=False)
    assert upload_timing() == (300.0, 30.0)

    monkeypatch.setenv(UPLOAD_OPERATION_TIMEOUT_ENV, "600")
    assert upload_timing() == (600.0, 30.0)

    monkeypatch.setenv(UPLOAD_OPERATION_TIMEOUT_ENV, "999999")
    assert upload_timing() == (3600.0, 30.0)


def test_upload_reconciles_existing_marker_without_reading_artifact_or_creating_duplicate() -> None:
    from drive.uploads import upload_drive_file

    existing = {
        "id": "existing-1",
        "name": "stock.csv",
        "mimeType": "text/csv",
        "size": "7",
        "webViewLink": "https://drive.google.com/file/d/existing-1/view",
    }
    transport = UploadTransport(existing=existing)

    result = upload_drive_file(
        transport,
        _payload(),
        _input(),
        artifact_streamer=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reconciliation must not read the artifact")
        ),
    )

    assert result["file_id"] == "existing-1"
    assert transport.starts == []
    assert transport.uploads == []


def test_upload_accepts_provider_normalized_mime_type_after_reconciliation() -> None:
    from drive.uploads import upload_drive_file

    existing = {
        "id": "existing-1",
        "name": "stock.csv",
        "mimeType": "text/csv",
        "size": "7",
        "webViewLink": "https://drive.google.com/file/d/existing-1/view",
    }
    result = upload_drive_file(
        UploadTransport(existing=existing),
        _payload(mime_type="application/octet-stream"),
        _input(),
        artifact_streamer=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reconciliation must not read the artifact")
        ),
    )

    assert result["mime_type"] == "text/csv"
    assert result["size"] == 7


@pytest.mark.parametrize(
    "web_view_link",
    ["", "http://drive.google.com/file/d/existing-1/view", "https://example.test/existing-1"],
)
def test_upload_rejects_untrusted_reconciled_web_view_link(web_view_link: str) -> None:
    from drive.errors import DriveExtensionError
    from drive.uploads import upload_drive_file

    existing = {
        "id": "existing-1",
        "name": "stock.csv",
        "mimeType": "text/csv",
        "size": "7",
        "webViewLink": web_view_link,
    }
    _raises_input_257_1 = UploadTransport(existing=existing)
    _raises_input_257_2 = _payload()
    _raises_input_257_3 = _input()

    def _raises_input_257_4(*_args, **_kwargs):
        return (_ for _ in ()).throw(AssertionError("reconciliation must not read the artifact"))

    with pytest.raises(DriveExtensionError) as exc_info:
        upload_drive_file(
            _raises_input_257_1,
            _raises_input_257_2,
            _raises_input_257_3,
            artifact_streamer=_raises_input_257_4,
        )

    assert exc_info.value.code == "provider_unavailable"


def test_ambiguous_upload_is_not_replayed_and_reports_unknown_effect() -> None:
    from drive.errors import DriveExtensionError
    from drive.uploads import upload_drive_file

    transport = UploadTransport(ambiguous=True)
    _raises_input_275_1 = _payload()
    _raises_input_275_2 = _input()

    def _raises_input_275_3(*_args, **_kwargs):
        return iter([b"one,two"])

    with pytest.raises(DriveExtensionError) as exc_info:
        upload_drive_file(
            transport,
            _raises_input_275_1,
            _raises_input_275_2,
            artifact_streamer=_raises_input_275_3,
        )

    assert exc_info.value.code == "timeout_unknown"
    assert len(transport.starts) == 1
    assert len(transport.uploads) == 1
    assert transport.marker_queries == [
        ("effect-123", "folder-1"),
        ("effect-123", "folder-1"),
    ]


def test_ambiguous_upload_recovers_file_created_under_operation_marker() -> None:
    from drive.uploads import upload_drive_file

    recovered = {
        "id": "uploaded-1",
        "name": "stock.csv",
        "mimeType": "text/csv",
        "size": "7",
        "webViewLink": "https://drive.google.com/file/d/uploaded-1/view",
    }
    transport = UploadTransport(ambiguous=True)

    def find_after_upload(operation_id: str, folder_id: str):
        transport.marker_queries.append((operation_id, folder_id))
        return recovered if transport.uploads else None

    transport.find_by_operation_marker = find_after_upload
    result = upload_drive_file(
        transport,
        _payload(),
        _input(),
        artifact_streamer=lambda *_args, **_kwargs: iter([b"one,two"]),
    )

    assert result["file_id"] == "uploaded-1"
    assert len(transport.starts) == 1
    assert len(transport.uploads) == 1
    assert transport.marker_queries == [
        ("effect-123", "folder-1"),
        ("effect-123", "folder-1"),
    ]


def test_private_artifact_transport_failure_is_a_safe_input_error() -> None:
    from drive.errors import DriveExtensionError
    from drive.uploads import upload_drive_file
    from flowsteward_extension_sdk import PinnedPeerError

    def blocked_stream(*_args, **_kwargs):
        raise PinnedPeerError("private host canary")
        yield b""  # pragma: no cover

    _raises_input_334_1 = UploadTransport()
    _raises_input_334_2 = _payload()
    _raises_input_334_3 = _input()
    with pytest.raises(DriveExtensionError) as exc_info:
        upload_drive_file(
            _raises_input_334_1,
            _raises_input_334_2,
            _raises_input_334_3,
            artifact_streamer=blocked_stream,
        )

    assert exc_info.value.code == "artifact_input_unavailable"
    assert "canary" not in str(exc_info.value).lower()


def test_upload_test_mode_validates_handle_but_suppresses_before_token_and_provider(
    monkeypatch,
) -> None:
    from drive import operations

    monkeypatch.setattr(
        operations,
        "access_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no OAuth in test mode")),
    )
    monkeypatch.setattr(
        operations,
        "DriveTransport",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no provider in test mode")),
    )
    payload = _payload()
    payload.update(
        {
            "mode": "action",
            "runtime_context": {"test_mode": True, "external_effect_id": "effect-123"},
            "action": {
                "action_id": "upload_drive_file",
                "page_id": "workflow-builder",
                "component_id": "workflow-test-run",
                "context": {"workflow_id": "release-audit-workflow"},
                "input": _input(),
            },
        }
    )

    response = operations.handle_runtime(payload)

    assert response == {
        "ok": True,
        "result": {
            "test_mode_status": "suppressed",
            "external_effect_status": "suppressed",
            "definitely_no_external_effect": True,
        },
        "external_effect_status": "suppressed",
        "definitely_no_external_effect": True,
    }


def test_live_upload_requires_signed_artifact_grant_before_oauth(monkeypatch) -> None:
    from drive import operations

    monkeypatch.setattr(
        operations,
        "access_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("artifact grant validation must happen before OAuth")
        ),
    )
    payload = {
        "mode": "action",
        "runtime_context": {"external_effect_id": "effect-123"},
        "action": {
            "action_id": "upload_drive_file",
            "page_id": "workflow-builder",
            "component_id": "workflow-run",
            "context": {"workflow_id": "release-audit-workflow"},
            "input": _input(),
        },
    }

    response = operations.handle_runtime(payload)

    assert response["ok"] is False
    assert response["error_code"] == "artifact_input_unavailable"


def test_drive_timeout_response_uses_host_ambiguity_vocabulary() -> None:
    from drive import operations

    response = operations._error("timeout_unknown", "Outcome is unknown", ambiguous=True)

    assert response["external_effect_status"] == "timeout_unknown"
    assert response["definitely_no_external_effect"] is False
