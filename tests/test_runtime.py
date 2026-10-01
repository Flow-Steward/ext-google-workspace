from __future__ import annotations

import importlib.util

import pytest
from gmail.errors import GmailExtensionError
from gmail.operations import handle_payload, handle_runtime
from gmail.search import compile_query, normalized_filters, search_messages


def _canonical_workflow_test_payload(
    action_id: str,
    operation_input: dict,
    *,
    connection_id: str,
    connection_type_id: str,
) -> dict:
    return {
        "contract_version": "extension_host_v1",
        "mode": "action",
        "host": {
            "contract_version": "extension_host_v1",
            "operation": "command",
            "scope": {"account_id": "account-a", "project_id": "project-a"},
            "principal": {"actor_type": "user", "actor_id": "user-a"},
        },
        "action": {
            "action_id": action_id,
            "page_id": "workflow-builder",
            "component_id": "workflow-test-run",
            "context": {
                "workflow_id": "release-audit-workflow",
                "job_id": "release-audit-job",
            },
            "input": operation_input,
            "target": {
                "connection": {
                    "connection_id": connection_id,
                    "connection_type_id": connection_type_id,
                    "secrets": {"access_token": "selected-test-token"},
                }
            },
        },
        "runtime_context": {"test_mode": True},
    }


def test_workflow_test_run_gmail_read_uses_canonical_envelope_and_returns_outputs(
    monkeypatch,
) -> None:
    import dispatcher
    from gmail.transport import GmailTransport

    calls = []

    def messages_list(_self, query):
        calls.append(query)
        return {"messages": [{"id": "message-1", "threadId": "thread-1"}]}

    monkeypatch.setattr(GmailTransport, "messages_list", messages_list)
    response = dispatcher.dispatch_runtime(
        _canonical_workflow_test_payload(
            "search_messages",
            {"connection_ref": "gmail-a", "label_id": "INBOX", "limit": 25},
            connection_id="gmail-a",
            connection_type_id="google_gmail_account",
        )
    )

    assert calls == [{"labelIds": "INBOX", "maxResults": 25}]
    assert response == {
        "ok": True,
        "result": {
            "messages": [{"message_id": "message-1", "thread_id": "thread-1"}],
            "next_cursor": None,
            "truncated": False,
        },
    }


def test_workflow_test_run_drive_read_uses_canonical_envelope_and_returns_outputs(
    monkeypatch,
) -> None:
    import dispatcher
    from drive import operations as drive_operations

    class TestDriveTransport:
        def __init__(self, token: str) -> None:
            assert token == "selected-test-token"

        def files_list(self, query):
            assert query["spaces"] == "drive"
            return {
                "files": [
                    {
                        "id": "file-1",
                        "name": "Release audit.csv",
                        "mimeType": "text/csv",
                        "size": "17",
                    }
                ]
            }

    monkeypatch.setattr(drive_operations, "DriveTransport", TestDriveTransport)
    response = dispatcher.dispatch_runtime(
        _canonical_workflow_test_payload(
            "search_drive_files",
            {"connection_ref": "drive-a", "limit": 10},
            connection_id="drive-a",
            connection_type_id="google_drive_account",
        )
    )

    assert response["ok"] is True
    assert response["result"] == {
        "files": [
            {
                "file_id": "file-1",
                "name": "Release audit.csv",
                "mime_type": "text/csv",
                "size": 17,
                "modified_time": None,
                "web_view_link": None,
                "is_folder": False,
            }
        ],
        "next_cursor": None,
    }


def test_host_suppressed_drive_upload_uses_canonical_envelope_without_artifact_grant(
    monkeypatch,
) -> None:
    import dispatcher
    from drive import operations as drive_operations

    monkeypatch.setattr(
        drive_operations,
        "input_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("host-suppressed test mode must not read an artifact grant")
        ),
    )
    monkeypatch.setattr(
        drive_operations,
        "access_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no OAuth in test mode")),
    )
    monkeypatch.setattr(
        drive_operations,
        "DriveTransport",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no provider in test mode")),
    )
    payload = _canonical_workflow_test_payload(
        "upload_drive_file",
        {
            "connection_ref": "drive-a",
            "artifact_handle": "artifact:source",
            "destination_folder_id": "folder-1",
            "filename": "stock.csv",
        },
        connection_id="drive-a",
        connection_type_id="google_drive_account",
    )
    payload["runtime_context"]["_host_test_mode_suppression"] = True

    response = dispatcher.dispatch_runtime(payload)

    assert response == {
        "ok": True,
        "result": {
            "test_mode_status": "suppressed",
            "external_effect_status": "suppressed",
            "definitely_no_external_effect": True,
        },
        "external_effect_status": "suppressed",
        "definitely_no_external_effect": True,
    }


