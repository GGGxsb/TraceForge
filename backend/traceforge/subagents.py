from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import shlex
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

from .artifacts import clip_output
from .checkpoints import CheckpointStore
from .file_filter import is_ignored_path
from .model_adapter import ModelAdapter, usage_sink
from .models import ExecutionRequest, SessionHeader, ToolExecutionResult, new_id
from .security import PathGuard
from .storage import JsonlSession, SessionStore
from .subagent_context import CHILD_TOOLS, HISTORY_TYPES, child_definitions, project_child
from .tools import ToolService, validate_tool_arguments


SAFE_ID = re.compile(r"(?:session|subagent)_[a-f0-9]{32}\Z")
Emit = Callable[[JsonlSession, str, dict[str, Any], str], Awaitable[Any]]
logger = logging.getLogger(__name__)


class SubAgentManager:
    """Bounded read-only workers. Parent/child context and histories stay separate."""

    def __init__(self, root: Path, sessions: SessionStore, adapter: ModelAdapter,
                 tools: ToolService, checkpoints: CheckpointStore, *, emit: Emit | None = None,
                 max_turns: int = 8, timeout_seconds: float = 120, max_tokens: int = 24000) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.sessions, self.adapter, self.tools, self.checkpoints = sessions, adapter, tools, checkpoints
        self.emit = emit
        self.max_turns, self.timeout_seconds, self.max_tokens = max_turns, timeout_seconds, max_tokens
        self._slot = asyncio.Semaphore(1)
        self._counts: dict[tuple[str, str], int] = {}

    def _directory(self, parent_id: str, child_id: str | None = None) -> Path:
        ids = [parent_id] + ([child_id] if child_id is not None else [])
        if any(not SAFE_ID.fullmatch(value) for value in ids):
            raise ValueError("Invalid subagent identity")
        path = self.root
        for value in ids:
            path /= value
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("Subagent storage must not contain links")
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Subagent storage escapes its root")
        return path

    def get(self, parent_id: str, child_id: str) -> JsonlSession:
        self.sessions.get(parent_id)
        path = self._directory(parent_id, child_id) / "session.jsonl"
        if not path.is_file() or path.is_symlink():
            raise KeyError("Subagent not found")
        trace = JsonlSession.load(path)
        if trace.header.kind != "subagent" or trace.header.parent_session_id != parent_id or trace.header.id != child_id:
            raise ValueError("Subagent ownership mismatch")
        return trace

    async def _parent_event(self, parent: JsonlSession, kind: str, payload: dict, run_id: str) -> None:
        if self.emit:
            await self.emit(parent, kind, payload, run_id)
        else:
            await parent.append(kind, payload, run_id=run_id)

    async def _update(self, parent: JsonlSession, child: JsonlSession, status: str, **extra: Any) -> None:
        snapshot = next((entry.payload.get("checkpoint_id") for entry in child.entries if entry.type == "subagent_snapshot"), None)
        await self._parent_event(parent, "subagent_update", {
            "child_session_id": child.header.id, "role": child.header.role,
            "status": status, "snapshot_id": snapshot, **extra,
        }, child.header.parent_run_id or "")

    def _freeze_history(self, parent: JsonlSession, directory: Path) -> dict[tuple[str, str], dict]:
        records: dict[tuple[str, str], dict] = {}
        total = 0
        with (directory / "sources.jsonl").open("x", encoding="utf-8") as handle:
            for item in self.sessions.list():
                if item["workspace_id"] != parent.header.workspace_id:
                    continue
                source = self.sessions.get(item["id"])
                active = {entry.id for entry in source.get_branch()}
                for entry in list(source.entries):
                    if entry.type not in HISTORY_TYPES:
                        continue
                    row = {"session_id": source.header.id, "on_active_branch": entry.id in active,
                           "entry": entry.model_dump(mode="json")}
                    raw = json.dumps(row, ensure_ascii=False)
                    total += len(raw.encode("utf-8"))
                    if total > 128 * 1024 * 1024 or len(records) >= 100000:
                        raise ValueError("Project history exceeds subagent snapshot limit; narrow the task first")
                    handle.write(raw + "\n")
                    records[(source.header.id, entry.id)] = row
        return records

    def _export_snapshot(self, parent: JsonlSession, directory: Path) -> dict:
        captured = self.checkpoints.capture(parent, path_filter=lambda path: not any(
            part.lower().startswith(".env") for part in path.parts))
        manifest = self.checkpoints.manifest(parent, str(captured["checkpoint_id"]))
        code = directory / "workspace"
        code.mkdir()
        blobs = self.checkpoints.root / parent.header.id / "blobs"
        for name, info in manifest["files"].items():
            relative = Path(name)
            if relative.is_absolute() or relative.drive or ".." in relative.parts:
                raise ValueError("Invalid snapshot file path")
            # Defense in depth: .env variants and repository metadata are not code evidence.
            if any(part == ".git" or part.lower().startswith(".env") for part in relative.parts):
                continue
            target = code / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            PathGuard(code).resolve(name, allow_missing=True)
            blob = blobs / str(info["sha256"])
            if blob.is_symlink() or not blob.resolve().is_relative_to(blobs.resolve()):
                raise ValueError("Unsafe checkpoint blob")
            data = blob.read_bytes()
            if hashlib.sha256(data).hexdigest() != info["sha256"]:
                raise ValueError("Snapshot checksum mismatch")
            target.write_bytes(data)
        return captured

    async def _freeze_git(self, workspace: str) -> dict[str, str]:
        """Read fixed Git operations, excluding secrets and external diff drivers."""
        evidence: dict[str, str] = {}
        common = ["-c", "core.fsmonitor=false"]
        guard = PathGuard(workspace)
        for name in ("git_status", "git_diff"):
            try:
                if name == "git_status":
                    evidence[name] = await self.tools._git(workspace, common + ["status", "--short", "--branch"])
                    continue
                names = await self.tools._git(workspace, common + [
                    "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", "HEAD",
                ])
                if names == "Workspace is not a Git repository":
                    evidence[name] = names
                    continue
                allowed = []
                for raw in names.split("\0"):
                    if not raw:
                        continue
                    relative = Path(raw)
                    if is_ignored_path(relative) or any(part.lower().startswith(".env") for part in relative.parts):
                        continue
                    try:
                        guard.resolve(raw, allow_missing=True)
                    except (OSError, ValueError, PermissionError):
                        continue
                    allowed.append(raw)
                parts = ["[Sensitive and generated paths excluded]\n"]
                # Bound Windows command-line size; literal pathspecs prevent pathspec injection.
                for offset in range(0, len(allowed), 16):
                    parts.append(await self.tools._git(workspace, common + [
                        "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "HEAD", "--",
                        *(f":(literal){path}" for path in allowed[offset:offset + 16]),
                    ]))
                    if sum(len(part) for part in parts) > 4 * 1024 * 1024:
                        raise ValueError("Git evidence exceeds the 4 MiB limit")
                evidence[name] = "".join(parts)
            except Exception as exc:
                evidence[name] = f"Git snapshot unavailable: {type(exc).__name__}: {exc}"
        return evidence

    async def delegate(self, parent: JsonlSession, run_id: str, call_id: str, arguments: dict,
                       *, context_window: int, budget_check: Callable[[], bool]) -> ToolExecutionResult:
        branch = parent.get_branch()
        by_id = {entry.id: entry for entry in branch}
        selected = arguments.get("context_entry_ids", [])
        if any(entry_id not in by_id or by_id[entry_id].type not in HISTORY_TYPES for entry_id in selected):
            return ToolExecutionResult(output="Selected context must be readable entries on the active parent branch", is_error=True)
        existing = [entry for entry in parent.entries if entry.type == "subagent_spawn" and entry.run_id == run_id]
        key = (parent.header.id, run_id)
        count = max(len(existing), self._counts.get(key, 0))
        if count >= 2:
            return ToolExecutionResult(output="Subagent delegation limit reached (two per parent run)", is_error=True)
        if any(entry.payload.get("call_id") == call_id for entry in existing):
            return ToolExecutionResult(output="This delegation call has already been started; it will not be replayed", is_error=True)
        spawn = next((entry for entry in reversed(branch) if entry.type == "tool_call"
                      and entry.payload.get("call_id") == call_id), None)
        if spawn is None:
            return ToolExecutionResult(output="Delegation must originate from a persisted parent tool call", is_error=True)
        self._counts[key] = count + 1
        child_id = new_id("subagent")
        directory = self._directory(parent.header.id, child_id)
        directory.mkdir(parents=True)
        child = JsonlSession.create(directory / "session.jsonl", SessionHeader(
            id=child_id, workspace_id=parent.header.workspace_id, workspace=str(directory / "workspace"),
            kind="subagent", parent_session_id=parent.header.id, parent_run_id=run_id,
            spawn_entry_id=spawn.id, role=arguments["role"],
        ))
        await self._parent_event(parent, "subagent_spawn", {
            "child_session_id": child_id, "call_id": call_id, "role": arguments["role"],
            "task": arguments["task"], "spawn_entry_id": spawn.id, "status": "queued",
            "permission_ceiling": "project_read_only", "context_entry_ids": selected,
        }, run_id)
        await child.append("run_state", {"status": "queued"}, run_id=child_id)
        report: dict[str, Any] = {"status": "failed", "summary": "Subtask did not complete", "findings": [], "unresolved": []}
        tokens = 0
        upstream = usage_sink.get()

        async def record_usage(record: dict) -> None:
            nonlocal tokens
            tokens += int(record.get("total_tokens") or 0)
            annotated = {**record, "source": "subagent", "child_session_id": child_id}
            await child.append("model_usage", annotated, run_id=child_id)
            if upstream:
                await upstream(annotated)

        sink = usage_sink.set(record_usage)
        try:
            async with asyncio.timeout(self.timeout_seconds), self._slot:
                if not budget_check():
                    raise ValueError("Parent budget exhausted")
                if arguments["role"] == "reviewer":
                    ready = await self.tools.sandbox.inspect()
                    if not ready.ready:
                        raise ValueError(f"Sandbox unavailable: {ready.reason}")
                captured = await asyncio.to_thread(self._export_snapshot, parent, directory)
                # Header stays immutable; the snapshot binding is an append-only record.
                await child.append("subagent_snapshot", {**captured, "workspace_read_only": True,
                                                         "network": False}, run_id=child_id)
                records = await asyncio.to_thread(self._freeze_history, parent, directory)
                git = await self._freeze_git(parent.workspace)
                handoff = None
                if self.tools.handoff_store:
                    try:
                        if self.tools.handoff_store.read(parent.header.workspace_id):
                            handoff = self.tools.handoff_store.read_body(parent.header.workspace_id)
                    except (OSError, ValueError):
                        pass
                (directory / "evidence.json").write_text(json.dumps({"git": git, "handoff": handoff}, ensure_ascii=False), encoding="utf-8")
                last_user = next((entry for entry in reversed(branch) if entry.type == "user_message"), None)
                seed = {"task": arguments["task"], "parent_request": str(last_user.payload.get("content", ""))[:6000] if last_user else "",
                        "selected_context": [by_id[key].model_dump(mode="json") for key in selected],
                        "snapshot_id": captured["checkpoint_id"],
                        "handoff_notice": "有历史交接文件，可按需 read_project_handoff。" if handoff else "尚无项目交接文件。"}
                if len(json.dumps(seed, ensure_ascii=False)) > 24000:
                    raise ValueError("Selected context is too large; select fewer records")
                await child.append("user_message", {"content": json.dumps(seed, ensure_ascii=False)}, run_id=child_id)
                await child.append("run_state", {"status": "executing"}, run_id=child_id)
                await self._update(parent, child, "executing", task=arguments["task"])
                report = await self._loop(parent, child, records, git, handoff, context_window,
                                          lambda: tokens < self.max_tokens and budget_check())
        except TimeoutError:
            report = self._partial_report(child, "Subtask timed out", "Time budget exhausted", status="timeout")
        except asyncio.CancelledError:
            report = self._partial_report(child, "Parent cancelled the subtask", "Task cancelled", status="cancelled")
            await self._finish(parent, child, report, tokens)
            raise
        except Exception as exc:
            report = self._partial_report(child, f"{type(exc).__name__}: {exc}",
                                          "Subtask failed; no host fallback was used", status="failed")
        finally:
            usage_sink.reset(sink)
        return await self._finish(parent, child, report, tokens)

    @staticmethod
    def _partial_report(child: JsonlSession, summary: str, unresolved: str, *, status: str = "partial") -> dict:
        """Return collected provenance, without inventing a conclusion after interruption."""
        outputs = {entry.payload.get("call_id"): entry for entry in child.entries if entry.type == "tool_result"}
        references: list[dict] = []
        files: list[str] = []
        for entry in child.entries:
            if entry.type != "tool_call":
                continue
            result = outputs.get(entry.payload.get("call_id"))
            if result is None or result.payload.get("is_error"):
                continue
            if entry.payload.get("name") == "read_history_entry":
                args = entry.payload["arguments"]
                reference = {"session_id": args["session_id"], "entry_id": args["entry_id"]}
                if reference not in references:
                    references.append(reference)
            elif entry.payload.get("name") == "read_file":
                path = entry.payload["arguments"]["path"]
                if path not in files:
                    files.append(path)
        findings = []
        if references:
            findings.append({"description": "已读取的原始历史节点，尚未形成结论，请继续核对。",
                             "file": None, "line": None, "evidence": references[-8:]})
        for path in files[-4:]:
            findings.append({"description": "已读取冻结快照文件，尚未完成审查。", "file": path, "line": None, "evidence": []})
        return {"status": status, "summary": summary, "findings": findings, "unresolved": [unresolved]}

    async def _finish(self, parent: JsonlSession, child: JsonlSession, report: dict, tokens: int) -> ToolExecutionResult:
        # Every interrupted model call gets exactly one result before termination.
        result_ids = {entry.payload.get("call_id") for entry in child.entries if entry.type == "tool_result"}
        for entry in list(child.entries):
            if entry.type == "tool_call" and entry.payload.get("call_id") not in result_ids:
                await child.append("tool_result", {"call_id": entry.payload["call_id"], "tool_name": entry.payload["name"],
                                                   "output": "Subtask interrupted", "is_error": True}, run_id=child.header.id)
                result_ids.add(entry.payload["call_id"])
        snapshot = next((entry.payload.get("checkpoint_id") for entry in child.entries if entry.type == "subagent_snapshot"), None)
        result = {**report, "child_session_id": child.header.id, "role": child.header.role,
                  "snapshot_id": snapshot, "total_tokens": tokens,
                  "evidence_only": True, "requires_current_code_check": True}
        await child.append("subagent_result", result, run_id=child.header.id)
        await child.append("run_state", {"status": result["status"]}, run_id=child.header.id)
        await self._update(parent, child, result["status"], summary=result["summary"], total_tokens=tokens, snapshot_id=snapshot)
        raw = json.dumps(result, ensure_ascii=False)
        artifact = self.tools.artifacts.save_text(child.header.id, raw)
        projection = {**result, "summary": str(result["summary"])[:3000],
                      "findings": list(result["findings"]),
                      "unresolved": [str(item)[:500] for item in result["unresolved"][:4]],
                      "omitted_findings": 0, "omitted_unresolved": max(0, len(result["unresolved"]) - 4),
                      "report_truncated": len(str(result["summary"])) > 3000,
                      "full_report_artifact_id": artifact["id"]}
        while len(json.dumps(projection, ensure_ascii=False)) > 8000:
            projection["report_truncated"] = True
            if projection["findings"]:
                projection["findings"].pop()
                projection["omitted_findings"] += 1
            elif projection["unresolved"]:
                projection["unresolved"].pop()
                projection["omitted_unresolved"] += 1
            else:
                projection["summary"] = projection["summary"][:1000]
        return ToolExecutionResult(output=json.dumps(projection, ensure_ascii=False),
                                   is_error=result["status"] in {"failed", "cancelled", "timeout"},
                                   artifact_id=artifact["id"], metadata={"subagent": {
                                       key: result[key] for key in ("child_session_id", "role", "snapshot_id", "status")}})

    async def _loop(self, parent: JsonlSession, child: JsonlSession, records: dict, git: dict,
                    handoff: str | None, context_window: int, budget_check: Callable[[], bool]) -> dict:
        searched: set[tuple[str, str]] = set()
        read: set[tuple[str, str]] = set()
        files_read: set[str] = set()
        definitions = {tool["name"]: tool for tool in child_definitions()}
        for turn in range(self.max_turns):
            instructions, items, estimate = project_child(child, child.header.role or "explore")
            reserve = min(4000, max(256, context_window // 4))
            if not budget_check() or estimate >= context_window - reserve:
                return self._partial_report(child, "Budget/context limit reached; collected evidence is not a completed conclusion",
                                            "Further investigation required")
            calls, reasoning, text = [], [], []
            async for delta in self.adapter.stream_turn(instructions, items, list(definitions.values())):
                if delta.type == "text_delta":
                    text.append(delta.text)
                elif delta.type == "tool_call" and delta.tool_call:
                    calls.append(delta.tool_call)
                elif delta.type == "reasoning_item" and delta.raw.get("type") == "reasoning":
                    reasoning.append(delta.raw)
                elif delta.type == "model_fallback":
                    await child.append("model_change", delta.raw, run_id=child.header.id)
                elif delta.type == "error":
                    raise RuntimeError(delta.text)
            for item in reasoning:
                await child.append("model_reasoning", {"item": item}, run_id=child.header.id)
            if text:
                await child.append("assistant_message", {"content": "".join(text)}, run_id=child.header.id)
            if len(calls) > 16:
                raise ValueError("Subagent tool batch limit reached (16 calls)")
            for call in calls:
                if any(entry.type == "tool_call" and entry.payload.get("call_id") == call.call_id for entry in child.entries):
                    raise ValueError("Duplicate child tool call id")
                await child.append("tool_call", {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}, run_id=child.header.id)
            finished = None
            for call in calls:
                try:
                    if finished is not None or call.name not in definitions:
                        raise PermissionError("Tool is unavailable under the subagent read-only ceiling")
                    args = validate_tool_arguments(definitions[call.name], call.arguments)
                    if call.name == "finish_subtask":
                        for finding in args["findings"]:
                            for ref in finding["evidence"]:
                                if (ref["session_id"], ref["entry_id"]) not in read:
                                    raise ValueError("History evidence must first be read with read_history_entry")
                            if finding["file"] is not None and finding["file"] not in files_read:
                                raise ValueError("File evidence must first be read with read_file")
                            if finding["line"] is not None and finding["file"] is None:
                                raise ValueError("A line citation requires a file")
                            if finding["line"] is not None:
                                path = PathGuard(child.header.workspace).resolve(finding["file"])
                                if finding["line"] > len(path.read_text(encoding="utf-8").splitlines()):
                                    raise ValueError("File line citation is outside the snapshot")
                        finished = {"status": "completed", **args}
                        result = ToolExecutionResult(output="Report accepted by the parent boundary")
                    else:
                        result = await self._read_tool(child, call.name, args, records, searched, read, files_read, git, handoff)
                except Exception as exc:
                    result = ToolExecutionResult(output=f"{type(exc).__name__}: {exc}", is_error=True)
                await child.append("tool_result", {"call_id": call.call_id, "tool_name": call.name,
                                                    **result.model_dump(mode="json")}, run_id=child.header.id)
            await self._update(parent, child, "executing", turn=turn + 1,
                               tool_count=sum(entry.type == "tool_call" for entry in child.entries))
            if finished:
                return finished
            if not calls:
                await child.append("user_message", {"content": "请调用 finish_subtask 返回带证据的结果；还缺少的信息写入 unresolved。"}, run_id=child.header.id)
        return self._partial_report(child, "Subagent turn limit reached", "Further investigation required")

    async def _read_tool(self, child: JsonlSession, name: str, args: dict, records: dict,
                         searched: set, read: set, files_read: set, git: dict, handoff: str | None) -> ToolExecutionResult:
        if name not in CHILD_TOOLS:
            raise PermissionError("Subagent tool is not allowed")
        if name == "search_history":
            needle = args["query"].strip().casefold()
            if not needle or len(needle) > 200:
                raise ValueError("Search query must contain 1–200 characters")
            matches = []
            for key, row in reversed(list(records.items())):
                if args["scope"] == "session" and key[0] != child.header.parent_session_id:
                    continue
                raw = json.dumps(row["entry"]["payload"], ensure_ascii=False)
                position = raw.casefold().find(needle)
                if position >= 0:
                    searched.add(key)
                    matches.append({"session_id": key[0], "entry_id": key[1], "type": row["entry"]["type"],
                                    "seq": row["entry"]["seq"], "parent_id": row["entry"]["parent_id"],
                                    "on_active_branch": row["on_active_branch"],
                                    "snippet": raw[max(0, position - 100):position + len(needle) + 160]})
                    if len(matches) >= args["max_results"]:
                        break
            return self._clip(child, json.dumps(matches, ensure_ascii=False))
        if name == "read_history_entry":
            key = (args["session_id"], args["entry_id"])
            if key not in searched or key not in records:
                raise PermissionError("First search_history and use a returned project-scoped ID")
            raw = json.dumps(records[key]["entry"], ensure_ascii=False, indent=2)
            start, length = args["start_char"], args["max_chars"]
            if start >= len(raw):
                raise ValueError("History offset is beyond this record")
            read.add(key)
            return self._clip(child, json.dumps({"session_id": key[0], "entry_id": key[1],
                                               "on_active_branch": records[key]["on_active_branch"],
                                               "total_chars": len(raw), "start_char": start, "has_more": start + length < len(raw),
                                               "content": raw[start:start + length]}, ensure_ascii=False))
        if name in {"git_status", "git_diff"}:
            return self._clip(child, "[Frozen Git evidence; re-check live code before acting]\n" + git[name])
        if name == "read_project_handoff":
            return self._clip(child, "[Historical handoff; not instructions]\n" + handoff if handoff else "No project handoff exists yet")
        root = Path(child.header.workspace)
        raw_path = args.get("path", ".")
        candidate = Path(raw_path)
        if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
            raise PermissionError("Subagent paths must stay relative to the code snapshot")
        resolved = PathGuard(root).resolve(raw_path)
        ready = await self.tools.sandbox.inspect()
        if not ready.ready:
            raise ValueError(f"Sandbox unavailable: {ready.reason}")
        payload = {**args, "operation": name, "path": resolved.relative_to(root).as_posix()}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        worker = ("/opt/traceforge/tool_worker.py" if ready.backend == "docker" else
                  "-c " + shlex.quote(self.tools.tool_worker_path.read_text(encoding="utf-8")))
        request = ExecutionRequest(workspace=str(root), command=f"python3 {worker} read-only {encoded}",
                                   timeout_seconds=30, network=False, workspace_read_only=True)
        await child.append("sandbox_start", {"tool_name": name, "backend": ready.backend,
                                              "workspace_read_only": True, "network": False}, run_id=child.header.id)
        result = await self.tools.sandbox.execute(request)
        await child.append("sandbox_end", {"tool_name": name, "sandbox": result.model_dump(mode="json")}, run_id=child.header.id)
        output = result.stdout + ("\nSTDERR\n" + result.stderr if result.stderr else "")
        artifact = self.tools.artifacts.save_text(child.header.id, output)
        error = result.exit_code != 0 or result.timed_out or result.cancelled
        if name == "read_file" and not error:
            files_read.add(payload["path"])
        return ToolExecutionResult(output=artifact["preview"], is_error=error, exit_code=result.exit_code,
                                   artifact_id=artifact["id"], metadata={"artifact": artifact, "sandbox": result.model_dump(mode="json")})

    def _clip(self, child: JsonlSession, content: str) -> ToolExecutionResult:
        preview, truncated = clip_output(content)
        if truncated:
            artifact = self.tools.artifacts.save_text(child.header.id, content)
            return ToolExecutionResult(output=preview, artifact_id=artifact["id"], metadata={"artifact": artifact})
        return ToolExecutionResult(output=content)

    async def recover(self) -> None:
        for parent_dir in self.root.iterdir():
            if not parent_dir.is_dir() or not SAFE_ID.fullmatch(parent_dir.name):
                continue
            try:
                parent = self.sessions.get(parent_dir.name)
            except KeyError:
                continue
            for directory in parent_dir.iterdir():
                if not directory.is_dir() or not SAFE_ID.fullmatch(directory.name):
                    continue
                try:
                    child = self.get(parent.header.id, directory.name)
                    final = next((entry for entry in reversed(child.entries) if entry.type == "subagent_result"), None)
                    if final is None:
                        tokens = sum(int(entry.payload.get("total_tokens") or 0) for entry in child.entries if entry.type == "model_usage")
                        await self._finish(parent, child, {"status": "failed", "summary": "Backend restart interrupted this subtask",
                                                         "findings": [], "unresolved": ["Subtask was not automatically replayed"]}, tokens)
                    elif child.entries[-1].type != "run_state":
                        # A crash can occur between the final report and its terminal state.
                        await child.append("run_state", {"status": final.payload["status"]}, run_id=child.header.id)
                        await self._update(parent, child, final.payload["status"], summary=final.payload["summary"])
                except (KeyError, ValueError, OSError):
                    # Keep the corrupt log for diagnosis; one child must not prevent startup.
                    logger.warning("Cannot recover subagent %s/%s", parent.header.id, directory.name, exc_info=True)

    def delete(self, parent_id: str) -> None:
        directory = self._directory(parent_id)
        if not directory.exists():
            return
        for child_dir in directory.iterdir():
            if SAFE_ID.fullmatch(child_dir.name):
                artifact_dir = self.tools.artifacts.root / child_dir.name
                if artifact_dir.exists():
                    if artifact_dir.is_symlink() or not artifact_dir.resolve().is_relative_to(self.tools.artifacts.root.resolve()):
                        raise ValueError("Unsafe subagent artifact path")
                    shutil.rmtree(artifact_dir)
        shutil.rmtree(directory)
