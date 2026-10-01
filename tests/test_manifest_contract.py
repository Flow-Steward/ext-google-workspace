from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
import yaml
from dispatcher import OPERATION_REGISTRY
from drive.errors import SAFE_ERROR_CODES as DRIVE_SAFE_ERROR_CODES
from drive.operations import DRIVE_QUERY_IDS
from drive.operations import OPERATION_IDS as DRIVE_OPERATION_IDS
from gmail.errors import SAFE_ERROR_CODES as GMAIL_SAFE_ERROR_CODES
from gmail.search import SEARCH_FILTER_FIELDS
from gmail.validation import MUTATIONS, OPERATION_FIELDS
from sheets.errors import SAFE_ERROR_CODES as SHEETS_SAFE_ERROR_CODES
from sheets.operations import OPERATION_IDS as SHEETS_OPERATION_IDS

BUNDLE_ROOT = Path(__file__).resolve().parents[1]

DRIVE_SHEETS_FIELDS = {
    "search_drive_files": {
        "connection_ref",
        "parent_folder_id",
        "name",
        "name_match",
        "mime_types",
        "modified_after",
        "modified_before",
        "trashed",
        "sort",
        "limit",
        "cursor",
    },
    "download_drive_file": {"connection_ref", "file_id", "export_mime_type"},
    "upload_drive_file": {
        "connection_ref",
        "artifact_handle",
        "destination_folder_id",
        "filename",
    },
    "read_google_sheet": {
        "connection_ref",
        "spreadsheet_id",
        "sheet_id",
        "sheet_title",
        "range",
        "header_mode",
    },
    "write_google_sheet": {
        "connection_ref",
        "mode",
        "dataset_handle",
        "inline_rows",
        "start_cell",
        "spreadsheet_title",
        "destination_folder_id",
        "spreadsheet_id",
        "sheet_id",
        "sheet_title",
    },
    "append_google_sheet_rows": {
        "connection_ref",
        "spreadsheet_id",
        "sheet_id",
        "sheet_title",
        "table_range",
        "dataset_handle",
        "inline_rows",
    },
}


def _yaml(relative: str) -> dict:
    return yaml.safe_load((BUNDLE_ROOT / relative).read_text())


@pytest.mark.parametrize(
    ("connection_type_id", "service_scope"),
    [
        ("google_gmail_account", "https://www.googleapis.com/auth/gmail.modify"),
        ("google_drive_account", "https://www.googleapis.com/auth/drive.file"),
        ("google_sheets_account", "https://www.googleapis.com/auth/drive.file"),
    ],
)
def test_connection_scope_contract_accepts_google_token_grant_shape(
    connection_type_id: str,
    service_scope: str,
) -> None:
    connections = {
        row["connection_type_id"]: row
        for row in _yaml("contracts/connection_types.yaml")["connection_types"]
    }
    required_scopes = set(connections[connection_type_id]["auth"]["scopes"]["required"])
    google_token_grant = {
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        service_scope,
    }

    assert required_scopes.issubset(google_token_grant)


