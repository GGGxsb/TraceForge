from __future__ import annotations

import base64
import fnmatch
import json
import os
import sys
from pathlib import Path


def _workspace_path(workspace: Path, raw: str) -> Path:
    path = (workspace / raw).resolve()
    if os.environ.get("TRACEFORGE_FULL_ACCESS") != "1":
        scoped_paths = os.environ.get("TRACEFORGE_ALLOWED_PATHS")
        if scoped_paths:
            if str(path) not in set(json.loads(scoped_paths)):
                raise PermissionError(f"Path was not approved for this operation: {raw}")
        else:
            path.relative_to(workspace)
    return path


def apply_patch(payload: dict, workspace: Path) -> int:
    replace_all = payload.get("replace_all", False)
    if not isinstance(replace_all, bool):
        raise ValueError("replace_all must be a boolean")
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
    if not old:
        raise ValueError("old_text must be non-empty for an existing file. Read the file and provide exact text to replace.")
    count = original.count(old)
    if count == 0:
        raise ValueError("old_text was not found")
    if count > 1 and not replace_all:
        raise ValueError(f"old_text matched {count} locations; set replace_all=true or provide more context")
    updated = original.replace(old, payload["new_text"], -1 if replace_all else 1)
    path.write_text(updated, encoding="utf-8")
    print(f"updated {payload['path']} ({count if replace_all else 1} replacement(s))")
    return 0


def create_file(payload: dict, workspace: Path) -> int:
    path = _workspace_path(workspace, payload["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(payload["content"])
    except FileExistsError as exc:
        raise FileExistsError(
            f"File already exists: {payload['path']}. No changes made. "
            "Use read_file, then apply_patch with exact old_text to edit it."
        ) from exc
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


def read_only(payload: dict, workspace: Path) -> int:
    """Fixed read operations for subagents; never accept arbitrary commands."""
    action = payload["operation"]
    path = _workspace_path(workspace, payload.get("path", "."))
    if action == "read_file":
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Expected a text file no larger than 4 MiB")
        text = path.read_text(encoding="utf-8")
        if "\x00" in text:
            raise ValueError("Binary files cannot be read as text")
        lines = text.splitlines()
        start = payload.get("start_line") or 1
        end = payload.get("end_line") or len(lines)
        if end < start:
            raise ValueError("end_line must be greater than or equal to start_line")
        print("\n".join(f"{index:>6}  {line}" for index, line in enumerate(lines[start - 1:end], start)))
        return 0
    if action not in {"list_files", "search_code"} or not path.is_dir():
        raise ValueError("Unsupported read operation or directory")
    results = []
    for directory, dirs, files in os.walk(path, followlinks=False):
        current = Path(directory)
        depth = len(current.relative_to(path).parts)
        dirs[:] = sorted(name for name in dirs if not (current / name).is_symlink())
        if action == "list_files":
            if depth >= payload["max_depth"]:
                dirs[:] = []
            for candidate in [*(current / name for name in dirs), *(current / name for name in sorted(files))]:
                if candidate.is_symlink():
                    continue
                results.append(candidate.relative_to(workspace).as_posix() + ("/" if candidate.is_dir() else ""))
                if len(results) >= 2000:
                    print("\n".join([*results, "… file listing capped at 2000 entries …"]))
                    return 0
        else:
            for name in sorted(files):
                candidate = current / name
                if candidate.is_symlink() or (payload.get("glob") and not fnmatch.fnmatch(name, payload["glob"])):
                    continue
                if candidate.stat().st_size > 2 * 1024 * 1024:
                    continue
                try:
                    content = candidate.read_text(encoding="utf-8")
                except (UnicodeError, OSError):
                    continue
                if "\x00" in content:
                    continue
                for number, line in enumerate(content.splitlines(), 1):
                    if payload["query"].casefold() in line.casefold():
                        results.append(f"{candidate.relative_to(workspace).as_posix()}:{number}: {line[:500]}")
                        if len(results) >= 500:
                            print("\n".join([*results, "… search capped at 500 matches …"]))
                            return 0
    print("\n".join(results) or "No matches")
    return 0


def main() -> int:
    actions = {
        "apply-patch": apply_patch,
        "create-file": create_file,
        "delete-file": delete_file,
        "move-file": move_file,
        "read-only": read_only,
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
