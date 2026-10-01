from __future__ import annotations

import hashlib

import pytest


class DownloadTransport:
    def __init__(self, metadata: dict, chunks: list[bytes]) -> None:
        self.metadata = metadata
        self.chunks = chunks
        self.media_calls: list[str] = []
        self.stream_limits: list[int] = []
        self.export_calls: list[tuple[str, str]] = []

    def file_metadata(self, file_id: str) -> dict:
        assert file_id == "file-1"
        return self.metadata

    def stream_file_media(self, file_id: str, *, max_bytes: int):
        self.media_calls.append(file_id)
        self.stream_limits.append(max_bytes)
        yield from self.chunks

    def stream_file_export(self, file_id: str, mime_type: str, *, max_bytes: int):
        self.export_calls.append((file_id, mime_type))
        yield from self.chunks


def _input(**overrides):
    value = {"connection_ref": "google-a", "file_id": "file-1"}
    value.update(overrides)
    return value


def _writer(captured: list[bytes], *, handle: str = "artifact:download"):
    def write(_payload, chunks, **kwargs):
        body = b"".join(chunks)
        captured.append(body)
        return {
            "artifact_handle": handle,
            "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "content_type": kwargs["content_type"],
        }

    return write


def test_binary_download_streams_only_to_artifact_and_returns_safe_projection(monkeypatch) -> None:
    from drive import artifacts

    monkeypatch.setattr(artifacts, "output_grant_limit", lambda _payload: 1024)
    captured: list[bytes] = []
    transport = DownloadTransport(
        {
            "id": "file-1",
            "name": "../report.csv",
            "mimeType": "text/csv",
            "size": "7",
            "modifiedTime": "2026-08-25T12:30:00Z",
        },
        [b"one", b",two"],
    )

    result = artifacts.download_drive_file(
        transport,
        {},
        _input(),
        artifact_writer=_writer(captured),
    )

    assert captured == [b"one,two"]
    assert transport.media_calls == ["file-1"]
    assert transport.export_calls == []
    assert result == {
        "artifact_handle": "artifact:download",
        "filename": "report.csv",
        "mime_type": "text/csv",
        "size": 7,
        "sha256": hashlib.sha256(b"one,two").hexdigest(),
        "file_id": "file-1",
        "modified_time": "2026-08-25T12:30:00Z",
    }
    assert b"one,two" not in repr(result).encode()


@pytest.mark.parametrize(
    ("native_type", "export_type", "expected_filename"),
    [
        ("application/vnd.google-apps.document", "application/pdf", "Plan.pdf"),
        ("application/vnd.google-apps.presentation", "application/pdf", "Plan.pdf"),
        ("application/vnd.google-apps.spreadsheet", "application/pdf", "Plan.pdf"),
        (
            "application/vnd.google-apps.spreadsheet",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "Plan.xlsx",
        ),
    ],
)
def test_native_download_accepts_only_closed_export_pairs(
    monkeypatch, native_type, export_type, expected_filename
) -> None:
    from drive import artifacts

    monkeypatch.setattr(artifacts, "output_grant_limit", lambda _payload: 1024)
    transport = DownloadTransport(
        {"id": "file-1", "name": "Plan", "mimeType": native_type},
        [b"export"],
    )

    result = artifacts.download_drive_file(
        transport,
        {},
        _input(export_mime_type=export_type),
        artifact_writer=_writer([]),
    )

    assert transport.export_calls == [("file-1", export_type)]
    assert result["filename"] == expected_filename
    assert result["mime_type"] == export_type


