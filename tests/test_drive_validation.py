from __future__ import annotations

from datetime import UTC, datetime

import pytest


def _input(**overrides):
    value = {
        "connection_ref": "connection-1",
        "parent_folder_id": "folder-1",
        "name": "Supplier's stock",
        "name_match": "contains",
        "mime_types": ["text/csv", "application/vnd.ms-excel"],
        "modified_after": "2026-08-01T00:00:00Z",
        "modified_before": "2026-08-26T00:00:00Z",
        "trashed": False,
        "sort": "modified_time_desc",
        "limit": 50,
    }
    value.update(overrides)
    return value


def test_drive_search_validation_normalizes_only_the_closed_contract() -> None:
    from drive.validation import validated_search_input

    assert validated_search_input(_input()) == _input()


@pytest.mark.parametrize(
    "change",
    [
        {"q": "'root' in parents"},
        {"fields": "*"},
        {"pageToken": "raw-provider-token"},
        {"origin": "https://attacker.test"},
        {"limit": True},
        {"limit": 101},
        {"name": ""},
        {"name_match": "regex"},
        {"mime_types": "text/csv"},
        {"mime_types": ["text/*"]},
        {"mime_types": ["text/csv"] * 21},
        {"modified_after": "2026-08-01T00:00:00+02:00"},
        {"modified_after": "2026-09-01T00:00:00Z"},
        {"trashed": 0},
        {"sort": "name desc"},
        {"cursor": "x" * 8193},
        {"parent_folder_id": "https://drive.google.com/drive/folders/folder-1"},
    ],
)
def test_drive_search_validation_rejects_raw_controls_and_malformed_values(change) -> None:
    from drive.errors import DriveExtensionError
    from drive.validation import validated_search_input

    value = _input()
    value.update(change)
    with pytest.raises(DriveExtensionError) as exc_info:
        validated_search_input(value)

    assert exc_info.value.code == "invalid_payload"


def test_drive_timestamp_accepts_bounded_utc_fractional_seconds() -> None:
    from drive.validation import validated_search_input

    timestamp = (
        datetime(2026, 8, 1, 12, 30, 0, 123000, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    )
    result = validated_search_input(
        _input(modified_after=timestamp, modified_before="2026-08-26T00:00:00Z")
    )

    assert result["modified_after"] == timestamp
