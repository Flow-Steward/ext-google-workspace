from __future__ import annotations

import io
from urllib.error import HTTPError

from gmail.errors import GmailExtensionError
from gmail.mutations import delete_messages, move_messages, set_message_flags
from gmail.transport import GmailTransport


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.trash_outcomes: dict[str, str] = {}

    def labels(self):
        self.calls.append(("labels",))
        return {
            "labels": [
                {"id": "INBOX", "type": "system"},
                {"id": "custom", "type": "user"},
            ]
        }

    def batch_modify(self, ids, add, remove):
        self.calls.append(("batch_modify", ids, add, remove))

    def trash(self, message_id):
        self.calls.append(("trash", message_id))
        outcome = self.trash_outcomes.get(message_id)
        if outcome == "failed":
            raise GmailExtensionError("message_not_found", "not found")
        if outcome == "ambiguous":
            raise GmailExtensionError("timeout_unknown", "timeout", ambiguous=True)


def test_test_mode_validates_and_suppresses_before_provider_access() -> None:
    transport = FakeTransport()

    result = set_message_flags(
        transport,
        {"message_ids": ["a", "a", "b"], "seen": True},
        test_mode=True,
    )

    assert transport.calls == []
    assert result == {
        "results": [],
        "external_effect_status": "suppressed",
        "definitely_no_external_effect": True,
    }


def test_move_lists_labels_once_then_uses_one_batch_modify() -> None:
    transport = FakeTransport()

    result = move_messages(
        transport,
        {
            "message_ids": ["a", "b"],
            "source_label_id": "INBOX",
            "target_label_id": "custom",
        },
        test_mode=False,
    )

    assert transport.calls == [
        ("labels",),
        ("batch_modify", ["a", "b"], ["custom"], ["INBOX"]),
    ]
    assert result["external_effect_status"] == "succeeded"
    assert [row["status"] for row in result["results"]] == ["succeeded", "succeeded"]


def test_batch_ambiguity_marks_every_submitted_id_timeout_unknown() -> None:
    transport = FakeTransport()

    def ambiguous(*_args):
        raise GmailExtensionError("timeout_unknown", "timeout", ambiguous=True)

    transport.batch_modify = ambiguous
    result = set_message_flags(
        transport,
        {"message_ids": ["a", "b"], "flagged": True},
        test_mode=False,
    )

    assert result["external_effect_status"] == "timeout_unknown"
    assert result["definitely_no_external_effect"] is False
    assert [row["status"] for row in result["results"]] == [
        "timeout_unknown",
        "timeout_unknown",
    ]


def test_sequential_trash_preserves_known_results_and_stops_after_ambiguity() -> None:
    transport = FakeTransport()
    transport.trash_outcomes = {"b": "failed", "c": "ambiguous"}

    result = delete_messages(
        transport,
        {"message_ids": ["a", "b", "c", "d"]},
        test_mode=False,
    )

    assert transport.calls == [("trash", "a"), ("trash", "b"), ("trash", "c")]
    assert result == {
        "results": [
            {"message_id": "a", "status": "succeeded"},
            {"message_id": "b", "status": "failed"},
            {"message_id": "c", "status": "timeout_unknown"},
            {"message_id": "d", "status": "not_attempted_after_timeout_unknown"},
        ],
        "external_effect_status": "timeout_unknown",
        "definitely_no_external_effect": False,
    }


def test_http_408_batch_modify_marks_every_submitted_id_timeout_unknown() -> None:
    def failed(request, **_kwargs):
        raise HTTPError(request.full_url, 408, "private timeout", {}, io.BytesIO(b"{}"))

    result = set_message_flags(
        GmailTransport("token", opener=failed),
        {"message_ids": ["a", "b"], "seen": True},
        test_mode=False,
    )

    assert result == {
        "results": [
            {"message_id": "a", "status": "timeout_unknown"},
            {"message_id": "b", "status": "timeout_unknown"},
        ],
        "external_effect_status": "timeout_unknown",
        "definitely_no_external_effect": False,
    }