@pytest.mark.parametrize(
    "metadata,operation_input",
    [
        (
            {"id": "file-1", "name": "a.csv", "mimeType": "text/csv", "size": "1"},
            _input(export_mime_type="application/pdf"),
        ),
        (
            {
                "id": "file-1",
                "name": "Plan",
                "mimeType": "application/vnd.google-apps.document",
            },
            _input(
                export_mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        ),
        (
            {
                "id": "file-1",
                "name": "Map",
                "mimeType": "application/vnd.google-apps.map",
            },
            _input(export_mime_type="application/pdf"),
        ),
    ],
)
def test_download_rejects_unsupported_exports_before_streaming(
    monkeypatch, metadata, operation_input
) -> None:
    from drive import artifacts
    from drive.errors import DriveExtensionError

    monkeypatch.setattr(artifacts, "output_grant_limit", lambda _payload: 1024)
    transport = DownloadTransport(metadata, [b"must-not-stream"])

    with pytest.raises(DriveExtensionError) as exc_info:
        artifacts.download_drive_file(transport, {}, operation_input)

    assert exc_info.value.code == "google_unsupported_export"
    assert transport.media_calls == []
    assert transport.export_calls == []


def test_download_uses_lower_grant_limit_and_actual_stream_is_authoritative(monkeypatch) -> None:
    from drive import artifacts
    from drive.errors import DriveExtensionError

    monkeypatch.setattr(artifacts, "output_grant_limit", lambda _payload: 5)
    transport = DownloadTransport(
        {"id": "file-1", "name": "a.bin", "mimeType": "application/octet-stream", "size": "4"},
        [b"123", b"456"],
    )

    _raises_input_178_1 = _input()
    _raises_input_178_2 = _writer([])
    with pytest.raises(DriveExtensionError) as exc_info:
        artifacts.download_drive_file(
            transport, {}, _raises_input_178_1, artifact_writer=_raises_input_178_2
        )

    assert exc_info.value.code == "drive_file_too_large"


def test_download_uses_shared_operator_artifact_limit(monkeypatch) -> None:
    from drive import artifacts

    configured = 2 * 1024 * 1024 * 1024
    monkeypatch.setenv("FS_EXTENSION_ARTIFACT_MAX_BYTES", str(configured))
    monkeypatch.setattr(artifacts, "output_grant_limit", lambda _payload: configured)
    transport = DownloadTransport(
        {"id": "file-1", "name": "a.bin", "mimeType": "application/octet-stream", "size": "1"},
        [b"x"],
    )

    artifacts.download_drive_file(
        transport,
        {},
        _input(),
        artifact_writer=_writer([]),
    )

    assert transport.stream_limits == [configured]


def test_download_rejects_truncated_binary_and_malformed_provider_metadata(monkeypatch) -> None:
    from drive import artifacts
    from drive.errors import DriveExtensionError

    monkeypatch.setattr(artifacts, "output_grant_limit", lambda _payload: 1024)
    truncated = DownloadTransport(
        {"id": "file-1", "name": "a.bin", "mimeType": "application/octet-stream", "size": "8"},
        [b"short"],
    )
    _raises_input_219_1 = _input()
    _raises_input_219_2 = _writer([])
    with pytest.raises(DriveExtensionError) as exc_info:
        artifacts.download_drive_file(
            truncated, {}, _raises_input_219_1, artifact_writer=_raises_input_219_2
        )
    assert exc_info.value.code == "google_write_incomplete"

    malformed = DownloadTransport(
        {"id": "other", "name": True, "mimeType": "application/octet-stream", "size": False},
        [],
    )
    _raises_input_227_1 = _input()
    _raises_input_227_2 = _writer([])
    with pytest.raises(DriveExtensionError) as exc_info:
        artifacts.download_drive_file(
            malformed, {}, _raises_input_227_1, artifact_writer=_raises_input_227_2
        )
    assert exc_info.value.code == "provider_unavailable"


def test_output_grant_scope_mismatch_fails_before_google_network(monkeypatch) -> None:
    from drive import artifacts
    from drive.errors import DriveExtensionError

    monkeypatch.setattr(artifacts, "verified_platform_grant_signature", lambda *_args: "sig")
    payload = {
        "host": {"scope": {"account_id": "account-a", "project_id": "project-a"}},
        "artifacts": {
            "outputs": [
                {
                    "artifact_id": "out",
                    "artifact_handle": "artifact:out",
                    "role": "output",
                    "binding_key": "drive_download_artifact",
                    "content_type": "application/octet-stream",
                    "scope": {"account_id": "account-a", "project_id": "project-b"},
                    "access": {
                        "transport": "presigned_url",
                        "mode": "write",
                        "max_size_bytes": 100,
                        "single_use": True,
                        "expires_at": "2999-01-01T00:00:00Z",
                        "platform_grant": {"version": 1, "signature": "sig"},
                    },
                }
            ]
        },
    }
    transport = DownloadTransport({}, [])

    _raises_input_262_1 = _input()
    with pytest.raises(DriveExtensionError) as exc_info:
        artifacts.download_drive_file(transport, payload, _raises_input_262_1)

    assert exc_info.value.code == "artifact_output_unavailable"
    assert transport.media_calls == []
