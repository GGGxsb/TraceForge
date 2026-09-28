from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from .models import PermissionMode, RunEvent, SessionEntry, SessionHeader, WorkspaceRecord, new_id


def _json_line(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class JsonlSession:
    """Append-only session tree with an in-memory index."""

    def __init__(
        self,
        path: Path,
        header: SessionHeader,
        entries: list[SessionEntry],
        recovery_issues: list[str] | None = None,
    ) -> None:
        self.path = path
        self.header = header
        self.entries = entries
        self.by_id: dict[str, SessionEntry] = {entry.id: entry for entry in entries}
        self.children_by_parent: dict[str | None, list[str]] = defaultdict(list)
        for entry in entries:
            self.children_by_parent[entry.parent_id].append(entry.id)
        self.active_leaf_id = entries[-1].id if entries else None
        self._next_seq = max((entry.seq for entry in entries), default=0) + 1
        self._lock = asyncio.Lock()
        self.recovery_issues = recovery_issues or []
        self._project_root: str | None = None

    @property
    def project_root(self) -> str:
        """Current registered project root; the JSONL header remains immutable."""
        return self._project_root or self.header.workspace

    def bind_project_root(self, path: str) -> None:
        self._project_root = path

    @property
    def archived(self) -> bool:
        latest = next((entry for entry in reversed(self.entries) if entry.type == "session_archive"), None)
        return bool(latest.payload.get("archived")) if latest else False

    @property
    def permission_mode(self) -> PermissionMode:
        latest = next((entry for entry in reversed(self.entries) if entry.type == "permission_mode"), None)
        if latest:
            try:
                return PermissionMode(str(latest.payload.get("mode")))
            except ValueError:
                pass
        return PermissionMode.REQUEST_APPROVAL

    def workspace_for(self, entry_id: str | None = None) -> str:
        """Resolve the code checkout bound to a branch, never trusting arbitrary JSONL paths."""
        target = entry_id if entry_id is not None else self.active_leaf_id
        if target:
            managed_root = (self.path.parent.parent / "worktrees" / self.header.id).resolve()
            original = Path(self.project_root).resolve()
            digest = hashlib.sha256(str(original).encode("utf-8")).hexdigest()[:12]
            alternate_root = (original.parent / f".traceforge-worktrees-{digest}" / self.header.id).resolve()
            for entry in reversed(self.get_branch(target)):
                if entry.type != "workspace_binding":
                    continue
                candidate = Path(str(entry.payload.get("path", ""))).resolve()
                if any(candidate.is_relative_to(root) and candidate != root
                       for root in (managed_root, alternate_root)):
                    return str(candidate)
        return self.project_root

    @property
    def workspace(self) -> str:
        return self.workspace_for()

    @classmethod
    def create(cls, path: Path, header: SessionHeader) -> "JsonlSession":
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            os.write(fd, _json_line(header))
            os.fsync(fd)
        finally:
            os.close(fd)
        return cls(path, header, [])

    @classmethod
    def load(cls, path: Path) -> "JsonlSession":
        header: SessionHeader | None = None
        entries: list[SessionEntry] = []
        recovery_issues: list[str] = []
        seen_ids: set[str] = set()
        raw = path.read_bytes() if path.exists() else b""
        if raw and not raw.endswith(b"\n"):
            # Keep the partial bytes for forensics, but terminate that record so
            # the next append remains independently parseable.
            with path.open("ab") as handle:
                handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            recovery_issues.append("检测到未完成的末行，已隔离并修复换行")
        lines = raw.splitlines()
        for index, raw_line in enumerate(lines):
            if not raw_line.strip():
                continue
            try:
                data = json.loads(raw_line)
                if header is None:
                    header = SessionHeader.model_validate(data)
                else:
                    entry = SessionEntry.model_validate(data)
                    if entry.id in seen_ids:
                        original_id = entry.id
                        digest = hashlib.sha256(raw_line).hexdigest()[:10]
                        recovered_id = f"recovered_duplicate_{entry.seq}_{index}_{digest}"
                        while recovered_id in seen_ids:
                            recovered_id += "_x"
                        entry = entry.model_copy(
                            update={
                                "id": recovered_id,
                                "payload": {
                                    **entry.payload,
                                    "_recovery": {"duplicate_original_id": original_id},
                                },
                            }
                        )
                        recovery_issues.append(f"重复节点 ID {original_id} 已作为恢复节点加载")
                    seen_ids.add(entry.id)
                    entries.append(entry)
            except (json.JSONDecodeError, ValueError):
                # A malformed final record is the expected crash-recovery case.
                # Interior corrupt records are preserved on disk and ignored in memory.
                recovery_issues.append(f"第 {index + 1} 行无法解析，已保留原始字节并跳过")
                continue
        if header is None:
            raise ValueError(f"Invalid or missing session header: {path}")
        return cls(path, header, entries, recovery_issues)

    async def append(
        self,
        entry_type: str,
        payload: dict[str, Any],
        *,
        parent_id: str | None = None,
        run_id: str | None = None,
        durable: bool = True,
    ) -> SessionEntry:
        async with self._lock:
            resolved_parent = self.active_leaf_id if parent_id is None else parent_id
            if resolved_parent is not None and resolved_parent not in self.by_id:
                raise KeyError(f"Parent entry does not exist: {resolved_parent}")
            entry = SessionEntry(
                type=entry_type,
                parent_id=resolved_parent,
                seq=self._next_seq,
                run_id=run_id,
                payload=payload,
            )
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
            try:
                data = _json_line(entry)
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                if durable:
                    os.fsync(fd)
            finally:
                os.close(fd)
            self.entries.append(entry)
            self.by_id[entry.id] = entry
            self.children_by_parent[entry.parent_id].append(entry.id)
            self.active_leaf_id = entry.id
            self._next_seq += 1
            return entry

    def get_branch(self, leaf_id: str | None = None) -> list[SessionEntry]:
        current_id = self.active_leaf_id if leaf_id is None else leaf_id
        path: list[SessionEntry] = []
        seen: set[str] = set()
        while current_id:
            if current_id in seen:
                raise ValueError(f"Cycle detected in session tree at {current_id}")
            seen.add(current_id)
            entry = self.by_id.get(current_id)
            if entry is None:
                break
            path.append(entry)
            parent = self.by_id.get(entry.parent_id) if entry.parent_id else None
            # An append-only tree can only point to an earlier record. A bad
            # parent reference remains on disk, but must not make the active
            # branch (and therefore the whole session UI) unusable.
            current_id = parent.id if parent and parent.seq < entry.seq else None
        path.reverse()
        return path

    def get_tree(self) -> list[dict[str, Any]]:
        nodes = {
            entry.id: {"entry": entry.model_dump(mode="json"), "children": [], "orphaned": False}
            for entry in self.entries
        }
        roots: list[dict[str, Any]] = []
        for entry in self.entries:
            node = nodes[entry.id]
            if entry.parent_id is None:
                roots.append(node)
                continue
            parent = nodes.get(entry.parent_id)
            parent_entry = self.by_id.get(entry.parent_id)
            if parent is None or parent_entry is None or parent_entry.seq >= entry.seq:
                node["orphaned"] = True
                roots.append(node)
            else:
                parent["children"].append(node)
        return roots

    def get_turn_tree(self) -> list[dict[str, Any]]:
        """Project the lossless entry tree as one node per conversational turn.

        The projection never rewrites JSONL. Each node retains a safe raw entry
        target so branch switching still uses the original ancestry and LCA.
        """
        roots: list[dict[str, Any]] = []
        turns_by_entry_id: dict[str, dict[str, Any]] = {}
        turn_starts = {"user_message", "clarification_answer"}
        terminal_states = {"completed", "failed", "cancelled"}

        def visit(raw: dict[str, Any], owner: dict[str, Any] | None) -> None:
            entry = self.by_id[raw["entry"]["id"]]
            if entry.type in turn_starts or (owner is None and raw["orphaned"]):
                turn = {
                    "entry": raw["entry"],
                    "target_entry_id": entry.id,
                    "assistant_preview": "",
                    "question": "",
                    "event_count": 0,
                    "tool_count": 0,
                    "status": "",
                    "branch_tips": [],
                    "children": [],
                    "orphaned": bool(raw["orphaned"]),
                }
                if owner is None:
                    roots.append(turn)
                else:
                    owner["children"].append(turn)
                turns_by_entry_id[entry.id] = turn
                owner = turn

            elif owner is None:
                # Agent runs persist a briefing state before the user message.
                # It is raw history, not an extra conversation turn. Keep an
                # orphaned raw root visible for recovery diagnostics above.
                for child in raw["children"]:
                    visit(child, None)
                return

            elif (
                entry.type == "run_state"
                and entry.payload.get("status") == "briefing"
                and owner.get("_target_final")
            ):
                # The next run starts before its user message is committed.
                # Do not overwrite the previous turn's terminal status/count.
                for child in raw["children"]:
                    visit(child, owner)
                return

            owner["event_count"] += 1
            if entry.type == "assistant_message":
                owner["assistant_preview"] = str(entry.payload.get("content", ""))
            elif entry.type == "clarification_question":
                owner["question"] = str(entry.payload.get("question", ""))
            elif entry.type == "tool_call":
                owner["tool_count"] += 1
            elif entry.type == "run_state":
                owner["status"] = str(entry.payload.get("status", ""))

            # The first terminal run state ends this turn. Later branch-switch
            # metadata may share its raw ancestry, but must not move its target.
            if not owner.get("_target_final"):
                if entry.type == "run_state" and entry.payload.get("status") in terminal_states:
                    owner["target_entry_id"] = entry.id
                    owner["_target_final"] = True
                elif entry.type not in {"branch_switch", "branch_summary", "branch_resume"}:
                    owner["target_entry_id"] = entry.id

            for child in raw["children"]:
                visit(child, owner)

        for raw_root in self.get_tree():
            visit(raw_root, None)

        # A turn's canonical target is its first completed run. Separate
        # branch tips retain the exact leaves from which a user can resume an
        # existing path without treating that action as a fresh fork.
        candidate_tips = {self.active_leaf_id} if self.active_leaf_id else set()
        candidate_tips.update(
            str(entry.payload["from_id"])
            for entry in self.entries
            if entry.type == "branch_switch" and entry.payload.get("from_id") in self.by_id
        )
        superseded_tips = {
            ancestor.id
            for tip_id in candidate_tips
            for ancestor in self.get_branch(tip_id)[:-1]
            if ancestor.id in candidate_tips
        }
        candidate_tips.difference_update(superseded_tips)
        for tip_id in candidate_tips:
            tip = self.by_id.get(tip_id)
            if tip is None:
                continue
            owner = next(
                (turns_by_entry_id[entry.id] for entry in reversed(self.get_branch(tip_id))
                 if entry.id in turns_by_entry_id),
                None,
            )
            if owner is not None:
                owner["branch_tips"].append({
                    "entry_id": tip_id,
                    "seq": tip.seq,
                    "active": tip_id == self.active_leaf_id,
                })

        for turn in turns_by_entry_id.values():
            turn["branch_tips"].sort(key=lambda tip: (not tip["active"], -tip["seq"]))

        def strip_internal(node: dict[str, Any]) -> None:
            node.pop("_target_final", None)
            target_path = self.get_branch(node["target_entry_id"])
            node["checkpoint_id"] = next((
                str(entry.payload.get("checkpoint_id")) for entry in reversed(target_path)
                if entry.type in {"workspace_checkpoint", "workspace_restore"}
            ), None)
            for child in node["children"]:
                strip_internal(child)

        for root in roots:
            strip_internal(root)
        return roots

    def replay_events(self, after_seq: int = 0) -> list[RunEvent]:
        return [
            RunEvent(
                session_id=self.header.id,
                run_id=entry.run_id,
                seq=entry.seq,
                type=entry.type,
                timestamp=entry.timestamp,
                payload={"entry_id": entry.id, **entry.payload},
            )
            for entry in self.entries
            if entry.seq > after_seq
        ]

    def find_lca(self, first_id: str, second_id: str) -> str | None:
        first_ancestors = {entry.id for entry in self.get_branch(first_id)}
        second_path = self.get_branch(second_id)
        for entry in reversed(second_path):
            if entry.id in first_ancestors:
                return entry.id
        return None

    def abandoned_segment(self, old_leaf_id: str, target_id: str) -> tuple[str | None, list[SessionEntry]]:
        lca_id = self.find_lca(old_leaf_id, target_id)
        old_path = self.get_branch(old_leaf_id)
        if lca_id is None:
            return None, old_path
        start = next((index + 1 for index, entry in enumerate(old_path) if entry.id == lca_id), 0)
        return lca_id, old_path[start:]

    def validate_tool_pairs(self, leaf_id: str | None = None) -> list[str]:
        calls: dict[str, str] = {}
        results: dict[str, int] = defaultdict(int)
        errors: list[str] = []
        for entry in self.get_branch(leaf_id):
            if entry.type == "tool_call":
                call_id = str(entry.payload.get("call_id", ""))
                if not call_id or call_id in calls:
                    errors.append(f"Duplicate or missing tool call id at {entry.id}")
                else:
                    calls[call_id] = entry.id
            elif entry.type == "tool_result":
                call_id = str(entry.payload.get("call_id", ""))
                results[call_id] += 1
                if call_id not in calls:
                    errors.append(f"Tool result {entry.id} has no preceding call")
        for call_id in calls:
            if results[call_id] != 1:
                errors.append(f"Tool call {call_id} has {results[call_id]} results")
        return errors

    async def recover_unmatched_tool_calls(self) -> list[SessionEntry]:
        branch = self.get_branch()
        calls: dict[str, SessionEntry] = {}
        results: set[str] = set()
        for entry in branch:
            call_id = str(entry.payload.get("call_id", ""))
            if entry.type == "tool_call" and call_id:
                calls[call_id] = entry
            elif entry.type == "tool_result" and call_id:
                results.add(call_id)
        recovered: list[SessionEntry] = []
        for call_id, call in calls.items():
            if call_id in results:
                continue
            recovered.append(
                await self.append(
                    "tool_result",
                    {
                        "call_id": call_id,
                        "tool_name": call.payload.get("name"),
                        "output": "Tool execution was interrupted before a result was persisted.",
                        "is_error": True,
                        "synthetic": True,
                    },
                    run_id=call.run_id,
                )
            )
        return recovered

    async def recover_pending_approvals(self, reason: str = "server_restarted") -> list[SessionEntry]:
        branch = self.get_branch()
        requests: dict[str, SessionEntry] = {}
        decided: set[str] = set()
        for entry in branch:
            approval_id = str(entry.payload.get("approval_id", ""))
            if entry.type == "approval_request" and approval_id:
                requests[approval_id] = entry
            elif entry.type == "approval_decision" and approval_id:
                decided.add(approval_id)
        recovered: list[SessionEntry] = []
        for approval_id, request in requests.items():
            if approval_id in decided:
                continue
            recovered.append(
                await self.append(
                    "approval_decision",
                    {
                        "approval_id": approval_id,
                        "decision": "cancelled",
                        "reason": reason,
                        "fingerprint": request.payload.get("policy", {}).get("fingerprint", ""),
                        "synthetic": True,
                    },
                    run_id=request.run_id,
                )
            )
        return recovered

    async def recover_interrupted_run(self) -> SessionEntry | None:
        last_state = next((entry for entry in reversed(self.get_branch()) if entry.type == "run_state"), None)
        if not last_state or last_state.payload.get("status") in {"completed", "failed", "cancelled"}:
            return None
        return await self.append(
            "run_state",
            {
                "status": "failed",
                "error": "Server restarted before this run completed.",
                "synthetic": True,
            },
            run_id=last_state.run_id,
        )


class SessionStore:
    def __init__(self, root: Path, workspaces: WorkspaceStore | None = None) -> None:
        self.root = root
        self.workspaces = workspaces
        self.root.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, JsonlSession] = {}
        self._paths: dict[str, Path] = {}
        self._discover()

    def _discover(self) -> None:
        for path in self.root.glob("*.jsonl"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    first = handle.readline()
                header = SessionHeader.model_validate_json(first)
                self._paths[header.id] = path
            except (OSError, ValueError):
                continue

    def create(self, workspace: WorkspaceRecord) -> JsonlSession:
        session_id = new_id("session")
        header = SessionHeader(
            id=session_id,
            workspace_id=workspace.id,
            workspace=workspace.path,
        )
        path = self.root / f"{session_id}.jsonl"
        session = JsonlSession.create(path, header)
        session.bind_project_root(workspace.path)
        self._sessions[session_id] = session
        self._paths[session_id] = path
        return session

    def get(self, session_id: str) -> JsonlSession:
        if session_id in self._sessions:
            session = self._sessions[session_id]
            self._bind_project(session)
            return session
        path = self._paths.get(session_id)
        if path is None:
            raise KeyError(f"Unknown session: {session_id}")
        session = JsonlSession.load(path)
        self._bind_project(session)
        self._sessions[session_id] = session
        return session

    def _bind_project(self, session: JsonlSession) -> None:
        if self.workspaces is None:
            return
        try:
            session.bind_project_root(self.workspaces.get(session.header.workspace_id).path)
        except KeyError:
            pass

    def list(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for session_id in self._paths:
            try:
                session = self.get(session_id)
            except (OSError, ValueError):
                continue
            first_user = next((e for e in session.entries if e.type == "user_message"), None)
            results.append(
                {
                    "id": session_id,
                    "workspace_id": session.header.workspace_id,
                    "workspace": session.project_root,
                    "created_at": session.header.created_at,
                    "title": (first_user.payload.get("content", "")[:80] if first_user else "新会话"),
                    "entry_count": len(session.entries),
                    "turn_count": sum(entry.type in {"user_message", "clarification_answer"} for entry in session.entries),
                    "archived": session.archived,
                    "permission_mode": session.permission_mode.value,
                    "active_leaf_id": session.active_leaf_id,
                }
            )
        return sorted(results, key=lambda item: item["created_at"], reverse=True)

    def delete(self, session_id: str, artifacts_root: Path) -> None:
        session = self.get(session_id)
        path = self._paths[session_id]
        if path.parent.resolve() != self.root.resolve() or path.name != f"{session_id}.jsonl" or path.is_symlink():
            raise ValueError("Session path is outside the session store")
        artifact_dir = artifacts_root / session_id
        if artifact_dir.exists():
            if artifact_dir.parent.resolve() != artifacts_root.resolve() or artifact_dir.is_symlink() or (
                hasattr(artifact_dir, "is_junction") and artifact_dir.is_junction()
            ):
                raise ValueError("Artifact path is unsafe")
        path.unlink()
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
        self._sessions.pop(session.header.id, None)
        self._paths.pop(session.header.id, None)

    async def recover_all(self, checkpoints: Any | None = None, worktrees: Any | None = None) -> dict[str, int]:
        recovered_tools = 0
        recovered_approvals = 0
        recovered_runs = 0
        for session_id in list(self._paths):
            try:
                session = self.get(session_id)
                if checkpoints:
                    completed = {
                        str(entry.payload.get("transaction_id")) for entry in session.entries
                        if entry.type == "workspace_restore" and entry.payload.get("transaction_id")
                    }
                    for begin in [entry for entry in session.entries if entry.type == "workspace_restore_begin"
                                  and entry.payload.get("transaction_id") not in completed]:
                        switched = begin.payload.get("reason") == "branch_switch" and any(
                            entry.seq > begin.seq and entry.run_id == begin.run_id
                            and entry.type in {"branch_resume", "branch_summary"}
                            for entry in session.entries
                        )
                        checkpoint_id = str(begin.payload[
                            "to_checkpoint_id" if switched else "from_checkpoint_id"
                        ])
                        try:
                            source_workspace = str(begin.payload.get("from_workspace") or session.header.workspace)
                            target_workspace = str(begin.payload.get("to_workspace") or session.header.workspace)
                            isolated = bool(begin.payload.get("isolated"))
                            created_worktree = bool(begin.payload.get("created_worktree"))
                            binding_written = any(
                                entry.seq > begin.seq and entry.run_id == begin.run_id
                                and entry.type == "workspace_binding"
                                and entry.payload.get("path") == target_workspace for entry in session.entries
                            )
                            restore_workspace = target_workspace if switched else source_workspace
                            changes: list[dict[str, Any]] = []
                            if isolated:
                                if not worktrees:
                                    raise ValueError("Git worktree recovery is unavailable")
                                known_workspaces = {session.header.workspace, *(
                                    str(entry.payload.get("path")) for entry in session.entries
                                    if entry.type == "workspace_binding"
                                )}
                                if source_workspace not in known_workspaces:
                                    raise ValueError("Unknown source worktree in recovery record")
                                if created_worktree:
                                    worktrees._validate(session, target_workspace)
                                    if switched:
                                        if not Path(target_workspace).exists():
                                            manifest = checkpoints.manifest(session, checkpoint_id)
                                            worktrees.create(session, str(manifest["git"]["head"]),
                                                             path=target_workspace)
                                        changes = checkpoints.restore(
                                            session, checkpoint_id, workspace_override=target_workspace,
                                        )
                                        if not binding_written:
                                            await session.append("workspace_binding", {
                                                "path": target_workspace,
                                                "source_workspace": source_workspace,
                                                "checkpoint_id": checkpoint_id,
                                            }, run_id=begin.run_id)
                                    elif Path(target_workspace).exists():
                                        worktrees.remove(session, target_workspace)
                                elif switched and (target_workspace not in known_workspaces or
                                                   not Path(target_workspace).exists()):
                                    raise ValueError("Target branch worktree is missing")
                                # Existing worktrees are never overwritten or removed by recovery.
                            else:
                                backup = checkpoints.capture(session)
                                await session.append("workspace_checkpoint", {
                                    **backup, "reason": "before_restore_recovery",
                                }, run_id=begin.run_id)
                                changes = checkpoints.restore(session, checkpoint_id,
                                                              workspace_override=restore_workspace)
                            await session.append("workspace_restore", {
                                "checkpoint_id": checkpoint_id,
                                "target_entry_id": begin.payload.get("target_entry_id"),
                                "changed_files": changes,
                                "reason": "server_recovery",
                                "transaction_id": begin.payload.get("transaction_id"),
                                "workspace": restore_workspace,
                            }, run_id=begin.run_id)
                        except (OSError, ValueError) as exc:
                            session.recovery_issues.append(f"未完成的代码恢复需要人工检查：{exc}")
                recovered_tools += len(await session.recover_unmatched_tool_calls())
                recovered_approvals += len(await session.recover_pending_approvals())
                last_state = next((entry for entry in reversed(session.get_branch()) if entry.type == "run_state"), None)
                if checkpoints and last_state and last_state.payload.get("status") not in {
                    "completed", "failed", "cancelled"
                }:
                    try:
                        snapshot = checkpoints.capture(session)
                        await session.append("workspace_checkpoint", {**snapshot, "reason": "server_recovery"},
                                             run_id=last_state.run_id)
                    except (OSError, ValueError) as exc:
                        session.recovery_issues.append(f"工作区恢复检查点失败：{exc}")
                recovered_runs += int(await session.recover_interrupted_run() is not None)
            except (OSError, ValueError, KeyError):
                continue
        return {"tool_results": recovered_tools, "approvals": recovered_approvals, "runs": recovered_runs}


class WorkspaceStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.root / "workspaces.json"
        self._items: dict[str, WorkspaceRecord] = {}
        if self.registry_path.exists():
            try:
                raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
                self._items = {item["id"]: WorkspaceRecord.model_validate(item) for item in raw}
            except (OSError, ValueError, json.JSONDecodeError):
                self._items = {}

    def register(self, path_value: str) -> WorkspaceRecord:
        path = Path(path_value).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError("Workspace must be a directory")
        for item in self._items.values():
            if Path(item.path) == path:
                return item
        record = WorkspaceRecord(
            path=str(path),
            name=path.name or str(path),
            kind="git" if (path / ".git").exists() else "directory",
        )
        self._items[record.id] = record
        self._persist()
        return record

    def relocate(self, workspace_id: str, path_value: str) -> WorkspaceRecord:
        record = self.get(workspace_id)
        path = Path(path_value).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError("Project root must be a directory")
        if any(item.id != workspace_id and Path(item.path) == path for item in self._items.values()):
            raise ValueError("This folder is already registered as another project")
        kind = "git" if (path / ".git").exists() else "directory"
        if kind != record.kind:
            raise ValueError("The selected folder does not match the project's repository type")
        if Path(record.path) == path:
            return record
        updated = record.model_copy(update={"path": str(path)})
        self._items[workspace_id] = updated
        try:
            self._persist()
        except OSError:
            self._items[workspace_id] = record
            raise
        return updated

    def _persist(self) -> None:
        temp = self.registry_path.with_suffix(".tmp")
        temp.write_text(
            json.dumps([item.model_dump(mode="json") for item in self._items.values()], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, self.registry_path)

    def get(self, workspace_id: str) -> WorkspaceRecord:
        try:
            return self._items[workspace_id]
        except KeyError as exc:
            raise KeyError(f"Unknown workspace: {workspace_id}") from exc

    def list(self) -> list[WorkspaceRecord]:
        return list(self._items.values())


class EventHub:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[RunEvent]]] = defaultdict(set)

    async def publish(self, event: RunEvent) -> None:
        for queue in tuple(self._subscribers[event.session_id]):
            await queue.put(event)

    def subscribe(self, session_id: str) -> asyncio.Queue[RunEvent]:
        queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=1000)
        self._subscribers[session_id].add(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[RunEvent]) -> None:
        self._subscribers[session_id].discard(queue)
