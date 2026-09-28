from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from uuid import uuid4

from .file_filter import is_ignored_path
from .security import PathGuard
from .storage import JsonlSession


MAX_FILES = 20_000
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024


class CheckpointError(ValueError):
    pass


class CheckpointStore:
    """Byte-exact, content-addressed snapshots of code files, separate from JSONL."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _session_dir(self, session: JsonlSession) -> Path:
        return self.root / session.header.id

    def _save_blob(self, session: JsonlSession, digest: str, data: bytes) -> None:
        blob = self._session_dir(session) / "blobs" / digest
        if blob.exists():
            return
        blob.parent.mkdir(parents=True, exist_ok=True)
        temp = blob.with_name(f".{digest}.{uuid4().hex}.tmp")
        try:
            with temp.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, blob)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _git_run(workspace: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(["git", "-C", str(workspace), *args], capture_output=True,
                                  timeout=30, check=False)
        except subprocess.TimeoutExpired as exc:
            raise CheckpointError("Git checkpoint operation timed out") from exc

    @staticmethod
    def _git_state(workspace: Path) -> tuple[dict | None, Path | None]:
        if not (workspace / ".git").exists():
            return None, None

        def git(*args: str) -> str:
            result = CheckpointStore._git_run(workspace, *args)
            if result.returncode:
                raise CheckpointError(result.stderr.decode(errors="replace") or "Git state check failed")
            return result.stdout.decode(errors="replace").strip()

        branch_result = CheckpointStore._git_run(workspace, "symbolic-ref", "-q", "HEAD")
        branch = branch_result.stdout.decode(errors="replace").strip() if branch_result.returncode == 0 else ""
        head_result = CheckpointStore._git_run(workspace, "rev-parse", "--verify", "HEAD")
        head = head_result.stdout.decode(errors="replace").strip() if head_result.returncode == 0 else ""
        index_name = git("rev-parse", "--git-path", "index")
        index = Path(index_name)
        if not index.is_absolute():
            index = workspace / index
        index = index.resolve()
        if index.exists() and index.stat().st_size > MAX_FILE_BYTES:
            raise CheckpointError("Git index exceeds checkpoint size limit")
        data = index.read_bytes() if index.exists() else b""
        stage_result = CheckpointStore._git_run(workspace, "ls-files", "--stage", "-z")
        if stage_result.returncode:
            raise CheckpointError(stage_result.stderr.decode(errors="replace") or "Git stage check failed")
        return {"head": head, "branch": branch, "index_sha256": hashlib.sha256(data).hexdigest(),
                "stage_sha256": hashlib.sha256(stage_result.stdout).hexdigest(),
                "index_exists": index.exists()}, index

    @staticmethod
    def _reject_links(workspace: Path, relative: Path) -> None:
        part = workspace
        for component in relative.parts:
            part = part / component
            if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                raise CheckpointError(f"Linked workspace path cannot be restored: {relative.as_posix()}")

    def _paths(self, workspace: Path) -> list[Path]:
        if (workspace / ".git").exists():
            result = self._git_run(workspace, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
            if result.returncode:
                raise CheckpointError(result.stderr.decode(errors="replace") or "Git file inventory failed")
            names = {os.fsdecode(name) for name in result.stdout.split(b"\0") if name}
            return sorted((workspace / name for name in names), key=lambda path: str(path))
        paths: list[Path] = []
        data_dir = self.root.parent.resolve()
        for directory, dirs, files in os.walk(workspace, followlinks=False):
            base = Path(directory)
            for name in list(dirs):
                relative = (base / name).relative_to(workspace)
                if is_ignored_path(relative) or (base / name).resolve() == data_dir or name.lower() in PathGuard.SENSITIVE_PARTS:
                    dirs.remove(name)
                elif (base / name).is_symlink() or (hasattr(base / name, "is_junction") and (base / name).is_junction()):
                    raise CheckpointError(f"Cannot checkpoint linked directory: {relative}")
            paths.extend(base / name for name in files if not is_ignored_path((base / name).relative_to(workspace)))
        return sorted(paths, key=lambda path: str(path))

    def inventory(self, session: JsonlSession, *, save_blobs: bool = False,
                  workspace_override: str | None = None) -> dict[str, dict[str, int | str]]:
        workspace = Path(workspace_override or session.workspace).resolve(strict=True)
        data_dir = self.root.parent.resolve()
        if data_dir == workspace:
            raise CheckpointError("TraceForge data directory cannot be the workspace root")
        guard = PathGuard(workspace)
        files: dict[str, dict[str, int | str]] = {}
        total = 0
        for candidate in self._paths(workspace):
            relative = candidate.relative_to(workspace)
            name = relative.as_posix()
            # Credentials and generated directories are deliberately outside the
            # managed code state. They are never deleted during restore.
            if is_ignored_path(relative):
                continue
            if data_dir.is_relative_to(workspace) and candidate.is_relative_to(data_dir):
                continue
            if (any(part.lower() in PathGuard.SENSITIVE_PARTS for part in relative.parts)
                or relative.name.lower() in PathGuard.SENSITIVE_FILES
                or relative.suffix.lower() in {".pem", ".p12", ".pfx"}):
                continue
            self._reject_links(workspace, relative)
            try:
                guard.resolve(name, allow_missing=True)
            except PermissionError:
                continue
            if not candidate.exists():
                continue  # tracked file deleted in this checkpoint
            if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
                raise CheckpointError(f"Cannot checkpoint linked file: {name}")
            if not candidate.is_file():
                raise CheckpointError(f"Unsupported workspace entry: {name}")
            size = candidate.stat().st_size
            total += size
            if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
                raise CheckpointError(f"Checkpoint size limit exceeded at {name}")
            data = candidate.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            files[name] = {"sha256": digest, "size": len(data), "mode": stat.S_IMODE(candidate.stat().st_mode)}
            if save_blobs:
                self._save_blob(session, digest, data)
        return files

    def capture(self, session: JsonlSession) -> dict[str, str | int]:
        files = self.inventory(session, save_blobs=True)
        git_state, index = self._git_state(Path(session.workspace))
        if git_state and index and git_state["index_exists"]:
            self._save_blob(session, str(git_state["index_sha256"]), index.read_bytes())
        checkpoint_id = f"checkpoint_{uuid4().hex}"
        manifest = {"version": 1, "workspace": str(Path(session.header.workspace).resolve()),
                    "files": files, "git": git_state}
        data = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
        directory = self._session_dir(session)
        directory.mkdir(parents=True, exist_ok=True)
        temp = directory / f".{checkpoint_id}.tmp"
        target = directory / f"{checkpoint_id}.json"
        try:
            with temp.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        fingerprint = hashlib.sha256(data).hexdigest()
        return {"checkpoint_id": checkpoint_id, "fingerprint": fingerprint, "file_count": len(files),
                "total_bytes": sum(int(file["size"]) for file in files.values())}

    def manifest(self, session: JsonlSession, checkpoint_id: str) -> dict:
        if not checkpoint_id.startswith("checkpoint_") or len(checkpoint_id) != 43 or not all(
            char in "0123456789abcdef" for char in checkpoint_id[11:]
        ):
            raise CheckpointError("Invalid checkpoint id")
        path = self._session_dir(session) / f"{checkpoint_id}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CheckpointError("Checkpoint manifest is missing or invalid") from exc
        if raw.get("version") != 1 or raw.get("workspace") != str(Path(session.header.workspace).resolve()):
            raise CheckpointError("Checkpoint belongs to another workspace")
        if not isinstance(raw.get("files"), dict):
            raise CheckpointError("Checkpoint file list is invalid")
        return raw

    @staticmethod
    def checkpoint_for(session: JsonlSession, target_id: str) -> str | None:
        if target_id not in session.by_id:
            raise KeyError(f"Unknown target entry: {target_id}")
        for entry in reversed(session.get_branch(target_id)):
            if entry.type in {"workspace_checkpoint", "workspace_restore"}:
                return str(entry.payload["checkpoint_id"])
        return None

    def preview(self, session: JsonlSession, target_id: str, *, workspace_override: str | None = None,
                allow_cross_head: bool = False) -> dict:
        checkpoint_id = self.checkpoint_for(session, target_id)
        if checkpoint_id is None:
            return {"available": False, "reason": "该历史节点没有代码检查点；旧会话无法还原当时的文件。", "changes": []}
        target_manifest = self.manifest(session, checkpoint_id)
        target = target_manifest["files"]
        current = self.inventory(session, workspace_override=workspace_override)
        current_git, _ = self._git_state(Path(workspace_override or session.workspace))
        target_git = target_manifest.get("git")
        if bool(target_git) != bool(current_git) or (target_git and current_git and
                                                      target_git["head"] != current_git["head"]
                                                      and not allow_cross_head):
            return {"available": False, "reason": "Git HEAD 已变化；当前版本不能安全地跨提交恢复代码。",
                    "changes": []}
        changes = []
        for name in sorted(target.keys() | current.keys()):
            before, after = current.get(name), target.get(name)
            if before == after:
                continue
            changes.append({"path": name, "action": "create" if before is None else "delete" if after is None else "modify"})
        if target_git and current_git and self._stage_changed(target_git, current_git):
            changes.append({"path": "(Git 暂存区)", "action": "modify"})
        return {"available": True, "checkpoint_id": checkpoint_id, "changes": changes,
                "git_head_change": ({"from": current_git["head"], "to": target_git["head"]}
                                    if target_git and current_git and target_git["head"] != current_git["head"]
                                    else None),
                "current_fingerprint": self._fingerprint({"files": current, "git": self._semantic_git(current_git)})}

    @staticmethod
    def _fingerprint(files: dict) -> str:
        return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _semantic_git(git_state: dict | None) -> dict | None:
        if git_state is None:
            return None
        return {key: git_state.get(key) for key in ("head", "branch", "stage_sha256")}

    @staticmethod
    def _stage_changed(target: dict, current: dict) -> bool:
        if "stage_sha256" in target:
            return target["stage_sha256"] != current["stage_sha256"]
        return (target["index_sha256"], target["index_exists"]) != (
            current["index_sha256"], current["index_exists"]
        )

    def live_fingerprint(self, session: JsonlSession, *, workspace_override: str | None = None) -> str:
        files = self.inventory(session, workspace_override=workspace_override)
        git_state, _ = self._git_state(Path(workspace_override or session.workspace))
        return self._fingerprint({"files": files, "git": self._semantic_git(git_state)})

    def status(self, session: JsonlSession) -> dict:
        current_fingerprint = self.live_fingerprint(session)
        if session.active_leaf_id is None or self.checkpoint_for(session, session.active_leaf_id) is None:
            return {"state": "unbound", "current_fingerprint": current_fingerprint,
                    "reason": "此会话尚无代码检查点", "changes": [], "checkpoint_id": None}
        preview = self.preview(session, session.active_leaf_id)
        if not preview["available"]:
            return {"state": "diverged", "current_fingerprint": current_fingerprint,
                    "reason": preview["reason"], "changes": [], "checkpoint_id": preview.get("checkpoint_id"),
                    "restorable": False}
        return {"state": "aligned" if not preview["changes"] else "diverged",
                "current_fingerprint": current_fingerprint,
                "reason": "工作区文件与当前会话检查点不同" if preview["changes"] else "",
                "changes": preview["changes"], "checkpoint_id": preview["checkpoint_id"],
                "restorable": True}

    def restore(self, session: JsonlSession, checkpoint_id: str, *, expected_current: str | None = None,
                workspace_override: str | None = None) -> list[dict]:
        target_manifest = self.manifest(session, checkpoint_id)
        target = target_manifest["files"]
        current = self.inventory(session, workspace_override=workspace_override)
        current_git, index_path = self._git_state(Path(workspace_override or session.workspace))
        target_git = target_manifest.get("git")
        if bool(target_git) != bool(current_git) or (target_git and current_git and
                                                      target_git["head"] != current_git["head"]):
            raise CheckpointError("Git HEAD 已变化；无法安全恢复代码")
        if expected_current is not None and self._fingerprint({"files": current, "git": self._semantic_git(current_git)}) != expected_current:
            raise CheckpointError("工作区在预览后发生变化，请重新查看差异。")
        workspace = Path(workspace_override or session.workspace).resolve(strict=True)
        guard = PathGuard(workspace)
        changes: list[dict] = []
        delete_names = {name for name in current if name not in target}
        replace_directories: list[Path] = []
        # Validate every destination and every blob before modifying the workspace.
        for name in sorted(target.keys() | current.keys()):
            before, after = current.get(name), target.get(name)
            if before == after:
                continue
            candidate = workspace / name
            guard.resolve(name, allow_missing=True)
            self._reject_links(workspace, Path(name))
            if candidate.is_symlink() or (candidate.exists() and not candidate.is_file() and not candidate.is_dir()):
                raise CheckpointError(f"Unsafe restore destination: {name}")
            if candidate.is_dir():
                if after is None:
                    raise CheckpointError(f"Unsafe restore destination: {name}")
                # A branch may replace a directory with a file. Only remove the
                # directory when every file inside it belongs to this restore.
                for root, dirs, files in os.walk(candidate, followlinks=False):
                    for child in [*dirs, *files]:
                        path = Path(root) / child
                        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                            raise CheckpointError(f"Unsafe restore destination: {name}")
                        if path.is_file() and path.relative_to(workspace).as_posix() not in delete_names:
                            raise CheckpointError(f"Unmanaged file blocks restore: {path.relative_to(workspace)}")
                replace_directories.append(candidate)
            if after is not None:
                blob = self._session_dir(session) / "blobs" / str(after["sha256"])
                if not blob.is_file() or hashlib.sha256(blob.read_bytes()).hexdigest() != after["sha256"]:
                    raise CheckpointError(f"Checkpoint data is missing or damaged: {name}")
            changes.append({"path": name, "action": "create" if before is None else "delete" if after is None else "modify"})
        index_changed = bool(target_git and current_git and index_path
                             and self._stage_changed(target_git, current_git))
        if index_changed and target_git and index_path:
            if index_path.with_name(index_path.name + ".lock").exists():
                raise CheckpointError("Git index is locked by another process")
            if target_git["index_exists"]:
                index_blob = self._session_dir(session) / "blobs" / target_git["index_sha256"]
                if not index_blob.is_file() or hashlib.sha256(index_blob.read_bytes()).hexdigest() != target_git["index_sha256"]:
                    raise CheckpointError("Checkpoint Git index is missing or damaged")
            changes.append({"path": "(Git 暂存区)", "action": "modify"})
        # Delete first so file/directory renames do not collide with each other.
        for change in changes:
            if change["action"] == "delete":
                (workspace / change["path"]).unlink()
        for directory in sorted(replace_directories, key=lambda item: len(item.parts), reverse=True):
            for root, _, _ in os.walk(directory, topdown=False, followlinks=False):
                Path(root).rmdir()
        for change in changes:
            name = change["path"]
            if name == "(Git 暂存区)":
                continue
            destination = workspace / name
            after = target.get(name)
            if after is None:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Re-check after mkdir in case a linked parent appeared concurrently.
            guard.resolve(name, allow_missing=True)
            blob = self._session_dir(session) / "blobs" / str(after["sha256"])
            temp = destination.with_name(f".{destination.name}.traceforge-{uuid4().hex}.tmp")
            try:
                shutil.copyfile(blob, temp)
                os.chmod(temp, int(after["mode"]))
                os.replace(temp, destination)
            finally:
                temp.unlink(missing_ok=True)
        if index_changed and target_git and index_path:
            if target_git["index_exists"]:
                temp = index_path.with_name(f".{index_path.name}.traceforge-{uuid4().hex}.tmp")
                try:
                    shutil.copyfile(self._session_dir(session) / "blobs" / target_git["index_sha256"], temp)
                    os.replace(temp, index_path)
                finally:
                    temp.unlink(missing_ok=True)
            else:
                index_path.unlink(missing_ok=True)
        return changes

    def delete_session(self, session_id: str) -> None:
        directory = self.root / session_id
        if directory.exists():
            if directory.parent.resolve() != self.root.resolve() or directory.is_symlink():
                raise CheckpointError("Unsafe checkpoint directory")
            shutil.rmtree(directory)