def test_static_contract_surfaces_match_closed_runtime_registry_exactly() -> None:
    manifest = _yaml("extension.yaml")
    operations = _yaml("contracts/operation_manifest.yaml")["operations"]
    actions = _yaml("ui/actions/actions.yaml")["actions"]
    steps = _yaml("contracts/step_ui_manifest.yaml")["forms"]

    operation_rows = {row["operation_id"]: row for row in operations}
    action_rows = {row["action_id"]: row for row in actions}
    step_rows = {row["operation_id"]: row for row in steps}
    expected = set(OPERATION_FIELDS) | set(DRIVE_SHEETS_FIELDS)

    assert set(operation_rows) == expected
    assert set(action_rows) == expected | {"verify_google_identity", "get_picker_bootstrap"}
    assert action_rows["verify_google_identity"]["workflow_visible"] is False
    assert action_rows["get_picker_bootstrap"] == {
        "action_id": "get_picker_bootstrap",
        "title": "Prepare Google Picker",
        "required_scopes": ["extension:invoke"],
        "handler": {"mode": "extension"},
        "mutates_platform": False,
        "workflow_visible": False,
        "parameters": [],
        "result_fields": ["client_id", "developer_key", "app_id", "origin", "email_hint", "scope"],
        "error_codes": ["invalid_payload", "provider_unavailable"],
    }
    assert set(step_rows) == expected
    assert {row["operation_id"] for row in manifest["external_effects"]} == set(MUTATIONS) | {
        "upload_drive_file",
        "write_google_sheet",
        "append_google_sheet_rows",
    }
    queries = _yaml("contracts/resource_queries.yaml")["queries"]
    assert {row["query_id"] for row in queries} == {
        "gmail_labels",
        "gmail_move_labels",
        *DRIVE_QUERY_IDS,
    }
    assert {row["query_id"]: row["eligibility"] for row in queries if "eligibility" in row} == {
        "gmail_labels": "search",
        "gmail_move_labels": "move",
    }
    assert set(OPERATION_REGISTRY) == expected | {
        "verify_google_identity",
        "get_picker_bootstrap",
        "gmail_labels",
        "gmail_move_labels",
        *DRIVE_QUERY_IDS,
    }
    declared_errors = set().union(*(set(row["error_codes"]) for row in operations))
    assert declared_errors == (
        set(GMAIL_SAFE_ERROR_CODES) | set(DRIVE_SAFE_ERROR_CODES) | set(SHEETS_SAFE_ERROR_CODES)
    )
    for operation_id in expected:
        operation = operation_rows[operation_id]
        action = action_rows[operation_id]
        assert [row["name"] for row in operation["inputs"]] == [
            row["name"] for row in action["parameters"]
        ]
        assert [row.removeprefix("result.") for row in operation["result_fields"]] == action[
            "result_fields"
        ]
        assert operation["error_codes"] == action["error_codes"]
        expected_fields = OPERATION_FIELDS.get(operation_id) or DRIVE_SHEETS_FIELDS[operation_id]
        assert {row["name"] for row in operation["inputs"]} == expected_fields

    mutating_operations = set(MUTATIONS) | {
        "upload_drive_file",
        "write_google_sheet",
        "append_google_sheet_rows",
    }
    assert {
        action_id
        for action_id, action in action_rows.items()
        if action.get("test_mode_behavior") == "suppress"
    } == mutating_operations
    assert all(action_rows[action_id]["mutates_platform"] for action_id in mutating_operations)

    assert {
        "from",
        "to",
        "cc",
        "subject",
        "text",
        "rfc_message_id",
        "since",
        "before",
        "read_state",
        "star_state",
        "attachment_state",
    } == SEARCH_FILTER_FIELDS


def test_oauth_manifest_owns_google_provider_gmail_intent_and_callback() -> None:
    manifest = _yaml("extension.yaml")
    provider = manifest["runtime"]["extension_contract_v2"]["oauth_providers"][0]
    connections = {
        row["connection_type_id"]: row
        for row in _yaml("contracts/connection_types.yaml")["connection_types"]
    }

    assert manifest["callbacks"] == [
        {
            "callback_id": "oauth_authorization",
            "purpose": "oauth_authorization",
            "methods": ["GET"],
            "connection_scoped": False,
        }
    ]
    assert provider["provider_id"] == "workspace_oauth"
    assert provider["setup_page_id"] == "setup-guide"
    assert provider["help_page_id"] == "setup-guide"
    assert provider["flow"] == {"type": "authorization_code", "pkce": "s256"}
    assert provider["authorization_endpoint"] == {
        "url": "https://accounts.google.com/o/oauth2/v2/auth"
    }
    assert provider["token_endpoint"] == {
        "url": "https://oauth2.googleapis.com/token",
        "client_auth_method": "client_secret_post",
    }
    assert provider["revocation_endpoint"] == {"url": "https://oauth2.googleapis.com/revoke"}
    assert {
        row["intent_id"]: row["static_parameters"] for row in provider["authorization_intents"]
    } == {
        "gmail_connect": {
            "access_type": "offline",
            "include_granted_scopes": False,
            "prompt": "consent select_account",
        },
        "drive_connect": {
            "access_type": "offline",
            "include_granted_scopes": False,
            "prompt": "consent select_account",
        },
        "sheets_connect": {
            "access_type": "offline",
            "include_granted_scopes": False,
            "prompt": "consent select_account",
        },
    }
    assert all(
        row["user_inputs"]
        == [
            {
                "input_id": "email_hint",
                "parameter": "login_hint",
                "validation": {"type": "email", "max_length": 320},
            }
        ]
        for row in provider["authorization_intents"]
    )
    assert provider["post_connect_action_id"] == "verify_google_identity"
    assert {key: row["auth"] for key, row in connections.items()} == {
        "google_gmail_account": {
            "type": "oauth2",
            "provider_id": "workspace_oauth",
            "scopes": {
                "required": [
                    "openid",
                    "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/gmail.modify",
                ],
                "optional": [],
            },
        },
        "google_drive_account": {
            "type": "oauth2",
            "provider_id": "workspace_oauth",
            "scopes": {
                "required": [
                    "openid",
                    "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/drive.file",
                ],
                "optional": [],
            },
        },
        "google_sheets_account": {
            "type": "oauth2",
            "provider_id": "workspace_oauth",
            "scopes": {
                "required": [
                    "openid",
                    "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/drive.file",
                ],
                "optional": [],
            },
        },
    }
    credential_fields = {row["field_id"]: row for row in provider["credential_fields"]}
    assert credential_fields["client_id"]["required"] is True
    assert credential_fields["client_secret"]["required"] is True
    assert {
        field_id: credential_fields[field_id]["required"]
        for field_id in ("picker_api_key", "picker_app_id", "picker_origin")
    } == {
        "picker_api_key": False,
        "picker_app_id": False,
        "picker_origin": False,
    }
    assert credential_fields["picker_origin"]["validation"] == {
        "type": "https_origin",
        "min_length": 9,
        "max_length": 2048,
    }
    assert credential_fields["picker_origin"]["label"] == "Picker browser origin"
    assert credential_fields["picker_origin"]["help_text"] == (
        "Optional. Required only to enable Google Picker shortcuts for Drive and Sheets."
    )
    gmail_page = _yaml("ui/pages/gmail.yaml")
    gmail_connection = next(
        component
        for component in gmail_page["components"]
        if component["component_id"] == "gmail_oauth_connections"
    )
    assert gmail_connection == {
        "component_id": "gmail_oauth_connections",
        "type": "connection_form",
        "title": "Gmail accounts",
        "description": "Add independently authorized Gmail accounts for this project.",
        "data": {
            "oauth_provider_id": "workspace_oauth",
            "connection_type": "google_gmail_account",
            "oauth_intent": "gmail_connect",
            "test_action": "test_connection",
        },
    }
    for page_name in ("drive", "sheets"):
        page = _yaml(f"ui/pages/{page_name}.yaml")
        connection = next(
            component for component in page["components"] if component["type"] == "connection_form"
        )
        assert "test_action" not in connection["data"]


