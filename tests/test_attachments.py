from __future__ import annotations

import base64

import pytest
from gmail import artifacts
from gmail.errors import GmailExtensionError


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class AttachmentTransport:
    def __init__(self, data: bytes, declared_size: int | None = None) -> None:
        body = {"data": _encoded(data)}
        if declared_size is not None:
            body["size"] = declared_size
        self.payload = {
            "payload": {
                "parts": [
                    {
                        "partId": "part-1",
                        "filename": "report.pdf",
                        "mimeType": "application/pdf",
                        "body": body,
                    }
                ]
            }
        }

    def message(self, _message_id):
        return self.payload


class ProviderAttachmentTransport(AttachmentTransport):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.payload["payload"]["parts"][0]["body"] = {
            "attachmentId": "private-provider-id",
            "size": len(data),
        }
        self.encoded = _encoded(data)

    def attachment(self, message_id, provider_attachment_id):
        assert message_id == "m1"
        assert provider_attachment_id == "private-provider-id"
        return {"data": self.encoded}


def test_attachment_writes_bytes_only_to_artifact_and_returns_handle(monkeypatch) -> None:
    written: list[bytes] = []
    monkeypatch.setattr(artifacts, "_output_grant_limit", lambda _payload: 1024)

    result = artifacts.get_attachment(
        AttachmentTransport(b"%PDF-example"),
        {},
        {"message_id": "m1", "attachment_id": "part-1"},
        artifact_writer=lambda _root, chunks, **_kwargs: (
            written.append(b"".join(chunks)) or {"artifact_handle": "artifact:one"}
        ),
    )

    assert written == [b"%PDF-example"]
    assert result["attachment_artifact_handle"] == "artifact:one"
    assert "data" not in result
    assert "base64" not in repr(result).lower()


def test_provider_attachment_id_is_internal_and_public_part_id_is_preserved(monkeypatch) -> None:
    monkeypatch.setattr(artifacts, "_output_grant_limit", lambda _payload: 1024)

    result = artifacts.get_attachment(
        ProviderAttachmentTransport(b"%PDF-example"),
        {},
        {"message_id": "m1", "attachment_id": "part-1"},
        artifact_writer=lambda _root, chunks, **_kwargs: (
            b"".join(chunks) and {"artifact_handle": "artifact:one"}
        ),
    )

    assert result["attachment_metadata"]["attachment_id"] == "part-1"
    assert "private-provider-id" not in repr(result)


def test_legacy_xls_accepts_generic_imap_compatible_mime(monkeypatch) -> None:
    monkeypatch.setattr(artifacts, "_output_grant_limit", lambda _payload: 1024)
    transport = AttachmentTransport(artifacts._OLE_MAGIC + b"legacy-workbook")
    part = transport.payload["payload"]["parts"][0]
    part["filename"] = "stock-feed.xls"
    part["mimeType"] = "application/x-msexcel"

    result = artifacts.get_attachment(
        transport,
        {},
        {"message_id": "m1", "attachment_id": "part-1"},
        artifact_writer=lambda _root, chunks, **_kwargs: (
            b"".join(chunks) and {"artifact_handle": "artifact:xls"}
        ),
    )

    assert result["attachment_artifact_handle"] == "artifact:xls"
    assert result["attachment_metadata"]["content_type"] == "application/x-msexcel"


def test_declared_size_is_early_rejection_but_decoded_size_is_authoritative(
    monkeypatch,
) -> None:
    monkeypatch.setattr(artifacts, "_output_grant_limit", lambda _payload: 8)
    _raises_input_110_1 = AttachmentTransport(b"%PDF-x", declared_size=9)
    with pytest.raises(GmailExtensionError) as declared_error:
        artifacts.get_attachment(
            _raises_input_110_1, {}, {"message_id": "m1", "attachment_id": "part-1"}
        )
    assert declared_error.value.code == "attachment_too_large"

    _raises_input_118_1 = AttachmentTransport(b"%PDF-actual-too-large", declared_size=1)
    with pytest.raises(GmailExtensionError) as actual_error:
        artifacts.get_attachment(
            _raises_input_118_1, {}, {"message_id": "m1", "attachment_id": "part-1"}
        )
    assert actual_error.value.code == "attachment_too_large"


@pytest.mark.parametrize("declared_size", [True, False, -1, "12"])
def test_attachment_rejects_malformed_declared_provider_size(
    monkeypatch,
    declared_size,
) -> None:
    monkeypatch.setattr(artifacts, "_output_grant_limit", lambda _payload: 1024)

    _raises_input_134_1 = AttachmentTransport(b"%PDF-example", declared_size=declared_size)
    with pytest.raises(GmailExtensionError) as error:
        artifacts.get_attachment(
            _raises_input_134_1, {}, {"message_id": "m1", "attachment_id": "part-1"}
        )

    assert error.value.code == "provider_unavailable"
