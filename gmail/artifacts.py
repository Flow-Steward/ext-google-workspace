from __future__ import annotations

import base64
import codecs
import hashlib
import os
import re
from collections.abc import Callable, Mapping
from typing import Any

from flowsteward_extension_sdk import (
    ArtifactAccessError,
    find_artifact_descriptor,
    write_artifact_stream,
)
from flowsteward_extension_sdk.http import verified_platform_grant_signature

from .errors import GmailExtensionError
from .messages import find_attachment_part
from .transport import MAX_ATTACHMENT_BYTES, GmailTransport

_TOKEN_ATTACHMENT_BIN = "attachment.bin"
_MESSAGE_THE_APPROVED_ARTIFACT_OUTPUT_IS_UNAVAILABLE = "The approved artifact output is unavailable"
_MESSAGE_ATTACHMENT_TYPE_IS_NOT_ALLOWED = "Attachment type is not allowed"

BINDING_KEY = "attachment_artifact_handle"
GRANT_CONTENT_TYPE = "application/octet-stream"
_ALLOWED_TYPES = {
    ".pdf": {"application/pdf"},
    ".csv": {"text/csv", "application/csv"},
    ".txt": {"text/plain"},
    ".xls": {
        "application/vnd.ms-excel",
        "application/msexcel",
        "application/x-msexcel",
    },
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
}
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def get_attachment(
    transport: GmailTransport,
    root_payload: dict[str, Any],
    operation_input: Mapping[str, Any],
    *,
    artifact_writer: Callable[..., dict[str, Any]] = write_artifact_stream,
) -> dict[str, Any]:
    grant_limit = _output_grant_limit(root_payload)
    message_id = _identifier(operation_input.get("message_id"), "message_id")
    attachment_id = _identifier(operation_input.get("attachment_id"), "attachment_id")
    message = transport.message(message_id)
    part = find_attachment_part(message, attachment_id)
    body = part.get("body") if isinstance(part.get("body"), Mapping) else {}
    effective_limit = min(MAX_ATTACHMENT_BYTES, grant_limit)
    declared_size = body.get("size")
    if declared_size is not None:
        if (
            isinstance(declared_size, bool)
            or not isinstance(declared_size, int)
            or declared_size < 0
        ):
            raise GmailExtensionError(
                "provider_unavailable", "Gmail returned an invalid attachment size"
            )
        if declared_size > effective_limit:
            raise GmailExtensionError("attachment_too_large", "The Gmail attachment is too large")
    encoded = body.get("data")
    if not encoded:
        provider_attachment_id = str(body.get("attachmentId") or "")
        if not provider_attachment_id:
            raise GmailExtensionError("attachment_not_found", "The Gmail attachment was not found")
        encoded = transport.attachment(message_id, provider_attachment_id).get("data")
    encoded_text = str(encoded or "")
    filename = _safe_filename(str(part.get("filename") or _TOKEN_ATTACHMENT_BIN))
    content_type = str(part.get("mimeType") or "application/octet-stream").lower()
    digest, decoded_size = _validate_decoded_stream(
        filename,
        content_type,
        encoded_text,
        effective_limit,
    )
    try:
        written = artifact_writer(
            root_payload,
            _iter_decoded_base64url(encoded_text, effective_limit),
            binding_key=BINDING_KEY,
            content_type=GRANT_CONTENT_TYPE,
        )
    except Exception as exc:
        raise GmailExtensionError(
            "artifact_output_unavailable", _MESSAGE_THE_APPROVED_ARTIFACT_OUTPUT_IS_UNAVAILABLE
        ) from exc
    handle = written.get("artifact_handle") if isinstance(written, Mapping) else None
    if not isinstance(handle, str) or not handle:
        raise GmailExtensionError(
            "artifact_output_unavailable", _MESSAGE_THE_APPROVED_ARTIFACT_OUTPUT_IS_UNAVAILABLE
        )
    return {
        "attachment_artifact_handle": handle,
        "attachment_metadata": {
            "message_id": message_id,
            "attachment_id": attachment_id,
            "original_filename": filename,
            "content_type": content_type,
            "size_bytes": decoded_size,
            "sha256": digest,
        },
    }


