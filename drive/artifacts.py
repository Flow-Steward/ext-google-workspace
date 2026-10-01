from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from flowsteward_extension_sdk import (
    ArtifactAccessError,
    find_artifact_descriptor,
    write_artifact_stream,
)
from flowsteward_extension_sdk.http import verified_platform_grant_signature

from .config import artifact_max_bytes
from .errors import DriveExtensionError
from .validation import identifier

_TOKEN_APPLICATION_PDF = "application/pdf"
_MESSAGE_THE_APPROVED_ARTIFACT_OUTPUT_IS_UNAVAILABLE = "The approved artifact output is unavailable"
_MESSAGE_GOOGLE_DRIVE_RETURNED_INVALID_METADATA = "Google Drive returned invalid metadata"

MAX_DRIVE_FILE_BYTES = 200 * 1024 * 1024
DOWNLOAD_BINDING_KEY = "drive_download_artifact"
GRANT_CONTENT_TYPE = "application/octet-stream"
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXPORT_PAIRS = {
    "application/vnd.google-apps.document": {_TOKEN_APPLICATION_PDF: ".pdf"},
    "application/vnd.google-apps.presentation": {_TOKEN_APPLICATION_PDF: ".pdf"},
    "application/vnd.google-apps.spreadsheet": {
        _TOKEN_APPLICATION_PDF: ".pdf",
        XLSX_MIME_TYPE: ".xlsx",
    },
}
_MIME_RE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")


def validated_download_input(value: Any) -> dict[str, Any]:
    allowed = {"connection_ref", "file_id", "export_mime_type"}
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise DriveExtensionError("invalid_payload", "Drive download input is invalid")
    result = {
        "connection_ref": identifier(value.get("connection_ref"), "connection_ref"),
        "file_id": identifier(value.get("file_id"), "file_id"),
    }
    if "export_mime_type" in value:
        result["export_mime_type"] = _mime_type(value.get("export_mime_type"), provider=False)
    return result


def download_drive_file(
    transport: Any,
    root_payload: dict[str, Any],
    operation_input: Mapping[str, Any],
    *,
    artifact_writer: Callable[..., dict[str, Any]] = write_artifact_stream,
) -> dict[str, Any]:
    normalized = validated_download_input(operation_input)
    grant_limit = output_grant_limit(root_payload)
    effective_limit = min(artifact_max_bytes(), grant_limit)
    file_id = normalized["file_id"]
    metadata = _file_metadata(transport.file_metadata(file_id), expected_id=file_id)
    native = metadata["mime_type"].startswith(GOOGLE_NATIVE_PREFIX)
    requested_export = normalized.get("export_mime_type")
    if native:
        suffix = EXPORT_PAIRS.get(metadata["mime_type"], {}).get(requested_export or "")
        if not suffix:
            raise DriveExtensionError(
                "google_unsupported_export", "This Google file cannot be exported in that format"
            )
        output_mime = requested_export
        filename = _export_filename(metadata["name"], suffix)
        chunks = transport.stream_file_export(file_id, output_mime, max_bytes=effective_limit)
        expected_size = None
    else:
        if requested_export is not None:
            raise DriveExtensionError(
                "google_unsupported_export", "Binary Drive files cannot use export_mime_type"
            )
        output_mime = metadata["mime_type"]
        filename = safe_filename(metadata["name"])
        expected_size = metadata["size"]
        if expected_size is None:
            raise DriveExtensionError("provider_unavailable", "Google Drive omitted the file size")
        if expected_size > effective_limit:
            raise DriveExtensionError("drive_file_too_large", "The Drive file is too large")
        chunks = transport.stream_file_media(file_id, max_bytes=effective_limit)

    counted, state = _bounded_chunks(chunks, limit=effective_limit, expected_size=expected_size)
    try:
        written = artifact_writer(
            root_payload,
            counted,
            binding_key=DOWNLOAD_BINDING_KEY,
            content_type=GRANT_CONTENT_TYPE,
        )
    except DriveExtensionError:
        raise
    except Exception as exc:
        raise DriveExtensionError(
            "artifact_output_unavailable", _MESSAGE_THE_APPROVED_ARTIFACT_OUTPUT_IS_UNAVAILABLE
        ) from exc
    if not isinstance(written, Mapping):
        raise DriveExtensionError(
            "artifact_output_unavailable", _MESSAGE_THE_APPROVED_ARTIFACT_OUTPUT_IS_UNAVAILABLE
        )
    handle = written.get("artifact_handle")
    written_size = written.get("size_bytes")
    sha256 = written.get("sha256")
    if (
        not isinstance(handle, str)
        or not handle.strip()
        or isinstance(written_size, bool)
        or not isinstance(written_size, int)
        or written_size != state["size"]
        or (sha256 is not None and not isinstance(sha256, str))
    ):
        raise DriveExtensionError(
            "artifact_output_unavailable", _MESSAGE_THE_APPROVED_ARTIFACT_OUTPUT_IS_UNAVAILABLE
        )
    return {
        "artifact_handle": handle.strip(),
        "filename": filename,
        "mime_type": output_mime,
        "size": state["size"],
        "sha256": sha256 or None,
        "file_id": file_id,
        "modified_time": metadata["modified_time"],
    }


