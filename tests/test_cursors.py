from __future__ import annotations

import pytest
from gmail.errors import GmailExtensionError
from gmail.search import CURSOR_DOMAIN, decode_cursor, encode_cursor


def test_cursor_is_gmail_domain_bound_and_filter_bound() -> None:
    cursor = encode_cursor(
        page_token="private-token",
        connection_ref="gmail-a",
        label_id="INBOX",
        filters={"read": False},
        limit=50,
    )

    assert CURSOR_DOMAIN == "flowsteward.google-workspace.gmail.cursor.v1"
    assert (
        decode_cursor(
            cursor,
            connection_ref="gmail-a",
            label_id="INBOX",
            filters={"read": False},
            limit=50,
        )
        == "private-token"
    )
    with pytest.raises(GmailExtensionError) as exc_info:
        decode_cursor(
            cursor,
            connection_ref="gmail-b",
            label_id="INBOX",
            filters={"read": False},
            limit=50,
        )
    assert exc_info.value.code == "invalid_cursor"
