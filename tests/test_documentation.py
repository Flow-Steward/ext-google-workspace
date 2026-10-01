from __future__ import annotations

from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parents[1]


def test_operator_readme_documents_drive_sheets_security_and_lifecycle() -> None:
    readme = (BUNDLE_ROOT / "README.md").read_text()

    for required in (
        "version 0.2.0",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/userinfo.email",
        "Google Picker API",
        "authorized JavaScript origin",
        "Picker App ID",
        "include_granted_scopes=false",
        "drive_connect",
        "sheets_connect",
        "Google subject",
        "post-commit",
        "200 MiB",
        "100,000 rows",
        "1,000,000 cells",
        "2 MiB",
        "RAW",
        "Shared Drive",
        "No default Google account",
        "Disable",
        "Uninstall",
        "Picker API key",
        "Drive API scopes",
        "Google Sheets API scopes",
        "Google Picker overview",
    ):
        assert required in readme

    for forbidden in (
        "Version 1 is Gmail-only",
        "exposes no Drive or Sheets",
        "ya29.",
        "AIzaSy",
        "Client Secret:",
    ):
        assert forbidden not in readme


def test_operator_readme_lists_exact_drive_and_sheets_operations_and_non_goals() -> None:
    readme = (BUNDLE_ROOT / "README.md").read_text()
    for operation_id in (
        "search_drive_files",
        "download_drive_file",
        "upload_drive_file",
        "read_google_sheet",
        "write_google_sheet",
        "append_google_sheet_rows",
    ):
        assert f"`{operation_id}`" in readme
    for non_goal in (
        "generic Google API",
        "Drive sharing",
        "Sheets formatting",
        "formula execution",
        "file conversion",
    ):
        assert non_goal in readme


def test_operator_readme_describes_browser_visible_picker_key_and_separate_consents() -> None:
    readme = (BUNDLE_ROOT / "README.md").read_text()

    assert "browser-visible non-secret configuration" in readme
    assert "personal `gmail.com`" in readme
    assert "separate consent flow" in readme
    assert "encrypted\nClient Secret and Picker API key" not in readme
    assert "Never put a Client Secret, access token, refresh token, Picker API key" not in readme
