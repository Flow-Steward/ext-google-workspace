from __future__ import annotations

import base64

import pytest
from gmail.errors import GmailExtensionError
from gmail.messages import (
    MAX_ATTACHMENTS,
    MAX_DEPTH,
    MAX_TEXT_BYTES,
    find_attachment_part,
    parse_message,
)


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _message_with_parts(parts: list[dict]) -> dict:
    return {
        "id": "m1",
        "threadId": "t1",
        "internalDate": "1724587200000",
        "labelIds": ["INBOX"],
        "payload": {"mimeType": "multipart/mixed", "headers": [], "parts": parts},
    }


def test_message_parser_exposes_closed_headers_and_bounded_attachment_metadata() -> None:
    message = {
        "id": "m1",
        "threadId": "t1",
        "internalDate": "1724587200000",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "Hello"},
                {"name": "Message-ID", "value": "<message@example.test>"},
                {"name": "X-Private", "value": "must not escape"},
            ],
            "parts": [
                {
                    "partId": "1",
                    "mimeType": "text/plain",
                    "body": {"data": _encoded(b"plain")},
                },
                {
                    "partId": "2",
                    "mimeType": "text/html",
                    "body": {"data": _encoded(b"<b>html</b>")},
                },
                {
                    "partId": "3",
                    "mimeType": "application/pdf",
                    "filename": "report.pdf",
                    "body": {"attachmentId": "private-id", "size": 12},
                },
            ],
        },
    }

    result = parse_message(message, include_raw_html=False)

    assert result["headers"] == {
        "date": "",
        "from": "",
        "to": "",
        "cc": "",
        "reply_to": "",
        "subject": "Hello",
        "rfc_message_id": "<message@example.test>",
    }
    assert result["internal_date"] == "2024-08-25T12:00:00Z"
    assert result["rfc_message_id"] == "<message@example.test>"
    assert result["plain_text"] == "plain"
    assert "raw_html" not in result
    assert result["attachments"] == [
        {
            "attachment_id": "3",
            "original_filename": "report.pdf",
            "content_type": "application/pdf",
            "size_bytes": 12,
        }
    ]
    assert "private-id" not in repr(result)

    metadata_only = parse_message(
        message,
        include_body=False,
        include_headers=False,
        include_attachment_metadata=False,
        include_raw_html=True,
    )
    assert "headers" not in metadata_only
    assert "plain_text" not in metadata_only
    assert "raw_html" not in metadata_only
    assert "attachments" not in metadata_only
    assert metadata_only["subject"] == "Hello"


@pytest.mark.parametrize("size", [True, -1, "12"])
def test_message_parser_rejects_invalid_attachment_size(size: object) -> None:
    message = _message_with_parts(
        [
            {
                "partId": "attachment-1",
                "mimeType": "application/pdf",
                "filename": "a.pdf",
                "body": {"size": size},
            }
        ]
    )

    with pytest.raises(GmailExtensionError) as exc_info:
        parse_message(message)

    assert exc_info.value.code == "provider_unavailable"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("labelIds", "INBOX"),
        ("headers", "From: private@example.test"),
        ("parts", "not-a-list"),
    ],
)
def test_message_parser_rejects_malformed_provider_collection_shapes(
    field: str,
    value: object,
) -> None:
    message = _message_with_parts([])
    if field == "labelIds":
        message[field] = value
    else:
        message["payload"][field] = value

    with pytest.raises(GmailExtensionError) as exc_info:
        parse_message(message)

    assert exc_info.value.code == "provider_unavailable"


def test_message_parser_enforces_aggregate_provider_header_input_bound() -> None:
    message = _message_with_parts([])
    message["payload"]["headers"] = [
        {"name": "X-Private", "value": "x" * (256 * 1024)},
        {"name": "Subject", "value": "still private"},
    ]

    with pytest.raises(GmailExtensionError) as exc_info:
        parse_message(message)

    assert exc_info.value.code == "response_too_large"