def test_workflow_test_run_sheets_read_uses_canonical_envelope_and_returns_outputs(
    monkeypatch,
) -> None:
    import dispatcher
    from sheets import operations as sheets_operations

    class TestSheetsTransport:
        def __init__(self, token: str) -> None:
            assert token == "selected-test-token"

    expected = {
        "dataset_handle": "dataset:release-audit",
        "row_count": 2,
        "column_count": 2,
        "schema": [],
        "preview": [],
        "spreadsheet_id": "spreadsheet-1",
        "sheet_id": 0,
    }
    monkeypatch.setattr(sheets_operations, "SheetsTransport", TestSheetsTransport)
    monkeypatch.setattr(
        sheets_operations,
        "read_google_sheet",
        lambda _transport, _payload, _operation_input: expected,
    )
    response = dispatcher.dispatch_runtime(
        _canonical_workflow_test_payload(
            "read_google_sheet",
            {
                "connection_ref": "sheets-a",
                "spreadsheet_id": "spreadsheet-1",
                "sheet_id": 0,
            },
            connection_id="sheets-a",
            connection_type_id="google_sheets_account",
        )
    )

    assert response["ok"] is True
    assert response["result"] == expected


def test_workflow_test_run_gmail_mutation_stays_suppressed_without_provider_access() -> None:
    from dispatcher import dispatch_runtime

    response = dispatch_runtime(
        _canonical_workflow_test_payload(
            "set_message_flags",
            {
                "connection_ref": "gmail-a",
                "message_ids": ["message-1"],
                "seen": True,
            },
            connection_id="gmail-a",
            connection_type_id="google_gmail_account",
        )
    )

    assert response == {
        "ok": True,
        "result": {
            "results": [],
            "external_effect_status": "suppressed",
            "definitely_no_external_effect": True,
        },
        "external_effect_status": "suppressed",
        "definitely_no_external_effect": True,
    }


@pytest.mark.parametrize(
    ("action_id", "operation_input"),
    [
        (
            "search_messages",
            {"connection_ref": "gmail-a", "label_id": "INBOX"},
        ),
        ("search_drive_files", {"connection_ref": "drive-a"}),
        (
            "read_google_sheet",
            {"connection_ref": "sheets-a", "spreadsheet_id": "spreadsheet-1"},
        ),
    ],
)
def test_workspace_services_reject_unknown_canonical_action_envelope_keys(
    action_id: str,
    operation_input: dict,
) -> None:
    from dispatcher import dispatch_runtime

    payload = _canonical_workflow_test_payload(
        action_id,
        operation_input,
        connection_id=str(operation_input["connection_ref"]),
        connection_type_id={
            "search_messages": "google_gmail_account",
            "search_drive_files": "google_drive_account",
            "read_google_sheet": "google_sheets_account",
        }[action_id],
    )
    payload["action"]["provider_controlled_key"] = "must-not-be-accepted"

    response = dispatch_runtime(payload)

    assert response["ok"] is False
    assert response["error_code"] == "invalid_payload"


def test_workspace_dispatcher_rejects_unknown_operations() -> None:
    assert importlib.util.find_spec("dispatcher") is not None

    from dispatcher import dispatch_runtime

    unknown = dispatch_runtime(
        {
            "mode": "action",
            "action": {
                "action_id": "unknown_google_operation",
                "input": {"connection_ref": "gmail-a"},
            },
        }
    )
    assert unknown["ok"] is False
    assert unknown["error_code"] == "invalid_payload"


def test_workspace_dispatcher_routes_drive_without_exposing_it_to_gmail(monkeypatch) -> None:
    import dispatcher

    captured = {}
    monkeypatch.setitem(
        dispatcher.SERVICE_HANDLERS,
        "drive",
        lambda payload: captured.update(payload) or {"ok": True, "result": {"files": []}},
    )
    payload = {
        "mode": "action",
        "action": {
            "action_id": "search_drive_files",
            "input": {"connection_ref": "google-a"},
        },
    }

    assert dispatcher.dispatch_runtime(payload) == {"ok": True, "result": {"files": []}}
    assert captured == payload


