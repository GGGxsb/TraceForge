from __future__ import annotations

import subprocess
import hashlib
from pathlib import Path
from uuid import uuid4

from .storage import JsonlSession


class WorktreeError(ValueError):
    pass


class WorktreeManager:
    """Managed Git checkouts for conversation forks; the original checkout is untouched."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        try:
            result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                    text=True, timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorktreeError(f"Git worktree operation failed: {exc}") from exc
        if result.returncode:
            raise WorktreeError(result.stderr.strip() or "Git worktree operation failed")
        return result.stdout.strip()

    def enabled(self, session: JsonlSession) -> bool:
        original = Path(session.project_root)
        if not (original / ".git").exists():
            return False
        try:
            return bool(self._git(original, "rev-parse", "--verify", "HEAD"))
        except WorktreeError:
            return False

    def _root_for(self, session: JsonlSession) -> Path:
        original = Path(session.project_root).resolve()
        if not self.root.is_relative_to(original):
            return self.root
        digest = hashlib.sha256(str(original).encode("utf-8")).hexdigest()[:12]
        return original.parent / f".traceforge-worktrees-{digest}"

    def _validate(self, session: JsonlSession, path: str | Path) -> Path:
        directory = Path(path).resolve()
        expected = self._root_for(session) / session.header.id
        if directory.parent != expected:
            raise WorktreeError("Worktree path is outside the managed session directory")
        return directory

    def allocate(self, session: JsonlSession) -> str:
        return str(self._validate(session, self._root_for(session) / session.header.id / f"fork_{uuid4().hex}"))

    def create(self, session: JsonlSession, head: str, *, path: str | None = None) -> str:
        if not self.enabled(session):
            raise WorktreeError("Git worktrees require a repository with a commit")
        if len(head) < 7 or any(char not in "0123456789abcdef" for char in head.lower()):
            raise WorktreeError("Invalid target Git commit")
        directory = self._validate(session, path or self.allocate(session))
        if directory.exists():
            raise WorktreeError("Managed worktree path already exists")
        directory.parent.mkdir(parents=True, exist_ok=True)
        self._git(Path(session.project_root), "worktree", "add", "--detach", str(directory), head)
        return str(directory)

    def remove(self, session: JsonlSession, path: str | Path, *, force: bool = False) -> None:
        directory = self._validate(session, path)
        if directory.exists():
            args = ("worktree", "remove", "--force", str(directory)) if force else (
                "worktree", "remove", str(directory))
            self._git(Path(session.project_root), *args)

    def delete_session(self, session: JsonlSession, checkpoints: object | None = None) -> list[str]:
        directory = self._root_for(session) / session.header.id
        if not directory.exists():
            return []
        if directory.resolve().parent != self._root_for(session):
            raise WorktreeError("Unsafe managed worktree directory")
        children = list(directory.iterdir())
        preserved: list[tuple[str, str]] = []
        # Preflight every checkout before removing any of them. Detached commits
        # get a named Git ref; uncommitted edits require the user to keep the session.
        for child in children:
            self._validate(session, child)
            if self._git(child, "status", "--porcelain"):
                raise WorktreeError(f"分支工作区仍有未提交的修改，请先处理后再删除会话：{child}")
            head = self._git(child, "rev-parse", "HEAD")
            binding = next((entry for entry in reversed(session.entries)
                            if entry.type == "workspace_binding" and entry.payload.get("path") == str(child)), None)
            initial_head = None
            if binding is not None and checkpoints is not None:
                checkpoint_id = binding.payload.get("checkpoint_id")
                if checkpoint_id:
                    manifest = checkpoints.manifest(session, str(checkpoint_id))
                    initial_head = manifest.get("git", {}).get("head")
            if head != initial_head:
                ref = f"traceforge-preserved/{session.header.id}/{child.name}"
                preserved.append((ref, head))
        for ref, head in preserved:
            try:
                existing = self._git(Path(session.project_root), "rev-parse", "--verify", f"refs/heads/{ref}")
            except WorktreeError:
                existing = None
            if existing and existing != head:
                raise WorktreeError(f"Preservation branch already points elsewhere: {ref}")
            if not existing:
                self._git(Path(session.project_root), "branch", ref, head)
        for child in children:
            self.remove(session, child)
        directory.rmdir()
        return [ref for ref, _ in preserved]
