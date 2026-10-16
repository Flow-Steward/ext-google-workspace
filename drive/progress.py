"""Tell the person watching a run how far a Drive transfer has got."""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from flowsteward_extension_sdk import report_progress

PROGRESS_MIN_INTERVAL_SECONDS = 1.0
_MEGABYTE = 1024 * 1024

TransferProgress = Callable[[int, bool], None]


def transfer_progress(
    verb: str,
    name: str,
    *,
    total_bytes: int | None,
    clock: Callable[[], float] = time.monotonic,
) -> TransferProgress:
    """Report a transfer in whole megabytes, at most once a second and always at the end.

    With a known size the step shows "N of M" megabytes; without one (a Google Docs
    export), how much has moved.
    """
    total_mb = math.ceil(total_bytes / _MEGABYTE) if total_bytes else None
    last = {"mb": 0, "at": float("-inf")}

    def report(size_bytes: int, finished: bool) -> None:
        done_mb = total_mb if finished and total_mb else size_bytes // _MEGABYTE
        now = clock()
        if not finished and (
            done_mb <= last["mb"] or now - last["at"] < PROGRESS_MIN_INTERVAL_SECONDS
        ):
            return
        if done_mb == 0:
            return  # under a megabyte: nothing worth a progress line
        last["mb"], last["at"] = done_mb, now
        if total_mb:
            report_progress(f"{verb} {name} (MB)", done=done_mb, total=total_mb)
        else:
            report_progress(f"{verb} {name}: {done_mb} MB so far", done=done_mb)

    return report


__all__ = ["report_progress", "transfer_progress"]