def test_dispatcher_routes_drive_picker_query_to_bounded_search(monkeypatch) -> None:
    import dispatcher
    from drive import operations as drive_operations

    captured = {}

    def fake_search(_transport, operation_input):
        captured.update(operation_input)
        return {
            "files": [
                {
                    "file_id": "file-1",
                    "name": "Invoices",
                    "mime_type": "application/pdf",
                    "size": 12,
                    "modified_time": None,
                    "web_view_link": None,
                    "is_folder": False,
                }
            ],
            "next_cursor": "next",
        }

    monkeypatch.setattr(drive_operations, "search_drive_files", fake_search)
    response = dispatcher.dispatch_runtime(
        {
            "mode": "query",
            "query": {
                "query_id": "google_drive_files",
                "context": {"connection_id": "google-a"},
                "params": {"search": "Invoices", "cursor": "cursor", "limit": 25},
            },
            "target": {
                "connection": {
                    "connection_id": "google-a",
                    "connection_type_id": "google_drive_account",
                    "secrets": {"access_token": "token"},
                }
            },
        }
    )

    assert captured == {
        "connection_ref": "google-a",
        "name": "Invoices",
        "name_match": "contains",
        "trashed": False,
        "sort": "provider_order",
        "limit": 25,
        "cursor": "cursor",
    }
    assert response == {
        "ok": True,
        "result": {
            "items": [{"id": "file-1", "label": "Invoices", "mime_type": "application/pdf"}],
            "next_cursor": "next",
            "total": 1,
        },
    }


def test_workspace_dispatcher_routes_sheets_to_its_service(monkeypatch) -> None:
    import dispatcher

    captured = {}
    monkeypatch.setitem(
        dispatcher.SERVICE_HANDLERS,
        "sheets",
        lambda payload: (
            captured.update(payload) or {"ok": True, "result": {"dataset_handle": "dataset:one"}}
        ),
    )
    payload = {
        "mode": "action",
        "action": {
            "action_id": "read_google_sheet",
            "input": {
                "connection_ref": "google-a",
                "spreadsheet_id": "spreadsheet-1",
                "sheet_id": 1,
            },
        },
    }

    assert dispatcher.dispatch_runtime(payload) == {
        "ok": True,
        "result": {"dataset_handle": "dataset:one"},
    }
    assert captured == payload


def test_sheets_runtime_uses_only_explicitly_selected_connection_token(monkeypatch) -> None:
    from sheets import operations as sheets_operations

    class SelectedTransport:
        def __init__(self, token: str) -> None:
            assert token == "selected-sheets-token"

    monkeypatch.setattr(sheets_operations, "SheetsTransport", SelectedTransport)
    monkeypatch.setattr(
        sheets_operations,
        "read_google_sheet",
        lambda _transport, _payload, operation_input: {
            "dataset_handle": "dataset:one",
            "spreadsheet_id": operation_input["spreadsheet_id"],
        },
    )
    response = sheets_operations.handle_runtime(
        {
            "mode": "action",
            "action": {
                "action_id": "read_google_sheet",
                "input": {
                    "connection_ref": "google-sheets-a",
                    "spreadsheet_id": "spreadsheet-1",
                    "sheet_id": 1,
                },
                "target": {
                    "connection": {
                        "connection_id": "google-sheets-a",
                        "connection_type_id": "google_sheets_account",
                        "secrets": {"access_token": "selected-sheets-token"},
                    }
                },
            },
            "provider": {"secrets": {"access_token": "wrong-provider-token"}},
        }
    )

    assert response["ok"] is True
    assert response["result"] == {
        "dataset_handle": "dataset:one",
        "spreadsheet_id": "spreadsheet-1",
    }


def test_drive_runtime_uses_only_the_explicitly_selected_connection_token(monkeypatch) -> None:
    from drive import operations as drive_operations

    class DriveListTransport:
        def __init__(self, token: str) -> None:
            assert token == "selected-drive-token"

        def files_list(self, query):
            assert query["spaces"] == "drive"
            return {"files": []}

    monkeypatch.setattr(drive_operations, "DriveTransport", DriveListTransport)
    response = drive_operations.handle_runtime(
        {
            "mode": "action",
            "action": {
                "action_id": "search_drive_files",
                "input": {"connection_ref": "google-drive-a"},
                "target": {
                    "connection": {
                        "connection_id": "google-drive-a",
                        "connection_type_id": "google_drive_account",
                        "secrets": {"access_token": "selected-drive-token"},
                    }
                },
            },
            "provider": {"secrets": {"access_token": "wrong-provider-token"}},
        }
    )

    assert response == {
        "ok": True,
        "result": {"files": [], "next_cursor": None},
        "error_code": None,
        "error": None,
        "errors": [],
    }


