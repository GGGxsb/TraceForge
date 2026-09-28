from __future__ import annotations

import asyncio
import difflib
import json
from pathlib import Path
from typing import Any

from .file_filter import is_ignored_path
from .security import PathGuard
from .storage import WorkspaceStore


class WorkspaceInspector:
    def __init__(self, store: WorkspaceStore) -> None:
        self.store = store

    def capture_directory_baseline(self, workspace_id: str) -> None:
        workspace = self.store.get(workspace_id)
        if workspace.kind == "git":
            return
        target = self.store.root / f"{workspace_id}.snapshot.json"
        if target.exists():
            return
        root = Path(workspace.path)
        guard = PathGuard(root)
        snapshot: dict[str, str] = {}
        for path in root.rglob("*"):
            if not path.is_file() or is_ignored_path(path.relative_to(root)):
                continue
            try:
                guard.resolve(str(path.relative_to(root)))
                if path.stat().st_size <= 256 * 1024:
                    snapshot[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, PermissionError):
                continue
            if len(snapshot) >= 2000:
                break
        target.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    def files(self, workspace_id: str, path_value: str = ".", max_depth: int = 5,
              *, path_override: str | None = None) -> list[dict[str, Any]]:
        workspace = self.store.get(workspace_id)
        workspace_path = path_override or workspace.path
        guard = PathGuard(workspace_path)
        root = guard.resolve(path_value)
        base_depth = len(root.parts)
        result: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(Path(workspace_path))
            if is_ignored_path(relative):
                continue
            try:
                guard.resolve(str(relative))
            except PermissionError:
                continue
            if len(path.parts) - base_depth > max_depth:
                continue
            result.append(
                {
                    "path": str(relative).replace("\\", "/"),
                    "name": path.name,
                    "type": "directory" if path.is_dir() else "file",
                    "size": path.stat().st_size if path.is_file() else None,
                }
            )
            if len(result) >= 3000:
                break
        return result

    def read_file(self, workspace_id: str, path_value: str, *, path_override: str | None = None) -> str:
        workspace = self.store.get(workspace_id)
        path = PathGuard(path_override or workspace.path).resolve(path_value)
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("File is not readable or exceeds 2 MiB")
        return path.read_text(encoding="utf-8")

    async def diff(self, workspace_id: str, *, path_override: str | None = None) -> str:
        workspace = self.store.get(workspace_id)
        workspace_path = path_override or workspace.path
        if workspace.kind == "git":
            async def git(*args: str) -> str:
                process = await asyncio.create_subprocess_exec(
                    "git",
                    *args,
                    cwd=workspace_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
                if process.returncode:
                    raise RuntimeError(stderr.decode(errors="replace"))
                return stdout.decode(errors="replace")

            unstaged, staged, porcelain = await asyncio.gather(
                git("diff", "--no-ext-diff", "--"),
                git("diff", "--no-ext-diff", "--cached", "--"),
                git("status", "--porcelain", "-z"),
            )
            untracked_chunks: list[str] = []
            for record in porcelain.split("\0"):
                if not record.startswith("?? "):
                    continue
                relative = record[3:]
                try:
                    path = PathGuard(workspace_path).resolve(relative)
                    if path.is_file() and path.stat().st_size <= 256 * 1024:
                        content = path.read_text(encoding="utf-8").splitlines(keepends=True)
                        untracked_chunks.extend(
                            difflib.unified_diff([], content, fromfile="/dev/null", tofile=f"b/{relative}")
                        )
                except (OSError, UnicodeDecodeError, PermissionError):
                    continue
            return "\n".join(
                part
                for part in (
                    f"UNSTAGED\n{unstaged}" if unstaged else "",
                    f"STAGED\n{staged}" if staged else "",
                    f"UNTRACKED\n{''.join(untracked_chunks)}" if untracked_chunks else "",
                )
                if part
            )
        snapshot_path = self.store.root / f"{workspace_id}.snapshot.json"
        baseline = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else {}
        baseline = {name: content for name, content in baseline.items() if not is_ignored_path(Path(name))}
        root = Path(workspace_path)
        guard = PathGuard(root)
        current: dict[str, str] = {}
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if not path.is_file() or is_ignored_path(relative):
                continue
            try:
                guard.resolve(str(relative))
                if path.stat().st_size <= 256 * 1024:
                    current[str(relative)] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, PermissionError):
                continue
        chunks: list[str] = []
        for name in sorted(set(baseline) | set(current)):
            try:
                guard.resolve(name, allow_missing=True)
            except PermissionError:
                continue
            before = baseline.get(name, "").splitlines(keepends=True)
            after = current.get(name, "").splitlines(keepends=True)
            if before != after:
                chunks.extend(
                    difflib.unified_diff(before, after, fromfile=f"a/{name}", tofile=f"b/{name}")
                )
        return "".join(chunks)
