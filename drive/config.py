from __future__ import annotations

import os
from collections.abc import Mapping

EXTENSION_ARTIFACT_MAX_BYTES_ENV = "FS_EXTENSION_ARTIFACT_MAX_BYTES"
UPLOAD_OPERATION_TIMEOUT_ENV = "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS"

DEFAULT_EXTENSION_ARTIFACT_MAX_BYTES = 200 * 1024 * 1024
MIN_EXTENSION_ARTIFACT_MAX_BYTES = 1024 * 1024
MAX_EXTENSION_ARTIFACT_MAX_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_UPLOAD_OPERATION_TIMEOUT_SECONDS = 300
MIN_UPLOAD_OPERATION_TIMEOUT_SECONDS = 30
MAX_UPLOAD_OPERATION_TIMEOUT_SECONDS = 3600
DEFAULT_UPLOAD_REQUEST_TIMEOUT_SECONDS = 30


def artifact_max_bytes(environ: Mapping[str, str] | None = None) -> int:
    values = os.environ if environ is None else environ
    return _bounded_int(
        values.get("FS_EXTENSION_ARTIFACT_MAX_BYTES"),
        default=DEFAULT_EXTENSION_ARTIFACT_MAX_BYTES,
        minimum=MIN_EXTENSION_ARTIFACT_MAX_BYTES,
        maximum=MAX_EXTENSION_ARTIFACT_MAX_BYTES,
    )


def upload_timing(environ: Mapping[str, str] | None = None) -> tuple[float, float]:
    values = os.environ if environ is None else environ
    operation_timeout = _bounded_int(
        values.get("FS_EXTENSION_OPERATION_TIMEOUT_SECONDS"),
        default=DEFAULT_UPLOAD_OPERATION_TIMEOUT_SECONDS,
        minimum=MIN_UPLOAD_OPERATION_TIMEOUT_SECONDS,
        maximum=MAX_UPLOAD_OPERATION_TIMEOUT_SECONDS,
    )
    return float(operation_timeout), float(DEFAULT_UPLOAD_REQUEST_TIMEOUT_SECONDS)


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


__all__ = [
    "DEFAULT_UPLOAD_OPERATION_TIMEOUT_SECONDS",
    "EXTENSION_ARTIFACT_MAX_BYTES_ENV",
    "MAX_UPLOAD_OPERATION_TIMEOUT_SECONDS",
    "MIN_UPLOAD_OPERATION_TIMEOUT_SECONDS",
    "UPLOAD_OPERATION_TIMEOUT_ENV",
    "artifact_max_bytes",
    "upload_timing",
]