def test_setup_guide_is_the_default_google_workspace_page() -> None:
    pages = _yaml("ui/ui_manifest.yaml")["pages"]

    assert pages == [
        {"ref": "pages/setup-guide.yaml"},
        {"ref": "pages/gmail.yaml"},
        {"ref": "pages/drive.yaml"},
        {"ref": "pages/sheets.yaml"},
    ]


def test_extension_owned_browser_acceptance_covers_copy_endpoints_intents_and_picker() -> None:
    source = (BUNDLE_ROOT / "tests/e2e/extension-oauth.spec.ts").read_text(encoding="utf-8")

    assert "page.route(" not in source
    assert "Google OAuth Client Secret" in source
    assert "https://accounts.google.com/o/oauth2/v2/auth" in source
    assert "gmail_connect" in source
    assert '"test_action":"test_connection"' in source
    assert "Picker API key" in source
    assert "await expect(serviceTab).toBeDisabled()" in source
    assert 'getByRole("tab", { name: pageName })).toBeEnabled()' in source


def test_drive_and_sheets_static_contracts_publish_exact_boundaries() -> None:
    manifest = _yaml("extension.yaml")
    operations = {
        row["operation_id"]: row for row in _yaml("contracts/operation_manifest.yaml")["operations"]
    }
    connections = {
        row["connection_type_id"]: row
        for row in _yaml("contracts/connection_types.yaml")["connection_types"]
    }
    policies = {
        row["operation_id"]: row
        for row in _yaml("contracts/artifact_policies.yaml")["artifact_policies"]
    }

    assert set(DRIVE_SHEETS_FIELDS) == set(DRIVE_OPERATION_IDS | SHEETS_OPERATION_IDS)
    assert set(connections["google_gmail_account"]["capabilities"]) == set(OPERATION_FIELDS)
    assert set(connections["google_drive_account"]["capabilities"]) == set(DRIVE_OPERATION_IDS)
    assert set(connections["google_sheets_account"]["capabilities"]) == set(SHEETS_OPERATION_IDS)
    assert set(policies) == {
        "get_attachment",
        "download_drive_file",
        "upload_drive_file",
        "read_google_sheet",
        "write_google_sheet",
        "append_google_sheet_rows",
    }
    assert {row["operation_id"] for row in manifest["external_effects"]} == (
        set(MUTATIONS) | {"upload_drive_file", "write_google_sheet", "append_google_sheet_rows"}
    )
    assert manifest["entrypoint"]["timeout_policy"] == {
        "env": "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS",
        "default_seconds": 300,
        "minimum_seconds": 30,
        "maximum_seconds": 3600,
        "headroom_seconds": 30,
    }
    assert manifest["version"] == "0.2.0"
    assert set(manifest["required_scopes"]) == {
        "extension:invoke",
        "artifact:read",
        "artifact:write",
    }
    queries = _yaml("contracts/resource_queries.yaml")["queries"]
    assert {row["query_id"] for row in queries} == {
        "gmail_labels",
        "gmail_move_labels",
        *DRIVE_QUERY_IDS,
    }

    for operation_id in DRIVE_OPERATION_IDS:
        operation = operations[operation_id]
        assert operation["connection_type_ids"] == ["google_drive_account"]
        assert operation["inputs"][0] == {
            "name": "connection_ref",
            "value_type": "connection_ref",
            "required": True,
        }
        _assert_closed_schemas(operation)
    for operation_id in SHEETS_OPERATION_IDS:
        operation = operations[operation_id]
        assert operation["connection_type_ids"] == ["google_sheets_account"]
        _assert_closed_schemas(operation)