def test_drive_picker_validation_reads_exact_file_through_selected_connection(monkeypatch) -> None:
    from drive import operations as drive_operations

    class ExactMetadataTransport:
        def __init__(self, token: str) -> None:
            assert token == "selected-drive-token"

        def file_metadata(self, file_id: str):
            assert file_id == "picked-file-1"
            return {
                "id": "picked-file-1",
                "name": "Stock.xlsx",
                "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "size": "41",
                "modifiedTime": "2026-08-26T10:00:00Z",
                "webViewLink": "https://drive.google.com/file/d/picked-file-1/view",
            }

        def files_list(self, _query):
            raise AssertionError("Picker validation must not search by file name")

    monkeypatch.setattr(drive_operations, "DriveTransport", ExactMetadataTransport)
    response = drive_operations.handle_runtime(
        {
            "mode": "query",
            "query": {
                "query_id": "google_drive_file_by_id",
                "context": {"connection_id": "google-drive-a"},
                "params": {"search": "picked-file-1", "limit": 1},
            },
            "target": {
                "connection": {
                    "connection_id": "google-drive-a",
                    "connection_type_id": "google_drive_account",
                    "secrets": {"access_token": "selected-drive-token"},
                }
            },
        }
    )

    assert response == {
        "ok": True,
        "result": {
            "items": [
                {
                    "id": "picked-file-1",
                    "label": "Stock.xlsx",
                    "mime_type": (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    ),
                }
            ],
            "next_cursor": "",
            "total": 1,
        },
    }


class ProfileTransport:
    def __init__(self, token: str) -> None:
        assert token == "selected-token"

    def profile(self):
        return {
            "emailAddress": "person@example.test",
            "messagesTotal": 12,
            "threadsTotal": 8,
            "historyId": "123",
        }


def test_runtime_uses_only_selected_connection_token() -> None:
    payload = {
        "mode": "action",
        "action": {
            "action_id": "test_connection",
            "input": {"connection_ref": "gmail-a"},
            "target": {
                "connection": {
                    "connection_id": "gmail-a",
                    "connection_type_id": "google_gmail_account",
                    "secrets": {"access_token": "selected-token"},
                }
            },
        },
        "provider": {"secrets": {"access_token": "wrong-provider-token"}},
    }

    response = handle_payload(payload, transport_factory=ProfileTransport)

    assert response == {
        "ok": True,
        "result": {
            "email_address": "person@example.test",
            "messages_total": 12,
            "threads_total": 8,
            "history_id": "123",
        },
    }


def test_search_test_mode_rejects_invalid_input_before_provider_access() -> None:
    class ForbiddenTransport:
        def __init__(self, token: str) -> None:
            assert token == "selected-token"

        def messages_list(self, _query):
            raise AssertionError("invalid input must not reach the provider")

    invalid = handle_payload(
        {
            "mode": "action",
            "action": {
                "action_id": "search_messages",
                "input": {
                    "connection_ref": "gmail-a",
                    "label_id": "INBOX",
                    "limit": 0,
                },
                "target": {
                    "connection": {
                        "connection_id": "gmail-a",
                        "connection_type_id": "google_gmail_account",
                        "secrets": {"access_token": "selected-token"},
                    }
                },
            },
            "runtime_context": {"test_mode": True},
        },
        transport_factory=ForbiddenTransport,
    )
    assert invalid["ok"] is False
    assert invalid["error_code"] == "invalid_payload"


def test_test_connection_rejects_boolean_provider_counts() -> None:
    class BooleanCountTransport(ProfileTransport):
        def profile(self):
            return {
                "emailAddress": "person@example.test",
                "messagesTotal": True,
                "threadsTotal": 8,
                "historyId": "123",
            }

    payload = {
        "mode": "action",
        "action": {
            "action_id": "test_connection",
            "input": {"connection_ref": "gmail-a"},
            "target": {
                "connection": {
                    "connection_id": "gmail-a",
                    "connection_type_id": "google_gmail_account",
                    "secrets": {"access_token": "selected-token"},
                }
            },
        },
    }

    response = handle_payload(payload, transport_factory=BooleanCountTransport)

    assert response["ok"] is False
    assert response["error_code"] == "mailbox_unusable"


def test_runtime_rejects_connection_ref_that_does_not_match_selected_connection() -> None:
    payload = {
        "mode": "action",
        "action": {
            "action_id": "test_connection",
            "input": {"connection_ref": "gmail-b"},
            "target": {
                "connection": {
                    "connection_id": "gmail-a",
                    "connection_type_id": "google_gmail_account",
                    "secrets": {"access_token": "selected-token"},
                }
            },
        },
        "provider": {"secrets": {"access_token": "must-not-be-used"}},
    }

    response = handle_payload(payload, transport_factory=ProfileTransport)

    assert response["ok"] is False
    assert response["error_code"] == "invalid_connection"


