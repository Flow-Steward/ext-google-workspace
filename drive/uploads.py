from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from flowsteward_extension_sdk import (
    ArtifactAccessError,
    PinnedPeerError,
    find_artifact_descriptor,
    stream_artifact_bytes,
)

from .artifacts import scope_matches
from .config import artifact_max_bytes
from .errors import DriveExtensionError
from .validation import bounded_text, identifier, provider_web_view_link

UPLOAD_BINDING_KEY = "drive_upload_artifact"
_MIME_RE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")


def validated_upload_input(value: Any) -> dict[str, str]:
    allowed = {"connection_ref", "artifact_handle", "destination_folder_id", "filename"}
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise DriveExtensionError("invalid_payload", "Drive upload input is invalid")
    filename = value.get("filename")
    if (
        not isinstance(filename, str)
        or not 1 <= len(filename.encode("utf-8")) <= 255
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or any(ord(char) < 32 or ord(char) == 127 for char in filename)
        or filename != filename.strip()
    ):
        raise DriveExtensionError("invalid_payload", "filename must be a safe basename")
    return {
        "connection_ref": identifier(value.get("connection_ref"), "connection_ref"),
        "artifact_handle": bounded_text(
            value.get("artifact_handle"), field="artifact_handle", maximum=512
        ),
        "destination_folder_id": identifier(
            value.get("destination_folder_id"), "destination_folder_id"
        ),
        "filename": filename,
    }


def input_artifact(payload: dict[str, Any], operation_input: Mapping[str, Any]) -> dict[str, Any]:
    try:
        descriptor = find_artifact_descriptor(
            payload,
            artifact_id=str(operation_input.get("artifact_handle") or ""),
            binding_key=UPLOAD_BINDING_KEY,
            role="input",
        )
        access = descriptor.get("access")
        size = descriptor.get("size_bytes")
        max_size = access.get("max_size_bytes") if isinstance(access, Mapping) else None
        mime_type = descriptor.get("content_type")
        if (
            descriptor.get("role") != "input"
            or descriptor.get("binding_key") != UPLOAD_BINDING_KEY
            or not isinstance(access, Mapping)
            or access.get("transport") != "presigned_url"
            or access.get("mode") not in {"read", "read_write"}
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or isinstance(max_size, bool)
            or not isinstance(max_size, int)
            or max_size <= 0
            or size > max_size
            or not isinstance(mime_type, str)
            or len(mime_type) > 255
            or _MIME_RE.fullmatch(mime_type) is None
            or not scope_matches(payload, descriptor)
        ):
            raise ArtifactAccessError("invalid input grant")
        if size > artifact_max_bytes():
            raise DriveExtensionError("drive_file_too_large", "The Drive upload is too large")
        return {"size": size, "mime_type": mime_type}
    except DriveExtensionError:
        raise
    except (ArtifactAccessError, TypeError, ValueError) as exc:
        raise DriveExtensionError(
            "artifact_input_unavailable", "The approved input artifact is unavailable"
        ) from exc


def upload_drive_file(
    transport: Any,
    root_payload: dict[str, Any],
    operation_input: Mapping[str, Any],
    *,
    artifact_streamer: Callable[..., Iterable[bytes]] = stream_artifact_bytes,
) -> dict[str, Any]:
    normalized = validated_upload_input(operation_input)
    grant = input_artifact(root_payload, normalized)
    begin_upload = getattr(transport, "begin_upload_operation", None)
    if callable(begin_upload):
        begin_upload()
    operation_id = _operation_id(root_payload)
    folder_id = normalized["destination_folder_id"]
    existing = transport.find_by_operation_marker(operation_id, folder_id)
    if existing is not None:
        return _result(
            existing,
            expected_size=grant["size"],
        )
    metadata = {
        "name": normalized["filename"],
        "parents": [folder_id],
        "appProperties": {"flow_steward_operation_id": operation_id},
    }
    session_url = transport.start_resumable_upload(metadata, grant["mime_type"], grant["size"])
    try:
        source = artifact_streamer(
            root_payload,
            artifact_id=normalized["artifact_handle"],
            binding_key=UPLOAD_BINDING_KEY,
            chunk_size=1024 * 1024,
            timeout_seconds=30.0,
        )
        counted, state = _exact_size_chunks(source, expected_size=grant["size"])
        uploaded = transport.upload_resumable(
            session_url,
            counted,
            size=grant["size"],
            mime_type=grant["mime_type"],
        )
        if state["size"] != grant["size"]:
            raise DriveExtensionError(
                "google_write_incomplete", "The input artifact stream was incomplete"
            )
    except DriveExtensionError as exc:
        if exc.code != "timeout_unknown":
            raise
        try:
            reconciler = getattr(transport, "reconcile_ambiguous_upload", None)
            recovered = (
                reconciler(operation_id, folder_id, expected_size=grant["size"])
                if callable(reconciler)
                else transport.find_by_operation_marker(operation_id, folder_id)
            )
            if recovered is not None:
                return _result(
                    recovered,
                    expected_size=grant["size"],
                )
        except DriveExtensionError:
            pass
        raise
    except (ArtifactAccessError, PinnedPeerError) as exc:
        raise DriveExtensionError(
            "artifact_input_unavailable", "The approved input artifact is unavailable"
        ) from exc
    return _result(
        uploaded,
        expected_size=grant["size"],
    )


def _operation_id(payload: Mapping[str, Any]) -> str:
    runtime = payload.get("runtime_context")
    value = runtime.get("external_effect_id") if isinstance(runtime, Mapping) else None
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise DriveExtensionError(
            "invalid_payload", "A stable host external-effect operation ID is required"
        )
    return text


def _exact_size_chunks(chunks: Iterable[bytes], *, expected_size: int):
    state = {"size": 0}

    def iterator():
        for raw in chunks:
            if not isinstance(raw, (bytes, bytearray, memoryview)):
                raise DriveExtensionError(
                    "artifact_input_unavailable", "The input artifact stream is invalid"
                )
            chunk = bytes(raw)
            if not chunk:
                continue
            state["size"] += len(chunk)
            if state["size"] > expected_size:
                raise DriveExtensionError(
                    "artifact_input_unavailable", "The input artifact exceeds its signed size"
                )
            yield chunk
        if state["size"] != expected_size:
            raise DriveExtensionError(
                "artifact_input_unavailable", "The input artifact stream is incomplete"
            )

    return iterator(), state


def _result(value: Any, *, expected_size: int) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - {
        "id",
        "name",
        "mimeType",
        "size",
        "webViewLink",
    }:
        raise DriveExtensionError("provider_unavailable", "Google Drive returned invalid metadata")
    file_id = value.get("id")
    name = value.get("name")
    mime_type = value.get("mimeType")
    size = value.get("size")
    link = provider_web_view_link(value.get("webViewLink"), maximum=2048)
    if (
        not isinstance(file_id, str)
        or not 1 <= len(file_id) <= 512
        or not isinstance(name, str)
        or not 1 <= len(name) <= 1024
        or not isinstance(mime_type, str)
        or _MIME_RE.fullmatch(mime_type) is None
        or not isinstance(size, str)
        or not size.isdigit()
        or int(size) != expected_size
    ):
        raise DriveExtensionError("provider_unavailable", "Google Drive returned invalid metadata")
    return {
        "file_id": file_id,
        "name": name,
        "mime_type": mime_type,
        "size": expected_size,
        "web_view_link": link or None,
    }


__all__ = ["input_artifact", "upload_drive_file", "validated_upload_input"]