def test_artifact_input_policies_bind_runtime_handles_from_operation_inputs() -> None:
    policy_rows = _yaml("contracts/artifact_policies.yaml")["artifact_policies"]
    policies = {row["operation_id"]: row for row in policy_rows}

    assert _yaml("extension.yaml")["artifact_policies"] == policy_rows

    upload_input = policies["upload_drive_file"]["inputs"][0]
    assert upload_input["binding_key"] == "drive_upload_artifact"
    assert upload_input["from_input"] == "artifact_handle"
    assert upload_input.get("required", True) is True
    assert upload_input["max_size_bytes"] == 200 * 1024 * 1024
    assert upload_input["max_size_env"] == "FS_EXTENSION_ARTIFACT_MAX_BYTES"
    download_output = policies["download_drive_file"]["outputs"][0]
    assert download_output["max_size_env"] == "FS_EXTENSION_ARTIFACT_MAX_BYTES"

    for operation_id in ("write_google_sheet", "append_google_sheet_rows"):
        dataset_input = policies[operation_id]["inputs"][0]
        assert dataset_input["binding_key"] == "google_sheet_input_dataset"
        assert dataset_input["from_input"] == "dataset_handle"
        assert dataset_input["required"] is False


def test_sheet_create_folder_is_optional_while_drive_upload_folder_is_required() -> None:
    operations = {
        row["operation_id"]: row for row in _yaml("contracts/operation_manifest.yaml")["operations"]
    }
    actions = {row["action_id"]: row for row in _yaml("ui/actions/actions.yaml")["actions"]}
    forms = {row["operation_id"]: row for row in _yaml("contracts/step_ui_manifest.yaml")["forms"]}

    def required(surface: dict, group: str) -> bool:
        row = next(item for item in surface[group] if item["name"] == "destination_folder_id")
        return bool(row.get("required", False))

    assert required(operations["write_google_sheet"], "inputs") is False
    assert required(actions["write_google_sheet"], "parameters") is False
    assert required(forms["write_google_sheet"], "fields") is False
    assert required(operations["upload_drive_file"], "inputs") is True
    assert required(actions["upload_drive_file"], "parameters") is True
    assert required(forms["upload_drive_file"], "fields") is True


def _assert_closed_schemas(operation: dict) -> None:
    for group in ("inputs", "outputs"):
        for field in operation[group]:
            schema = field.get("schema")
            if field["value_type"] in {"object", "array"}:
                assert isinstance(schema, dict), (operation["operation_id"], group, field["name"])
                _assert_recursive_object_closure(schema)


def _assert_recursive_object_closure(schema: dict) -> None:
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False
        for child in schema.get("properties", {}).values():
            if isinstance(child, dict):
                _assert_recursive_object_closure(child)
    items = schema.get("items")
    if isinstance(items, dict):
        _assert_recursive_object_closure(items)


