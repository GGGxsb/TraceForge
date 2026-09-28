from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path


def _workspace_path(workspace: Path, raw: str) -> Path:
    path = (workspace / raw).resolve()
    if os.environ.get("TRACEFORGE_FULL_ACCESS") != "1":
        path.relative_to(workspace)
    return path


def apply_patch(payload: dict, workspace: Path) -> int:
    path = _workspace_path(workspace, payload["path"])
    if not path.exists():
        if payload["old_text"]:
            raise ValueError("Cannot create a file when old_text is non-empty")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload["new_text"], encoding="utf-8")
        print(f"created {payload['path']}")
        return 0
    original = path.read_text(encoding="utf-8")
    old = payload["old_text"]
    count = original.count(old)
    if count == 0:
        raise ValueError("old_text was not found")
    if count > 1 and not payload["replace_all"]:
        raise ValueError(f"old_text matched {count} locations; set replace_all=true or provide more context")
    updated = original.replace(old, payload["new_text"], -1 if payload["replace_all"] else 1)
    path.write_text(updated, encoding="utf-8")
    print(f"updated {payload['path']} ({count if payload['replace_all'] else 1} replacement(s))")
    return 0


def create_file(payload: dict, workspace: Path) -> int:
    path = _workspace_path(workspace, payload["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(payload["content"])
    print(f"created {payload['path']}")
    return 0


def delete_file(payload: dict, workspace: Path) -> int:
    path = _workspace_path(workspace, payload["path"])
    if not path.is_file():
        raise ValueError("Only existing files can be deleted")
    path.unlink()
    print(f"deleted {payload['path']}")
    return 0


def move_file(payload: dict, workspace: Path) -> int:
    source = _workspace_path(workspace, payload["source_path"])
    destination = _workspace_path(workspace, payload["destination_path"])
    if not source.is_file():
        raise ValueError("Source must be an existing file")
    if destination.exists():
        raise FileExistsError(f"Destination already exists: {payload['destination_path']}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    print(f"moved {payload['source_path']} to {payload['destination_path']}")
    return 0


def main() -> int:
    actions = {
        "apply-patch": apply_patch,
        "create-file": create_file,
        "delete-file": delete_file,
        "move-file": move_file,
    }
    if len(sys.argv) != 3 or sys.argv[1] not in actions:
        print("usage: tool_worker.py <apply-patch|create-file|delete-file|move-file> <base64-json>", file=sys.stderr)
        return 2
    try:
        payload = json.loads(base64.urlsafe_b64decode(sys.argv[2].encode("ascii")))
        workspace = Path(os.environ.get("TRACEFORGE_WORKSPACE_ROOT", "/workspace")).resolve()
        return actions[sys.argv[1]](payload, workspace)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
