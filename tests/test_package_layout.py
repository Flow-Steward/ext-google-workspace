from __future__ import annotations

import importlib
from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
GMAIL_MODULES = {
    "artifacts",
    "errors",
    "messages",
    "mutations",
    "operations",
    "search",
    "transport",
    "validation",
}
DRIVE_MODULES = {
    "config",
    "artifacts",
    "errors",
    "operations",
    "search",
    "transport",
    "uploads",
    "validation",
}
SHEETS_MODULES = {
    "errors",
    "operations",
    "ranges",
    "reads",
    "transport",
    "validation",
    "writes",
}


def test_gmail_runtime_uses_service_package_layout() -> None:
    importlib.import_module("action_envelope")
    assert {path.stem for path in (BUNDLE_ROOT / "gmail").glob("*.py")} == {
        "__init__",
        *GMAIL_MODULES,
    }
    assert not {
        "attachments.py",
        "cursors.py",
        "errors.py",
        "gmail_transport.py",
        "mime.py",
        "mutations.py",
        "operations.py",
        "runtime.py",
    }.intersection(path.name for path in BUNDLE_ROOT.glob("*.py"))

    for module_name in GMAIL_MODULES:
        importlib.import_module(f"gmail.{module_name}")


def test_drive_runtime_uses_its_own_service_package_layout() -> None:
    assert {path.stem for path in (BUNDLE_ROOT / "drive").glob("*.py")} == {
        "__init__",
        *DRIVE_MODULES,
    }
    for module_name in DRIVE_MODULES:
        importlib.import_module(f"drive.{module_name}")


def test_sheets_runtime_uses_its_own_service_package_layout() -> None:
    assert {path.stem for path in (BUNDLE_ROOT / "sheets").glob("*.py")} == {
        "__init__",
        *SHEETS_MODULES,
    }
    for module_name in SHEETS_MODULES:
        importlib.import_module(f"sheets.{module_name}")
