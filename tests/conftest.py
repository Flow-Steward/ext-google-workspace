from __future__ import annotations

import sys
from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
if str(BUNDLE_ROOT) not in sys.path:
    sys.path.insert(0, str(BUNDLE_ROOT))

#: The host contract every subprocess payload declares.
HOST_CONTRACT_VERSION = "extension_host_v2"
#: The final extension_host_v2 execution scope: all four keys, at most one owner.
ROOT_STEP_SCOPE: dict[str, str | None] = {
    "group_id": None,
    "for_each_id": None,
    "step_id": "sync_workspace",
    "step_execution_id": "3f9b1c2d-5e6a-4b7c-8d9e-000000000001",
}
GROUP_STEP_SCOPE: dict[str, str | None] = {**ROOT_STEP_SCOPE, "group_id": "prepare_files"}
FOR_EACH_STEP_SCOPE: dict[str, str | None] = {
    **ROOT_STEP_SCOPE,
    "for_each_id": "each_file",
    "step_execution_id": "3f9b1c2d-5e6a-4b7c-8d9e-000000000002",
}