class LabelsTransport:
    def __init__(self, token: str) -> None:
        assert token == "selected-token"

    def labels(self):
        return {
            "labels": [
                {"id": "INBOX", "name": "Inbox", "type": "system"},
                {"id": "SENT", "name": "Sent", "type": "system"},
                {"id": "custom", "name": "Custom", "type": "user"},
            ]
        }


def test_move_label_resource_query_excludes_ineligible_system_labels(monkeypatch) -> None:
    monkeypatch.setattr(
        "gmail.operations.handle_payload",
        lambda payload: handle_payload(payload, transport_factory=LabelsTransport),
    )
    response = handle_runtime(
        {
            "mode": "query",
            "query": {
                "query_id": "gmail_move_labels",
                "context": {"connection_id": "gmail-a"},
            },
            "target": {
                "connection": {
                    "connection_id": "gmail-a",
                    "connection_type": "google_gmail_account",
                    "secrets": {"access_token": "selected-token"},
                }
            },
        }
    )

    assert response["ok"] is True
    assert [row["label_id"] for row in response["result"]["items"]] == ["INBOX", "custom"]


def test_search_filters_use_closed_typed_contract_and_rfc_message_id() -> None:
    filters = normalized_filters(
        {
            "rfc_message_id": "<message@example.test>",
            "read_state": "unread",
            "star_state": "starred",
            "attachment_state": "without_attachments",
        }
    )

    assert compile_query(filters) == (
        'rfc822msgid:"<message@example.test>" is:unread is:starred -has:attachment'
    )

    for rejected in ("seen", "unseen", "flagged", "unflagged", "has_attachments"):
        try:
            normalized_filters({rejected: True})
        except GmailExtensionError as exc:
            assert exc.code == "invalid_payload"
        else:  # pragma: no cover - closed contract assertion
            raise AssertionError(f"legacy filter was accepted: {rejected}")


@pytest.mark.parametrize(
    "provider_payload",
    [
        {"messages": {}},
        {"messages": 0},
        {"messages": ""},
        {"messages": [{"id": 123, "threadId": "thread-1"}]},
        {"messages": [{"id": "message-1", "threadId": 123}]},
        {"messages": [], "nextPageToken": 123},
    ],
)
def test_search_rejects_malformed_provider_collection_ids_and_page_token(
    provider_payload,
) -> None:
    class SearchTransport:
        def messages_list(self, _query):
            return provider_payload

    _raises_input_773_1 = SearchTransport()
    with pytest.raises(GmailExtensionError) as error:
        search_messages(
            _raises_input_773_1, {"connection_ref": "gmail-a", "label_id": "INBOX", "limit": 10}
        )

    assert error.value.code == "provider_unavailable"


@pytest.mark.parametrize(
    "provider_payload",
    [
        {"messages": [{"id": "m" * 513, "threadId": "thread-1"}]},
        {"messages": [{"id": "message-1", "threadId": "t" * 513}]},
        {"messages": [], "nextPageToken": "p" * 4097},
    ],
)
def test_search_rejects_oversized_provider_ids_and_page_token(provider_payload) -> None:
    class SearchTransport:
        def messages_list(self, _query):
            return provider_payload

    _raises_input_795_1 = SearchTransport()
    with pytest.raises(GmailExtensionError) as error:
        search_messages(
            _raises_input_795_1, {"connection_ref": "gmail-a", "label_id": "INBOX", "limit": 10}
        )

    assert error.value.code == "response_too_large"


def test_search_accepts_absent_or_null_messages_as_empty_provider_page() -> None:
    class SearchTransport:
        def __init__(self, payload):
            self.payload = payload

        def messages_list(self, _query):
            return self.payload

    operation_input = {"connection_ref": "gmail-a", "label_id": "INBOX", "limit": 10}
    assert search_messages(SearchTransport({}), operation_input)["messages"] == []
    assert search_messages(SearchTransport({"messages": None}), operation_input)["messages"] == []


def test_get_message_include_switches_reject_non_boolean_values_before_io() -> None:
    for field in (
        "include_body",
        "include_headers",
        "include_attachment_metadata",
        "include_raw_html",
    ):
        response = handle_payload(
            {
                "mode": "action",
                "action": {
                    "action_id": "get_message",
                    "input": {
                        "connection_ref": "gmail-a",
                        "message_id": "message-a",
                        field: "false",
                    },
                },
            }
        )
        assert response["ok"] is False
        assert response["error_code"] == "invalid_payload"
