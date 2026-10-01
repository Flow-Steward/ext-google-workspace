"""Canonical action-envelope boundary shared by Workspace services."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ALLOWED_ACTION_KEYS = frozenset(
    {
        "action_id",
        "operation_id",
        "page_id",
        "component_id",
        "context",
        "input",
        "target",
    }
)


def is_valid_action_envelope(value: Any) -> bool:
    """Accept the complete host envelope and reject provider-controlled extras."""
    return isinstance(value, Mapping) and not set(value).difference(ALLOWED_ACTION_KEYS)


__all__ = ["ALLOWED_ACTION_KEYS", "is_valid_action_envelope"]