def test_message_parser_does_not_decode_unrequested_html() -> None:
    message = {
        "id": "m1",
        "internalDate": "1724587200000",
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "partId": "plain",
                    "mimeType": "text/plain",
                    "body": {"data": _encoded(b"small plain body")},
                },
                {
                    "partId": "html",
                    "mimeType": "text/html",
                    "body": {"data": _encoded(b"x" * (MAX_TEXT_BYTES + 1))},
                },
            ],
        },
    }

    result = parse_message(message, include_raw_html=False)

    assert result["plain_text"] == "small plain body"
    assert "raw_html" not in result


@pytest.mark.parametrize(
    "attachment_body",
    [
        {"data": _encoded(b"ATTACHMENT-CANARY"), "size": 17},
        {"attachmentId": "private-provider-id", "size": 17},
    ],
)
def test_hidden_text_attachment_never_becomes_message_body(
    attachment_body: dict[str, object],
) -> None:
    message = _message_with_parts(
        [
            {
                "partId": "attachment",
                "mimeType": "text/plain",
                "filename": "private.txt",
                "body": attachment_body,
            },
            {
                "partId": "body",
                "mimeType": "text/plain",
                "body": {"data": _encoded(b"actual message body")},
            },
        ]
    )

    result = parse_message(
        message,
        include_body=True,
        include_attachment_metadata=False,
    )

    assert result["plain_text"] == "actual message body"
    assert "ATTACHMENT-CANARY" not in repr(result)
    assert "private-provider-id" not in repr(result)
    assert "attachments" not in result


def test_attached_message_descendants_never_become_parent_message_body() -> None:
    message = _message_with_parts(
        [
            {
                "partId": "attached-message",
                "mimeType": "message/rfc822",
                "filename": "private.eml",
                "body": {"attachmentId": "private-rfc822", "size": 128},
                "parts": [
                    {
                        "partId": "attached-body",
                        "mimeType": "text/plain",
                        "body": {"data": _encoded(b"NESTED-ATTACHMENT-CANARY")},
                    }
                ],
            },
            {
                "partId": "parent-body",
                "mimeType": "text/plain",
                "body": {"data": _encoded(b"parent message body")},
            },
        ]
    )

    result = parse_message(
        message,
        include_body=True,
        include_attachment_metadata=False,
    )

    assert result["plain_text"] == "parent message body"
    assert "NESTED-ATTACHMENT-CANARY" not in repr(result)


def test_attachment_lookup_rejects_mime_tree_beyond_parser_depth_limit() -> None:
    root: dict = {"partId": "root", "parts": []}
    current = root
    for depth in range(MAX_DEPTH + 1):
        child = {"partId": f"part-{depth}", "parts": []}
        current["parts"] = [child]
        current = child
    current["partId"] = "deep-attachment"
    message = {"payload": root}

    with pytest.raises(GmailExtensionError) as exc_info:
        find_attachment_part(message, "deep-attachment")

    assert exc_info.value.code == "message_too_complex"


@pytest.mark.parametrize(
    ("attachment_count", "expected_count", "expected_truncated"),
    [
        (MAX_ATTACHMENTS - 1, MAX_ATTACHMENTS - 1, False),
        (MAX_ATTACHMENTS, MAX_ATTACHMENTS, False),
        (MAX_ATTACHMENTS + 1, MAX_ATTACHMENTS, True),
    ],
)
def test_attachment_metadata_reports_truncation_only_after_real_overflow(
    attachment_count: int,
    expected_count: int,
    expected_truncated: bool,
) -> None:
    message = {
        "id": "m1",
        "internalDate": "1724587200000",
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "partId": f"attachment-{index}",
                    "mimeType": "application/octet-stream",
                    "filename": f"attachment-{index}.bin",
                    "body": {"size": index},
                }
                for index in range(attachment_count)
            ],
        },
    }

    result = parse_message(message)

    assert len(result["attachments"]) == expected_count
    assert result["attachments_truncated"] is expected_truncated