def test_picker_behavior_and_sdk_declarations_are_extension_owned() -> None:
    forms = _yaml("contracts/step_ui_manifest.yaml")["forms"]
    picker_fields = {
        (form["operation_id"], field["name"]): field["external_resource_picker"]
        for form in forms
        for field in form.get("fields", [])
        if field.get("widget_type") == "resource_picker" and "external_resource_picker" in field
    }

    assert picker_fields
    assert (BUNDLE_ROOT / "assets/google-picker-bridge.js").is_file()
    for (operation_id, field_name), declaration in picker_fields.items():
        assert declaration["bootstrap_action_id"] == "get_picker_bootstrap"
        assert declaration["bridge_global"] == "FlowStewardGoogleWorkspacePicker"
        assert declaration["scripts"] == [
            {"src": "https://accounts.google.com/gsi/client"},
            {"src": "https://apis.google.com/js/api.js"},
            {"asset_ref": "assets/google-picker-bridge.js"},
        ]
        assert declaration["allowed_script_origins"] == [
            "https://accounts.google.com",
            "https://apis.google.com",
        ]
        assert declaration["selection"] == {
            "maximum": 1,
            "fields": [
                {
                    "source": "id",
                    "target": field_name,
                    "required": True,
                    "max_length": 1024,
                }
            ],
        }
        assert declaration["options"]["resource_kind"] in {
            "file",
            "folder",
            "spreadsheet",
        }, (operation_id, field_name)


def test_frontend_google_picker_contract_fixture_matches_bundled_declarations() -> None:
    forms = _yaml("contracts/step_ui_manifest.yaml")["forms"]
    picker_fields = {
        (form["operation_id"], field["name"]): field["external_resource_picker"]
        for form in forms
        for field in form.get("fields", [])
        if field.get("widget_type") == "resource_picker" and "external_resource_picker" in field
    }
    fixture_path = BUNDLE_ROOT / "tests/frontend/google-picker-contract-fixture.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    common = fixture["common"]
    projected = {}
    for row in fixture["cases"]:
        field_name = row["field_name"]
        projected[(row["operation_id"], field_name)] = {
            **common,
            "button_label": row["button_label"],
            "error_message": row["error_message"],
            "options": {"resource_kind": row["resource_kind"]},
            "selection": {
                "maximum": 1,
                "fields": [
                    {
                        "source": "id",
                        "target": field_name,
                        "required": True,
                        "max_length": 1024,
                    }
                ],
            },
        }

    assert projected == picker_fields


def test_extension_python_has_no_core_google_sdk_or_requests_imports() -> None:
    forbidden_roots = {"core", "google", "googleapiclient", "requests"}
    for path in BUNDLE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            assert not names & forbidden_roots, f"forbidden dependency in {path.name}"


def test_workspace_ui_has_exact_service_tabs_and_operator_guidance() -> None:
    ui_manifest = _yaml("ui/ui_manifest.yaml")
    page_refs = [row["ref"] for row in ui_manifest["pages"]]
    pages = [_yaml(f"ui/{ref}") for ref in page_refs]

    assert [page["page_id"] for page in pages] == ["setup-guide", "gmail", "drive", "sheets"]
    assert [page["title"] for page in pages] == ["Setup guide", "Gmail", "Drive", "Sheets"]
    combined = "\n".join(
        str(component.get("body") or "")
        for page in pages
        for component in page.get("components", [])
        if isinstance(component, dict)
    )
    for required in (
        "Before you connect",
        "Flow Steward project administrator",
        "Google account owner",
        "https://developers.google.com/workspace/guides/configure-oauth-consent",
        "https://developers.google.com/workspace/drive/picker/guides/web-picker",
        "https://developers.google.com/workspace/sheets/api/scopes",
    ):
        assert required in combined
    for forbidden in ("Client Secret:", "refresh token:", "access token:"):
        assert forbidden not in combined


def test_oauth_browser_spec_awaits_manifest_page_content_after_tab_navigation() -> None:
    source = (BUNDLE_ROOT / "tests/e2e/extension-oauth.spec.ts").read_text(encoding="utf-8")

    for page_name in ("gmail", "drive"):
        assert _yaml(f"ui/pages/{page_name}.yaml")["description"] in source
    assert 'getByRole("tab", { name: "Gmail", selected: true })' in source
    assert 'getByRole("tab", { name: "Drive", selected: true })' in source


def test_shipped_documentation_describes_current_extension_owned_lifecycle() -> None:
    readme = (BUNDLE_ROOT / "README.md").read_text(encoding="utf-8")
    architecture = (BUNDLE_ROOT / "docs/oauth-architecture.md").read_text(encoding="utf-8")
    combined = f"{readme}\n{architecture}"

    assert "personal `gmail.com`" in readme
    assert "visible only after a successful code exchange" in readme
    assert "separate project-scoped connections" in architecture
    assert "Host code does not contain Google endpoints" in architecture
    for stale in (
        "Core owns provider credentials",
        "visible pending connection",
        "Sheets becomes `authorized`",
        "Remove performs",
        "0023-extension-owned-oauth-boundary.md",
        "Duplicate subjects and duplicate primary emails are rejected",
        "only after the A successful OAuth callback",
    ):
        assert stale not in combined
