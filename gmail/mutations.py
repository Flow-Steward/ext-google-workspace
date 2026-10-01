from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .errors import GmailExtensionError

if TYPE_CHECKING:
    from .transport import GmailTransport


def normalized_message_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise GmailExtensionError("invalid_payload", "message_ids must be an array")
    result: list[str] = []
    for raw in value:
        item = str(raw or "").strip() if isinstance(raw, str) else ""
        if not item or len(item) > 512 or any(ord(char) < 32 for char in item):
            raise GmailExtensionError("invalid_payload", "message_ids contains an invalid ID")
        if item not in result:
            result.append(item)
    if not 1 <= len(result) <= 100:
        raise GmailExtensionError("invalid_payload", "message_ids must contain 1 to 100 IDs")
    return result


def suppressed(ids: list[str]) -> dict[str, Any]:
    del ids
    return {
        "results": [],
        "external_effect_status": "suppressed",
        "definitely_no_external_effect": True,
    }


def set_message_flags(
    transport: GmailTransport, operation_input: Mapping[str, Any], *, test_mode: bool
) -> dict[str, Any]:
    ids = normalized_message_ids(operation_input.get("message_ids"))
    seen = operation_input.get("seen")
    flagged = operation_input.get("flagged")
    if seen is None and flagged is None:
        raise GmailExtensionError("invalid_payload", "seen or flagged is required")
    if seen is not None and not isinstance(seen, bool):
        raise GmailExtensionError("invalid_payload", "seen must be a boolean")
    if flagged is not None and not isinstance(flagged, bool):
        raise GmailExtensionError("invalid_payload", "flagged must be a boolean")
    if test_mode:
        return suppressed(ids)
    add: list[str] = []
    remove: list[str] = []
    if seen is not None:
        (remove if seen else add).append("UNREAD")
    if flagged is not None:
        (add if flagged else remove).append("STARRED")
    return _batch_result(transport, ids, add, remove)


def move_messages(
    transport: GmailTransport, operation_input: Mapping[str, Any], *, test_mode: bool
) -> dict[str, Any]:
    ids = normalized_message_ids(operation_input.get("message_ids"))
    source = _label(operation_input.get("source_label_id"))
    target = _label(operation_input.get("target_label_id"))
    if source == target:
        raise GmailExtensionError("invalid_payload", "Source and target labels must differ")
    if test_mode:
        return suppressed(ids)
    labels_payload = transport.labels()
    rows = labels_payload.get("labels")
    if not isinstance(rows, list):
        raise GmailExtensionError("provider_unavailable", "Gmail returned invalid labels")
    eligible = {
        str(row.get("id"))
        for row in rows
        if isinstance(row, Mapping)
        and (str(row.get("id")) == "INBOX" or str(row.get("type")).lower() == "user")
    }
    if source not in eligible or target not in eligible:
        raise GmailExtensionError("label_ineligible", "Selected Gmail label cannot be moved")
    return _batch_result(transport, ids, [target], [source])


def delete_messages(
    transport: GmailTransport, operation_input: Mapping[str, Any], *, test_mode: bool
) -> dict[str, Any]:
    ids = normalized_message_ids(operation_input.get("message_ids"))
    if test_mode:
        return suppressed(ids)
    results: list[dict[str, str]] = []
    for index, message_id in enumerate(ids):
        try:
            transport.trash(message_id)
            results.append({"message_id": message_id, "status": "succeeded"})
        except GmailExtensionError as exc:
            if exc.ambiguous or exc.code == "timeout_unknown":
                results.append({"message_id": message_id, "status": "timeout_unknown"})
                results.extend(
                    {
                        "message_id": remaining,
                        "status": "not_attempted_after_timeout_unknown",
                    }
                    for remaining in ids[index + 1 :]
                )
                return _aggregate(results)
            results.append({"message_id": message_id, "status": "failed"})
    return _aggregate(results)


def _batch_result(
    transport: GmailTransport,
    ids: list[str],
    add: list[str],
    remove: list[str],
) -> dict[str, Any]:
    try:
        transport.batch_modify(ids, add, remove)
    except GmailExtensionError as exc:
        status = "timeout_unknown" if exc.ambiguous or exc.code == "timeout_unknown" else "failed"
        return _aggregate([{"message_id": item, "status": status} for item in ids])
    return _aggregate([{"message_id": item, "status": "succeeded"} for item in ids])


def _aggregate(results: list[dict[str, str]]) -> dict[str, Any]:
    statuses = {row["status"] for row in results}
    if "timeout_unknown" in statuses:
        aggregate = "timeout_unknown"
    elif statuses == {"succeeded"}:
        aggregate = "succeeded"
    elif "succeeded" in statuses:
        aggregate = "timeout_unknown"
    else:
        aggregate = "failed"
    return {
        "results": results,
        "external_effect_status": aggregate,
        "definitely_no_external_effect": not (
            "succeeded" in statuses or "timeout_unknown" in statuses
        ),
    }


def _label(value: Any) -> str:
    label = str(value or "").strip() if isinstance(value, str) else ""
    if not label or len(label) > 512 or any(ord(char) < 32 for char in label):
        raise GmailExtensionError("invalid_payload", "Gmail label ID is invalid")
    return label