def output_grant_limit(payload: dict[str, Any]) -> int:
    try:
        descriptor = find_artifact_descriptor(
            payload, binding_key=DOWNLOAD_BINDING_KEY, role="output"
        )
        access = descriptor.get("access")
        max_size = access.get("max_size_bytes") if isinstance(access, Mapping) else None
        if (
            descriptor.get("role") != "output"
            or descriptor.get("binding_key") != DOWNLOAD_BINDING_KEY
            or descriptor.get("content_type") != GRANT_CONTENT_TYPE
            or not isinstance(access, Mapping)
            or access.get("transport") != "presigned_url"
            or access.get("mode") not in {"write", "read_write"}
            or isinstance(max_size, bool)
            or not isinstance(max_size, int)
            or max_size <= 0
            or not verified_platform_grant_signature(descriptor, dict(access))
            or not scope_matches(payload, descriptor)
        ):
            raise ArtifactAccessError("invalid output grant")
        return max_size
    except (ArtifactAccessError, TypeError, ValueError) as exc:
        raise DriveExtensionError(
            "artifact_output_unavailable", _MESSAGE_THE_APPROVED_ARTIFACT_OUTPUT_IS_UNAVAILABLE
        ) from exc


def scope_matches(payload: Mapping[str, Any], descriptor: Mapping[str, Any]) -> bool:
    host = payload.get("host")
    host_scope = host.get("scope") if isinstance(host, Mapping) else None
    grant_scope = descriptor.get("scope")
    if not isinstance(host_scope, Mapping) or not isinstance(grant_scope, Mapping):
        return False
    for field in ("account_id", "project_id"):
        expected = str(host_scope.get(field) or "").strip()
        actual = str(grant_scope.get(field) or "").strip()
        if not expected or actual != expected:
            return False
    return True


def safe_filename(value: Any) -> str:
    if not isinstance(value, str):
        raise DriveExtensionError(
            "provider_unavailable", _MESSAGE_GOOGLE_DRIVE_RETURNED_INVALID_METADATA
        )
    name = os.path.basename(value.replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    encoded = name.encode("utf-8")[:255]
    normalized = encoded.decode("utf-8", errors="ignore").strip()
    if not normalized:
        raise DriveExtensionError(
            "provider_unavailable", _MESSAGE_GOOGLE_DRIVE_RETURNED_INVALID_METADATA
        )
    return normalized


def _export_filename(name: str, suffix: str) -> str:
    normalized = safe_filename(name)
    if normalized.lower().endswith(suffix):
        return normalized
    encoded = normalized.encode("utf-8")[: 255 - len(suffix)]
    stem = encoded.decode("utf-8", errors="ignore").rstrip(".") or "export"
    return f"{stem}{suffix}"


def _file_metadata(value: Any, *, expected_id: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - {
        "id",
        "name",
        "mimeType",
        "size",
        "modifiedTime",
        "webViewLink",
    }:
        raise DriveExtensionError(
            "provider_unavailable", _MESSAGE_GOOGLE_DRIVE_RETURNED_INVALID_METADATA
        )
    if value.get("id") != expected_id:
        raise DriveExtensionError(
            "provider_unavailable", _MESSAGE_GOOGLE_DRIVE_RETURNED_INVALID_METADATA
        )
    name = value.get("name")
    if not isinstance(name, str) or not name or len(name) > 1024:
        raise DriveExtensionError(
            "provider_unavailable", _MESSAGE_GOOGLE_DRIVE_RETURNED_INVALID_METADATA
        )
    mime_type = _mime_type(value.get("mimeType"), provider=True)
    raw_size = value.get("size")
    size = None
    if raw_size is not None:
        if not isinstance(raw_size, str) or not raw_size.isdigit():
            raise DriveExtensionError(
                "provider_unavailable", _MESSAGE_GOOGLE_DRIVE_RETURNED_INVALID_METADATA
            )
        size = int(raw_size)
        if size < 0:
            raise DriveExtensionError(
                "provider_unavailable", _MESSAGE_GOOGLE_DRIVE_RETURNED_INVALID_METADATA
            )
    modified = value.get("modifiedTime")
    if modified is not None and (
        not isinstance(modified, str) or not modified or len(modified) > 64
    ):
        raise DriveExtensionError(
            "provider_unavailable", _MESSAGE_GOOGLE_DRIVE_RETURNED_INVALID_METADATA
        )
    return {"name": name, "mime_type": mime_type, "size": size, "modified_time": modified}


def _mime_type(value: Any, *, provider: bool) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > 255 or _MIME_RE.fullmatch(text) is None:
        code = "provider_unavailable" if provider else "invalid_payload"
        raise DriveExtensionError(code, "Drive MIME type is invalid")
    return text


def _bounded_chunks(chunks: Iterable[bytes], *, limit: int, expected_size: int | None):
    state = {"size": 0}

    def iterator():
        for raw in chunks:
            if not isinstance(raw, (bytes, bytearray, memoryview)):
                raise DriveExtensionError(
                    "provider_unavailable", "Google Drive returned an invalid byte stream"
                )
            chunk = bytes(raw)
            if not chunk:
                continue
            state["size"] += len(chunk)
            if state["size"] > limit:
                raise DriveExtensionError("drive_file_too_large", "The Drive file is too large")
            yield chunk
        if expected_size is not None and state["size"] != expected_size:
            raise DriveExtensionError(
                "google_write_incomplete", "Google Drive returned an incomplete file"
            )

    return iterator(), state


__all__ = ["MAX_DRIVE_FILE_BYTES", "download_drive_file", "validated_download_input"]