def _output_grant_limit(payload: dict[str, Any]) -> int:
    try:
        descriptor = find_artifact_descriptor(payload, binding_key=BINDING_KEY, role="output")
        access = descriptor.get("access")
        if not isinstance(access, dict):
            raise ArtifactAccessError("missing access")
        max_size = access.get("max_size_bytes")
        if (
            descriptor.get("role") != "output"
            or descriptor.get("binding_key") != BINDING_KEY
            or descriptor.get("content_type") != GRANT_CONTENT_TYPE
            or access.get("transport") != "presigned_url"
            or access.get("mode") not in {"write", "read_write"}
            or not isinstance(max_size, int)
            or isinstance(max_size, bool)
            or max_size <= 0
            or not verified_platform_grant_signature(descriptor, access)
        ):
            raise ArtifactAccessError("invalid grant")
        return max_size
    except (ArtifactAccessError, TypeError, ValueError) as exc:
        raise GmailExtensionError(
            "artifact_output_unavailable", _MESSAGE_THE_APPROVED_ARTIFACT_OUTPUT_IS_UNAVAILABLE
        ) from exc


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 512 or any(ord(char) < 32 for char in text):
        raise GmailExtensionError("invalid_payload", f"{field} is invalid")
    return text


def _safe_filename(value: str) -> str:
    name = os.path.basename(value.replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    if not name:
        name = _TOKEN_ATTACHMENT_BIN
    encoded = name.encode("utf-8")[:240]
    return encoded.decode("utf-8", errors="ignore") or _TOKEN_ATTACHMENT_BIN


def _validate_decoded_stream(
    filename: str,
    content_type: str,
    encoded: str,
    max_bytes: int,
) -> tuple[str, int]:
    suffix = next((item for item in _ALLOWED_TYPES if filename.lower().endswith(item)), "")
    if not suffix or content_type not in _ALLOWED_TYPES[suffix]:
        raise GmailExtensionError(
            "attachment_type_disallowed", _MESSAGE_ATTACHMENT_TYPE_IS_NOT_ALLOWED
        )
    digest = hashlib.sha256()
    decoded_size = 0
    prefix = bytearray()
    text_decoder = codecs.getincrementaldecoder("utf-8")() if suffix in {".csv", ".txt"} else None
    try:
        for chunk in _iter_decoded_base64url(encoded, max_bytes):
            digest.update(chunk)
            decoded_size += len(chunk)
            if len(prefix) < 16:
                prefix.extend(chunk[: 16 - len(prefix)])
            if text_decoder is not None:
                if b"\x00" in chunk:
                    raise UnicodeDecodeError("utf-8", chunk, 0, 1, "NUL is not text")
                text_decoder.decode(chunk, final=False)
        if text_decoder is not None:
            text_decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise GmailExtensionError(
            "attachment_type_disallowed", _MESSAGE_ATTACHMENT_TYPE_IS_NOT_ALLOWED
        ) from exc
    valid = {
        ".pdf": bytes(prefix).startswith(b"%PDF-"),
        ".xls": bytes(prefix).startswith(_OLE_MAGIC),
        ".xlsx": bytes(prefix).startswith(b"PK\x03\x04"),
        ".csv": True,
        ".txt": True,
    }[suffix]
    if not valid:
        raise GmailExtensionError(
            "attachment_type_disallowed", _MESSAGE_ATTACHMENT_TYPE_IS_NOT_ALLOWED
        )
    return digest.hexdigest(), decoded_size


def _iter_decoded_base64url(encoded: str, max_bytes: int):
    text = str(encoded or "")
    chunk_chars = 64 * 1024
    decoded_size = 0
    try:
        for offset in range(0, len(text), chunk_chars):
            block = text[offset : offset + chunk_chars]
            if offset + chunk_chars >= len(text):
                block += "=" * (-len(block) % 4)
            decoded = base64.b64decode(
                block.replace("-", "+").replace("_", "/"),
                validate=True,
            )
            decoded_size += len(decoded)
            if decoded_size > max_bytes:
                raise GmailExtensionError(
                    "attachment_too_large", "The Gmail attachment is too large"
                )
            if decoded:
                yield decoded
    except ValueError as exc:
        raise GmailExtensionError(
            "provider_unavailable", "Gmail returned invalid base64 data"
        ) from exc
