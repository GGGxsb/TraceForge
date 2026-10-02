from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .context import CompactionService, ContextProjector
from .checkpoints import CheckpointStore
from .hooks import HookRegistry
from .model_adapter import ModelAdapter, usage_sink
from .models import (
    HookContext,
    HookPoint,
    PermissionMode,
    PolicyResult,
    RiskLevel,
    RunEvent,
    RunStatus,
    SessionEntry,
    ToolCall,
    ToolExecutionResult,
    new_id,
)
from .security import PolicyEngine, RISK_ORDER
from .storage import EventHub, JsonlSession, SessionStore
from .tools import READ_ONLY_TOOLS, ToolService, validate_tool_arguments
from .skills import SkillCatalog


class HookPaused(Exception):
    pass


class ApprovalBroker:
    def __init__(self) -> None:
        self._pending: dict[str, PendingApproval] = {}
        self._session_grants: dict[str, set[str]] = defaultdict(set)

    def is_granted(self, session_id: str, fingerprint: str) -> bool:
        return fingerprint in self._session_grants[session_id]

    def restore_session_grants(self, session: JsonlSession) -> None:
        for entry in session.entries:
            if entry.type != "approval_decision" or entry.payload.get("decision") != "allow_session":
                continue
            fingerprint = str(entry.payload.get("fingerprint", ""))
            if fingerprint:
                self._session_grants[session.header.id].add(fingerprint)

    async def request(
        self,
        approval_id: str,
        session_id: str,
        fingerprint: str,
        timeout_seconds: int,
    ) -> str:
        self.open(approval_id, session_id, fingerprint)
        return await self.wait(approval_id, timeout_seconds)

    def open(self, approval_id: str, session_id: str, fingerprint: str) -> None:
        if approval_id in self._pending:
            raise ValueError(f"Approval is already pending: {approval_id}")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[approval_id] = PendingApproval(
            session_id=session_id,
            fingerprint=fingerprint,
            future=future,
        )

    async def wait(self, approval_id: str, timeout_seconds: int) -> str:
        pending = self._pending.get(approval_id)
        if pending is None:
            raise KeyError(f"Approval is not pending: {approval_id}")
        try:
            return await asyncio.wait_for(asyncio.shield(pending.future), timeout=timeout_seconds)
        except TimeoutError:
            return "timeout"
        finally:
            self._pending.pop(approval_id, None)

    def discard(self, approval_id: str) -> None:
        pending = self._pending.pop(approval_id, None)
        if pending and not pending.future.done():
            pending.future.cancel()

    def decide(self, session_id: str, approval_id: str, decision: str, fingerprint: str) -> None:
        pending = self._pending.get(approval_id)
        if pending is None or pending.future.done():
            raise KeyError(f"Approval is not pending: {approval_id}")
        if pending.session_id != session_id or pending.fingerprint != fingerprint:
            raise PermissionError("Approval session or policy fingerprint does not match")
        if decision == "allow_session":
            self._session_grants[session_id].add(fingerprint)
        pending.future.set_result(decision)


@dataclass(slots=True)
class PendingApproval:
    session_id: str
    fingerprint: str
    future: asyncio.Future[str]


@dataclass(slots=True)
class ActiveRun:
    id: str
    session_id: str
    status: RunStatus
    task: asyncio.Task | None = None
    state: dict[str, Any] = field(default_factory=dict)


class AgentRunner:
    def __init__(
        self,
        *,
        sessions: SessionStore,
        events: EventHub,
        hooks: HookRegistry,
        adapter: ModelAdapter,
        projector: ContextProjector,
        compactor: CompactionService,
        tools: ToolService,
        policy: PolicyEngine,
        approvals: ApprovalBroker,
        max_rounds: int,
        max_run_tokens: int = 0,
        max_run_cost_usd: float = 0.0,
        input_price_per_million: float = 0.0,
        output_price_per_million: float = 0.0,
        approval_timeout: int,
        protected_paths: tuple[Path, ...] = (),
        skills: SkillCatalog | None = None,
        checkpoints: CheckpointStore | None = None,
        subagents: Any | None = None,
    ) -> None:
        if max_run_cost_usd > 0 and input_price_per_million <= 0 and output_price_per_million <= 0:
            raise ValueError("Cost budget requires TRACEFORGE_INPUT_USD_PER_1M or TRACEFORGE_OUTPUT_USD_PER_1M")
        self.sessions = sessions
        self.events = events
        self.hooks = hooks
        self.adapter = adapter
        self.projector = projector
        self.compactor = compactor
        self.tools = tools
        self.policy = policy
        self.approvals = approvals
        self.max_rounds = max_rounds
        self.max_run_tokens = max_run_tokens
        self.max_run_cost_usd = max_run_cost_usd
        self.input_price_per_million = input_price_per_million
        self.output_price_per_million = output_price_per_million
        self.approval_timeout = approval_timeout
        self.protected_paths = tuple(path.resolve() for path in protected_paths)
        self.skills = skills
        self.checkpoints = checkpoints
        self.subagents = subagents
        if subagents is not None:
            self.tools.subagents_enabled = True
        self.runs: dict[str, ActiveRun] = {}
        self._base_reserve_tokens = compactor.reserve_tokens
        self._base_keep_recent_tokens = compactor.keep_recent_tokens

    def configure_context_window(self, context_window: int) -> None:
        """Apply the currently configured model limit to both projection and compaction."""
        if context_window < 1024:
            raise ValueError("Model context window must be at least 1024 tokens")
        reserve = min(self._base_reserve_tokens, max(256, context_window // 4))
        recent = min(self._base_keep_recent_tokens, max(256, (context_window - reserve) // 2))
        self.projector.context_window = context_window
        self.projector.reserve_tokens = reserve
        self.compactor.context_window = context_window
        self.compactor.reserve_tokens = reserve
        self.compactor.keep_recent_tokens = recent

    def start(
        self,
        session_id: str,
        content: str,
        *,
        brief_text: str | None = None,
        resume_from_entry_id: str | None = None,
        request_source_id: str | None = None,
        queue_chain_root: str | None = None,
        queued_message_id: str | None = None,
    ) -> ActiveRun:
        workspace = Path(self.sessions.get(session_id).workspace).resolve()
        if any(path.is_relative_to(workspace) for path in self.protected_paths):
            raise RuntimeError("工作区包含 TraceForge 模型配置，请选择更具体的代码目录")
        if any(run.session_id == session_id and run.task and not run.task.done() for run in self.runs.values()):
            raise RuntimeError("This session already has an active run")
        skill_command = self.skills.parse_command(content) if self.skills and not resume_from_entry_id else None
        if skill_command:
            try:
                self.skills.get(workspace, skill_command[0])
            except KeyError as exc:
                raise ValueError(f"未找到 Skill：{skill_command[0]}") from exc
            brief_text = f"使用 {skill_command[0]} Skill 完成：{skill_command[1]}" if skill_command[1] else f"使用 {skill_command[0]} Skill"
        run = ActiveRun(
            id=new_id("run"),
            session_id=session_id,
            status=RunStatus.IDLE,
            state={
                "brief_user_text": brief_text or content,
                "resume_from_entry_id": resume_from_entry_id,
                "request_source_id": request_source_id,
                "queue_chain_root": queue_chain_root,
                "queued_message_id": queued_message_id,
                "workspace": str(workspace),
                "skill_command": skill_command,
                "started_monotonic": time.monotonic(),
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                          "cached_input_tokens": 0, "reasoning_tokens": 0},
                "tool_calls": 0,
                "tool_errors": 0,
            },
        )
        self.runs[run.id] = run

        async def persist_usage(record: dict[str, Any]) -> None:
            session = self.sessions.get(session_id)
            if self.input_price_per_million or self.output_price_per_million:
                record["estimated_cost_usd"] = round((
                    int(record.get("input_tokens") or 0) * self.input_price_per_million
                    + int(record.get("output_tokens") or 0) * self.output_price_per_million
                ) / 1_000_000, 8)
            await self._append(session, "model_usage", record, run.id)
            totals = run.state["usage"]
            for key in totals:
                totals[key] += int(record.get(key) or 0)
            if record.get("estimated_cost_usd") is not None:
                run.state["estimated_cost_usd"] = round(
                    float(run.state.get("estimated_cost_usd", 0)) + float(record["estimated_cost_usd"]), 8,
                )

        token = usage_sink.set(persist_usage)
        try:
            run.task = asyncio.create_task(self._run(run, content), name=run.id)
            run.state["queue_chain_root"] = queue_chain_root or run.id
            run.task.add_done_callback(lambda _: asyncio.create_task(self._continue_queued(run)))
        finally:
            usage_sink.reset(token)
        return run

    @staticmethod
    def pending_messages(session: JsonlSession) -> list[SessionEntry]:
        branch = session.get_branch()
        delivered = {
            str(entry.payload.get("queued_message_id")) for entry in branch
            if entry.type in {"user_message", "queued_message_cancel"}
            and entry.payload.get("queued_message_id")
        }
        return [entry for entry in branch if entry.type == "queued_message" and entry.id not in delivered]

    async def queue_message(self, session_id: str, run_id: str, content: str, timing: str) -> SessionEntry:
        if timing not in {"after_tool_batch", "after_run"} or not content.strip():
            raise ValueError("Invalid queued message")
        run = self.active_for_session(session_id)
        if not run or run.id != run_id or run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            raise RuntimeError("目标运行已结束，请直接发送消息")
        session = self.sessions.get(session_id)
        return await self._append(session, "queued_message", {
            "content": content.strip(), "timing": timing,
            "target_run_id": run_id, "queue_chain_root": run.state["queue_chain_root"],
        }, run_id)

    async def cancel_queued_message(self, session_id: str, message_id: str) -> None:
        session = self.sessions.get(session_id)
        message = next((entry for entry in self.pending_messages(session) if entry.id == message_id), None)
        if message is None:
            raise KeyError("Queued message is no longer pending")
        await self._append(session, "queued_message_cancel", {"queued_message_id": message_id}, message.run_id or "")

    async def _deliver_steering(self, run: ActiveRun, session: JsonlSession,
                                evidence: list[dict[str, Any]], brief_text: str) -> str | None:
        pending = [entry for entry in self.pending_messages(session)
                   if entry.payload.get("timing") == "after_tool_batch"
                   and entry.payload.get("target_run_id") == run.id]
        if not pending:
            return None
        combined: list[str] = []
        for entry in pending:
            content = str(entry.payload["content"])
            delivered = await self._append(session, "user_message", {
                "content": content, "queued_message_id": entry.id, "delivery": "after_tool_batch",
            }, run.id)
            run.state["latest_user_entry_id"] = delivered.id
            combined.append(content)
        text = brief_text + "\n后续补充指令：\n" + "\n".join(combined)
        await self._dispatch_hooks(run, session, HookPoint.USER_MESSAGE_COMMITTED, user_text=text, evidence=evidence)
        return text

    async def _continue_queued(self, finished: ActiveRun) -> None:
        if finished.status != RunStatus.COMPLETED or finished.state.get("terminal_outcome"):
            return
        session = self.sessions.get(finished.session_id)
        if session.archived or self.active_for_session(finished.session_id):
            return
        if any(run.task and not run.task.done()
               and self.sessions.get(run.session_id).header.workspace_id == session.header.workspace_id
               for run in self.runs.values()):
            return
        pending = [entry for entry in self.pending_messages(session)
                   if entry.payload.get("queue_chain_root") == finished.state["queue_chain_root"]]
        if not pending:
            return
        # A steering message that arrived after the last safe boundary becomes
        # the next run. Each remaining follow-up gets its own run in order.
        entry = pending[0]
        try:
            content = str(entry.payload["content"])
            self.start(session.header.id, content,
                       queue_chain_root=finished.state["queue_chain_root"],
                       queued_message_id=entry.id)
        except Exception as exc:  # preserve queued work for visible recovery
            await self._append(session, "hook_state", {
                "point": "queued_message", "error": f"{type(exc).__name__}: {exc}",
            }, finished.id)

    def active_for_session(self, session_id: str) -> ActiveRun | None:
        return next(
            (
                run for run in self.runs.values()
                if run.session_id == session_id and run.task and not run.task.done()
            ),
            None,
        )

    async def cancel(self, run_id: str) -> None:
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(f"Unknown run: {run_id}")
        if run.task and not run.task.done():
            run.task.cancel()
            try:
                await run.task
            except asyncio.CancelledError:
                pass

    async def _append(
        self,
        session: JsonlSession,
        entry_type: str,
        payload: dict[str, Any],
        run_id: str,
        *,
        parent_id: str | None = None,
    ) -> SessionEntry:
        entry = await session.append(entry_type, payload, run_id=run_id, parent_id=parent_id)
        await self.events.publish(
            RunEvent(
                session_id=session.header.id,
                run_id=run_id,
                seq=entry.seq,
                type=entry.type,
                timestamp=entry.timestamp,
                payload={"entry_id": entry.id, **entry.payload},
            )
        )
        return entry

    async def _set_status(self, run: ActiveRun, session: JsonlSession, status: RunStatus, **extra: Any) -> None:
        if self.checkpoints and status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            try:
                snapshot = self.checkpoints.capture(session)
                await self._append(session, "workspace_checkpoint", {**snapshot, "reason": "run_finished"}, run.id)
            except Exception as exc:  # checkpoint failure must not hide the run's final state
                extra["checkpoint_error"] = f"{type(exc).__name__}: {exc}"
        if status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            extra.update({
                "usage": dict(run.state.get("usage", {})),
                "elapsed_seconds": round(time.monotonic() - float(run.state.get("started_monotonic", time.monotonic())), 3),
                "tool_calls": int(run.state.get("tool_calls", 0)),
                "tool_errors": int(run.state.get("tool_errors", 0)),
                "estimated_cost_usd": run.state.get("estimated_cost_usd"),
            })
        run.status = status
        if status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            run.state["terminal_outcome"] = extra.get("outcome")
        await self._append(session, "run_state", {"status": status.value, **extra}, run.id)

    async def _request_clarification(
        self,
        run: ActiveRun,
        session: JsonlSession,
        source_message_id: str,
        brief_text: str,
        evidence: list[dict[str, Any]],
    ) -> None:
        brief = run.state.get("task_brief", {})
        gaps = brief.get("missing_points", [])
        selected_gap_ids = run.state.get("ask_gap_ids")
        if selected_gap_ids is not None:
            gaps = [gap for gap in gaps if gap.get("id") in selected_gap_ids]
        previous_questions = [entry for entry in session.get_branch() if entry.type == "clarification_question"]
        asked = 0
        for gap in gaps:
            description = str(gap.get("description", "")).strip()
            question = str(gap.get("question", "")).strip()
            if not question and description:
                question = f"请确认：{description.rstrip('。？?')}？"
            if not question:
                continue
            gap_id = str(gap.get("id") or "gap_unspecified")
            parent = next(
                (entry.payload.get("question_id") for entry in reversed(previous_questions)
                 if entry.payload.get("gap_id") == gap_id),
                None,
            )
            options = list(dict.fromkeys(
                value for item in gap.get("options", [])
                if (value := str(item).strip())
            ))[:4]
            await self._append(
                session,
                "clarification_question",
                {
                    "question_id": new_id("question"),
                    "parent_question_id": parent,
                    "question": question,
                    "options": options,
                    "gap_id": gap_id,
                    "source_message_id": source_message_id,
                    "impact": str(gap.get("impact", "")),
                    "status": "pending",
                },
                run.id,
            )
            asked += 1
        if not asked:
            await self._append(
                session,
                "clarification_question",
                {
                    "question_id": new_id("question"),
                    "parent_question_id": None,
                    "question": "仓库调查后仍缺少关键信息，请说明目标和期望结果。",
                    "options": [],
                    "gap_id": "gap_discovery_stalled",
                    "source_message_id": source_message_id,
                    "status": "pending",
                },
                run.id,
            )
        await self._set_status(run, session, RunStatus.COMPLETED, outcome="awaiting_clarification")
        await self._dispatch_hooks(
            run, session, HookPoint.RUN_FINISHED, user_text=brief_text, evidence=evidence
        )

    async def _dispatch_hooks(
        self,
        run: ActiveRun,
        session: JsonlSession,
        point: HookPoint,
        *,
        user_text: str,
        evidence: list[dict[str, Any]],
    ):
        result = await self.hooks.dispatch(
            HookContext(
                session_id=session.header.id,
                run_id=run.id,
                point=point,
                status=run.status,
                active_leaf_id=session.active_leaf_id,
                user_text=user_text,
                evidence=evidence,
                state=run.state,
            )
        )
        run.state.update(result.state_updates)
        for draft in result.entry_drafts:
            await self._append(session, draft.type, draft.payload, run.id)
        for error in result.errors:
            await self._append(session, "hook_state", {"point": point.value, "error": error}, run.id)
        return result

    async def _run(self, run: ActiveRun, content: str) -> None:
        session = self.sessions.get(run.session_id)
        evidence: list[dict[str, Any]] = []
        brief_text = str(run.state.get("brief_user_text", content))
        try:
            if self.checkpoints:
                baseline = self.checkpoints.capture(session)
                await self._append(session, "workspace_checkpoint", {**baseline, "reason": "before_run"}, run.id)
            await self._set_status(run, session, RunStatus.BRIEFING)
            resume_id = run.state.get("resume_from_entry_id")
            user_entry = session.by_id[resume_id] if resume_id else await self._append(
                session, "user_message", {
                    "content": content,
                    **({"queued_message_id": run.state["queued_message_id"], "delivery": "after_run"}
                       if run.state.get("queued_message_id") else {}),
                }, run.id
            )
            skill_command = run.state.get("skill_command")
            if skill_command and self.skills:
                skill, skill_content = self.skills.read(session.workspace, skill_command[0])
                await self._append(session, "skill_activation", {
                    "name": skill.name,
                    "path": str(skill.path),
                    "source": skill.source,
                    "sha256": self.skills.fingerprint(skill_content),
                    "content": skill_content,
                    "arguments": skill_command[1],
                }, run.id)
            source_message_id = str(run.state.get("request_source_id") or user_entry.id)
            await self._dispatch_hooks(
                run,
                session,
                HookPoint.USER_MESSAGE_COMMITTED,
                user_text=brief_text,
                evidence=evidence,
            )

            for round_index in range(self.max_rounds):
                token_limit = self.max_run_tokens and run.state["usage"]["total_tokens"] >= self.max_run_tokens
                cost_limit = self.max_run_cost_usd and float(run.state.get("estimated_cost_usd", 0)) >= self.max_run_cost_usd
                if token_limit or cost_limit:
                    limit_text = (f"{self.max_run_tokens} token" if token_limit
                                  else f"${self.max_run_cost_usd:.4f} 估算费用")
                    gap_id = "agent_token_budget" if token_limit else "agent_cost_budget"
                    await self._append(session, "clarification_question", {
                        "question_id": new_id("question"), "parent_question_id": None,
                        "question": f"本轮已达到 {limit_text} 预算。是否继续执行？",
                        "gap_id": gap_id, "gap_ids": [gap_id],
                        "source_message_id": user_entry.id, "status": "pending",
                    }, run.id)
                    await self._set_status(run, session, RunStatus.COMPLETED,
                                           outcome="token_budget" if token_limit else "cost_budget")
                    return
                hook_result = await self._dispatch_hooks(
                    run,
                    session,
                    HookPoint.BEFORE_MODEL_REQUEST,
                    user_text=brief_text,
                    evidence=evidence,
                )
                if hook_result.action == "abort":
                    raise RuntimeError("A before_model_request hook aborted the run")
                if hook_result.action == "pause":
                    raise HookPaused("before_model_request")
                phase = RunStatus(run.state.get("brief_phase", RunStatus.EXECUTING.value))
                if phase == RunStatus.BRIEFING and hook_result.errors:
                    # TaskBrief is fail-open: the main agent receives the full
                    # request and decides whether it needs to ask a question.
                    phase = RunStatus.EXECUTING
                await self._set_status(run, session, phase, round=round_index + 1)

                if phase == RunStatus.CLARIFYING:
                    await self._request_clarification(
                        run, session, source_message_id, brief_text, evidence
                    )
                    return

                sections = dict(hook_result.context_sections)
                if self.skills:
                    inventory = self.skills.inventory(session.workspace)
                    if inventory:
                        sections["available_skills"] = inventory
                projected = self.projector.project(session, sections)
                if self.compactor.should_compact(projected):
                    projected, _ = await self._compact_context(
                        run,
                        session,
                        brief_text,
                        evidence,
                        sections,
                    )

                allowed_tools = hook_result.allowed_tools
                model_tools = (self.tools.definitions(allowed_tools, session.permission_mode)
                               if session.permission_mode == PermissionMode.FULL_ACCESS
                               else self.tools.definitions(allowed_tools))
                if self.skills:
                    discovered = self.skills.discover(session.workspace)[0]
                    has_visible_skill = any(not skill.disable_model_invocation for skill in discovered)
                    has_activated_skill = any(
                        entry.type == "skill_activation" for entry in session.get_branch()
                    )
                    if not has_visible_skill and not has_activated_skill:
                        model_tools = [tool for tool in model_tools if tool["name"] != "read_skill"]
                offered_tools = {str(tool["name"]) for tool in model_tools}
                text_parts: list[str] = []
                tool_calls: list[ToolCall] = []
                reasoning_items: list[dict[str, Any]] = []
                for model_attempt in range(2):
                    try:
                        await self.events.publish(
                            RunEvent(
                                session_id=session.header.id,
                                run_id=run.id,
                                seq=session._next_seq,
                                type="reasoning_stream_reset",
                            )
                        )
                        async for delta in self.adapter.stream_turn(
                            projected.instructions,
                            projected.input_items,
                            model_tools,
                        ):
                            if delta.type == "text_delta":
                                text_parts.append(delta.text)
                                await self.events.publish(
                                    RunEvent(
                                        session_id=session.header.id,
                                        run_id=run.id,
                                        seq=session._next_seq,
                                        type="assistant_delta",
                                        payload={"delta": delta.text},
                                    )
                                )
                            elif delta.type == "reasoning_delta":
                                await self.events.publish(
                                    RunEvent(
                                        session_id=session.header.id,
                                        run_id=run.id,
                                        seq=session._next_seq,
                                        type="reasoning_delta",
                                        payload={"delta": delta.text},
                                    )
                                )
                            elif delta.type == "tool_call" and delta.tool_call:
                                tool_calls.append(delta.tool_call)
                            elif delta.type == "reasoning_item" and delta.raw.get("type") == "reasoning":
                                reasoning_items.append(delta.raw)
                            elif delta.type == "model_fallback":
                                await self._append(session, "model_change", {
                                    **delta.raw, "reason": "primary_request_failed_before_stream",
                                }, run.id)
                            elif delta.type == "error":
                                raise RuntimeError(delta.text)
                        break
                    except Exception as exc:
                        if model_attempt > 0 or not self._is_context_overflow(exc):
                            raise
                        projected, compacted = await self._compact_context(
                            run,
                            session,
                            brief_text,
                            evidence,
                            sections,
                        )
                        if not compacted:
                            raise
                        text_parts.clear()
                        tool_calls.clear()
                        reasoning_items.clear()
                        await self.events.publish(
                            RunEvent(
                                session_id=session.header.id,
                                run_id=run.id,
                                seq=session._next_seq,
                                type="assistant_stream_reset",
                                payload={"reason": "context_overflow_retry"},
                            )
                        )

                text = "".join(text_parts)
                if not (phase == RunStatus.DISCOVERING and not tool_calls):
                    for item in reasoning_items:
                        await self._append(session, "model_reasoning", {"item": item}, run.id)
                if text and not (phase == RunStatus.DISCOVERING and not tool_calls):
                    await self._append(session, "assistant_message", {"content": text}, run.id)
                response_hook = await self._dispatch_hooks(
                    run,
                    session,
                    HookPoint.AFTER_MODEL_RESPONSE,
                    user_text=brief_text,
                    evidence=evidence,
                )
                if response_hook.action == "abort":
                    raise RuntimeError("An after_model_response hook aborted the run")
                if response_hook.action == "pause":
                    raise HookPaused("after_model_response")

                if not tool_calls:
                    can_continue = (
                        round_index + 1 < self.max_rounds
                        and (not self.max_run_tokens or run.state["usage"]["total_tokens"] < self.max_run_tokens)
                        and (not self.max_run_cost_usd or float(run.state.get("estimated_cost_usd", 0)) < self.max_run_cost_usd)
                    )
                    steering = (await self._deliver_steering(run, session, evidence, brief_text)
                                if can_continue else None)
                    if steering:
                        brief_text = steering
                        source_message_id = str(run.state["latest_user_entry_id"])
                        continue
                    if phase == RunStatus.DISCOVERING:
                        if text:
                            await self.events.publish(
                                RunEvent(
                                    session_id=session.header.id,
                                    run_id=run.id,
                                    seq=session._next_seq,
                                    type="assistant_stream_reset",
                                    payload={"reason": "discovery_needs_clarification"},
                                )
                            )
                        await self._request_clarification(
                            run, session, source_message_id, brief_text, evidence
                        )
                        return
                    await self._set_status(run, session, RunStatus.COMPLETED)
                    await self._dispatch_hooks(
                        run,
                        session,
                        HookPoint.RUN_FINISHED,
                        user_text=brief_text,
                        evidence=evidence,
                    )
                    return

                batch_evidence: list[dict[str, Any]] = []
                # A model response may contain several calls. Preserve that
                # response as one batch in JSONL before recording any outputs;
                # reasoning-mode APIs require this order on the next request.
                call_entries = [
                    await self._append(
                        session,
                        "tool_call",
                        {"call_id": call.call_id, "name": call.name, "arguments": call.arguments},
                        run.id,
                    )
                    for call in tool_calls
                ]
                call_index = 0
                while call_index < len(tool_calls):
                    if tool_calls[call_index].name in READ_ONLY_TOOLS:
                        batch_end = call_index
                        while batch_end < len(tool_calls) and tool_calls[batch_end].name in READ_ONLY_TOOLS:
                            batch_end += 1
                        group = tool_calls[call_index:batch_end]
                    else:
                        batch_end = call_index + 1
                        group = [tool_calls[call_index]]

                    group_entries = call_entries[call_index:batch_end]
                    if len(group) > 1:
                        results = await asyncio.gather(
                            *(self._execute_tool(run, session, call, brief_text, evidence, offered_tools) for call in group)
                        )
                    else:
                        results = [
                            await self._execute_tool(run, session, group[0], brief_text, evidence, offered_tools)
                        ]

                    # Persist in model output order even when the read-only work
                    # ran concurrently, keeping replay deterministic.
                    for call, call_entry, result in zip(group, group_entries, results, strict=True):
                        run.state["tool_calls"] = int(run.state.get("tool_calls", 0)) + 1
                        run.state["tool_errors"] = int(run.state.get("tool_errors", 0)) + int(result.is_error)
                        tool_entry = await self._append(
                            session,
                            "tool_result",
                            {
                                "call_id": call.call_id,
                                "tool_name": call.name,
                                "output": result.output,
                                "is_error": result.is_error,
                                "exit_code": result.exit_code,
                                "artifact_id": result.artifact_id,
                                "metadata": result.metadata,
                                "call_entry_id": call_entry.id,
                            },
                            run.id,
                        )
                        if call.name in READ_ONLY_TOOLS:
                            batch_evidence.append(
                                {
                                    "tool": call.name,
                                    "arguments": call.arguments,
                                    "result": result.output[:16000],
                                    "entry_id": tool_entry.id,
                                }
                            )
                        tool_hook = await self._dispatch_hooks(
                            run,
                            session,
                            HookPoint.AFTER_TOOL_RESULT,
                            user_text=brief_text,
                            evidence=batch_evidence,
                        )
                        if tool_hook.action == "abort":
                            raise RuntimeError("An after_tool_result hook aborted the run")
                        if tool_hook.action == "pause":
                            raise HookPaused("after_tool_result")
                    call_index = batch_end
                evidence.extend(batch_evidence)
                batch_hook = await self._dispatch_hooks(
                    run,
                    session,
                    HookPoint.AFTER_TOOL_BATCH,
                    user_text=brief_text,
                    evidence=batch_evidence,
                )
                if batch_hook.action == "abort":
                    raise RuntimeError("An after_tool_batch hook aborted the run")
                if batch_hook.action == "pause":
                    raise HookPaused("after_tool_batch")
                next_result = await self._dispatch_hooks(
                    run,
                    session,
                    HookPoint.BEFORE_NEXT_MODEL_REQUEST,
                    user_text=brief_text,
                    evidence=batch_evidence,
                )
                if next_result.action == "abort":
                    raise RuntimeError("A before_next_model_request hook aborted the run")
                if next_result.action == "pause":
                    raise HookPaused("before_next_model_request")
                has_next_round = round_index + 1 < self.max_rounds
                has_token_budget = not self.max_run_tokens or run.state["usage"]["total_tokens"] < self.max_run_tokens
                has_cost_budget = not self.max_run_cost_usd or float(run.state.get("estimated_cost_usd", 0)) < self.max_run_cost_usd
                if has_next_round and has_token_budget and has_cost_budget:
                    steering = await self._deliver_steering(run, session, evidence, brief_text)
                    if steering:
                        brief_text = steering
                        source_message_id = str(run.state["latest_user_entry_id"])
                    # The entire tool batch is persisted here. Compact before
                    # the next model request when this run approaches its
                    # context limit, without splitting a call from its result.
                    after_batch = self.projector.project(session, sections)
                    if self.compactor.should_compact(after_batch):
                        await self._compact_context(
                            run, session, brief_text, evidence, sections,
                        )

            await self._append(
                session,
                "clarification_question",
                {
                    "question_id": new_id("question"),
                    "parent_question_id": None,
                    "question": f"Agent 已达到 {self.max_rounds} 个工具轮次。是否继续执行？",
                    "gap_id": "agent_round_limit",
                    "gap_ids": ["agent_round_limit"],
                    "source_message_id": user_entry.id,
                    "status": "pending",
                },
                run.id,
            )
            await self._set_status(run, session, RunStatus.COMPLETED, outcome="round_limit")
            await self._dispatch_hooks(
                run,
                session,
                HookPoint.RUN_FINISHED,
                user_text=brief_text,
                evidence=evidence,
            )
            return
        except HookPaused as exc:
            await self._recover_interrupted(run, session, "hook_paused")
            await self._set_status(run, session, RunStatus.COMPLETED, outcome="hook_paused", point=str(exc))
            await self._dispatch_hooks(
                run,
                session,
                HookPoint.RUN_FINISHED,
                user_text=brief_text,
                evidence=evidence,
            )
        except asyncio.CancelledError:
            await self._recover_interrupted(run, session, "run_cancelled")
            await self._set_status(run, session, RunStatus.CANCELLED)
            raise
        except Exception as exc:  # noqa: BLE001
            await self._recover_interrupted(run, session, "run_failed")
            await self._set_status(run, session, RunStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
            await self._dispatch_hooks(
                run,
                session,
                HookPoint.RUN_FAILED,
                user_text=brief_text,
                evidence=evidence,
            )

    async def _recover_interrupted(self, run: ActiveRun, session: JsonlSession, reason: str) -> None:
        recovered = [
            *await session.recover_unmatched_tool_calls(),
            *await session.recover_pending_approvals(reason),
        ]
        for entry in recovered:
            await self.events.publish(
                RunEvent(
                    session_id=session.header.id,
                    run_id=entry.run_id or run.id,
                    seq=entry.seq,
                    type=entry.type,
                    timestamp=entry.timestamp,
                    payload={"entry_id": entry.id, **entry.payload},
                )
            )

    async def _compact_context(
        self,
        run: ActiveRun,
        session: JsonlSession,
        user_text: str,
        evidence: list[dict[str, Any]],
        sections: dict[str, str],
    ):
        before = await self._dispatch_hooks(
            run,
            session,
            HookPoint.BEFORE_COMPACTION,
            user_text=user_text,
            evidence=evidence,
        )
        if before.action == "abort":
            raise RuntimeError("A before_compaction hook aborted the run")
        if before.action == "pause":
            raise HookPaused("before_compaction")
        compacted = await self.compactor.compact(session, run.id)
        if compacted:
            await self.events.publish(
                RunEvent(
                    session_id=session.header.id,
                    run_id=run.id,
                    seq=compacted.seq,
                    type=compacted.type,
                    timestamp=compacted.timestamp,
                    payload={"entry_id": compacted.id, **compacted.payload},
                )
            )
        after = await self._dispatch_hooks(
            run,
            session,
            HookPoint.AFTER_COMPACTION,
            user_text=user_text,
            evidence=evidence,
        )
        if after.action == "abort":
            raise RuntimeError("An after_compaction hook aborted the run")
        if after.action == "pause":
            raise HookPaused("after_compaction")
        return self.projector.project(session, sections), compacted is not None

    @staticmethod
    def _is_context_overflow(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(
            marker in message
            for marker in ("context_length", "context window", "maximum context", "too many tokens")
        )

    async def _execute_tool(
        self,
        run: ActiveRun,
        session: JsonlSession,
        call: ToolCall,
        user_text: str,
        evidence: list[dict[str, Any]],
        offered_tools: set[str] | None = None,
    ):
        hook_result = await self._dispatch_hooks(
            run,
            session,
            HookPoint.BEFORE_TOOL_CALL,
            user_text=user_text,
            evidence=evidence,
        )
        if hook_result.action in {"pause", "abort"}:
            return self._denied_result(
                PolicyResult(
                    decision=RiskLevel.DENY,
                    capabilities=["hook_control"],
                    reasons=[f"Hook requested {hook_result.action} before tool execution"],
                ),
                "Hook blocked this operation",
            )
        if (offered_tools is not None and call.name not in offered_tools) or (
            hook_result.allowed_tools is not None and call.name not in hook_result.allowed_tools
        ):
            return self._denied_result(
                PolicyResult(
                    decision=RiskLevel.DENY,
                    capabilities=["tool_not_available"],
                    reasons=[f"Tool is not available in this phase: {call.name}"],
                ),
                "Tool not available",
            )
        definition = next((tool for tool in self.tools.definitions() if tool["name"] == call.name), None)
        if definition is None:
            return ToolExecutionResult(output=f"Unknown tool: {call.name}", is_error=True)
        try:
            call = call.model_copy(update={"arguments": validate_tool_arguments(definition, call.arguments)})
        except ValueError as exc:
            return ToolExecutionResult(output=str(exc), is_error=True)
        if call.name == "read_skill" and self.skills:
            try:
                skill = self.skills.get(session.workspace, str(call.arguments.get("name", "")))
            except KeyError:
                skill = None
            if skill and skill.disable_model_invocation and not any(
                entry.type == "skill_activation" and entry.payload.get("name") == skill.name
                for entry in session.get_branch()
            ):
                return self._denied_result(
                    PolicyResult(
                        decision=RiskLevel.DENY,
                        capabilities=["explicit_skill_only"],
                        reasons=[f"Skill requires explicit /skill:{skill.name} invocation"],
                    ),
                    "Skill not available automatically",
                )
        permission_mode = session.permission_mode
        try:
            policy = self.policy.evaluate(call.name, call.arguments, session.workspace, permission_mode)
        except (ValueError, KeyError, TypeError) as exc:
            return ToolExecutionResult(output=f"Invalid tool arguments: {exc}", is_error=True)
        registered_tools = {str(tool["name"]) for tool in self.tools.definitions()}
        if call.name not in registered_tools:
            return self._denied_result(policy, "Unregistered tool")
        if permission_mode == PermissionMode.FULL_ACCESS:
            policy.decision = RiskLevel.ALLOW
            policy.network = True
            policy.capabilities = sorted(set(policy.capabilities) | {"host_full_access", "network_access"})
            policy.reasons = ["Full access mode permits host execution as the current user"]
            for key in ("path", "cwd", "source_path", "destination_path"):
                raw_path = call.arguments.get(key)
                if not isinstance(raw_path, str) or not raw_path or (call.name == "read_skill" and key == "path"):
                    continue
                resolved = str((Path(session.workspace) / raw_path).resolve())
                if resolved not in policy.affected_paths:
                    policy.affected_paths.append(resolved)
        if hook_result.risk_floor and RISK_ORDER[hook_result.risk_floor] > RISK_ORDER[policy.decision]:
            policy.decision = hook_result.risk_floor
            policy.reasons.append("A registered hook raised the minimum risk level")
        if policy.decision == RiskLevel.DENY:
            return self._denied_result(policy, "Policy denied this operation")
        must_ask_each_time = permission_mode == PermissionMode.REQUEST_APPROVAL and bool(
            {"external_file_write", "network_access"} & set(policy.capabilities)
        )
        already_granted = not must_ask_each_time and self.approvals.is_granted(
            session.header.id, policy.fingerprint,
        )
        if policy.decision == RiskLevel.ASK and permission_mode == PermissionMode.FULL_ACCESS:
            await self._append(session, "approval_decision", {
                "approval_id": new_id("approval"), "decision": "allow_once",
                "fingerprint": policy.fingerprint, "automatic": True,
                "permission_mode": permission_mode.value, "call_id": call.call_id,
                "tool_name": call.name, "policy": policy.model_dump(mode="json"),
            }, run.id)
        elif policy.decision == RiskLevel.ASK and not already_granted:
            approval_id = new_id("approval")
            self.approvals.open(approval_id, session.header.id, policy.fingerprint)
            try:
                await self._append(
                    session,
                    "approval_request",
                    {
                        "approval_id": approval_id,
                        "call_id": call.call_id,
                        "tool_name": call.name,
                        "arguments": call.arguments,
                        "policy": policy.model_dump(mode="json"),
                        "status": "pending",
                    },
                    run.id,
                )
                await self._set_status(run, session, RunStatus.WAITING_APPROVAL, approval_id=approval_id)
                decision = await self.approvals.wait(approval_id, self.approval_timeout)
            finally:
                self.approvals.discard(approval_id)
            await self._append(
                session,
                "approval_decision",
                {
                    "approval_id": approval_id,
                    "decision": decision,
                    "fingerprint": policy.fingerprint,
                },
                run.id,
            )
            if decision in {"deny", "timeout"}:
                message = "Approval timed out" if decision == "timeout" else "User denied this operation"
                return self._denied_result(policy, message)
            await self._set_status(run, session, RunStatus.EXECUTING)

        if call.name == "delegate_task":
            if self.subagents is None:
                return ToolExecutionResult(output="Subagents are unavailable", is_error=True)

            def budget_available() -> bool:
                return ((not self.max_run_tokens or run.state["usage"]["total_tokens"] < self.max_run_tokens)
                        and (not self.max_run_cost_usd
                             or float(run.state.get("estimated_cost_usd", 0)) < self.max_run_cost_usd))

            return await self.subagents.delegate(
                session, run.id, call.call_id, call.arguments,
                context_window=self.projector.context_window, budget_check=budget_available,
            )

        mutating = call.name in {"apply_patch", "create_file", "delete_file", "move_file", "run_command"} or bool(
            getattr(self.tools, "plugins", None) and self.tools.plugins.get(call.name)
        )
        external_file_write = "external_file_write" in policy.capabilities
        if mutating:
            capabilities = await (self.tools.host.inspect() if permission_mode == PermissionMode.FULL_ACCESS or external_file_write
                                  else self.tools.sandbox.inspect())
            await self._append(
                session,
                "sandbox_start",
                {
                    "call_id": call.call_id,
                    "backend": capabilities.backend,
                    "ready": capabilities.ready,
                    "reason": capabilities.reason,
                    "capabilities": capabilities.model_dump(mode="json"),
                    "policy_version": self.policy.version,
                    "network": policy.network,
                    "workspace": session.workspace,
                    "credential_policy": ("host-user-access" if permission_mode == PermissionMode.FULL_ACCESS
                                          else "approved-file-only" if external_file_write else "not-forwarded"),
                    "external_file_scope": policy.affected_paths if external_file_write else [],
                    "permission_mode": permission_mode.value,
                },
                run.id,
            )
        try:
            effective_arguments = dict(call.arguments)
            if call.name == "run_command" and policy.network:
                effective_arguments["network"] = True
            if permission_mode == PermissionMode.FULL_ACCESS:
                result = await self.tools.execute(
                    session.header.id, session.workspace, call.name, effective_arguments,
                    permission_mode=permission_mode,
                )
            elif external_file_write:
                result = await self.tools.execute(
                    session.header.id, session.workspace, call.name, effective_arguments,
                    allowed_external_paths=policy.affected_paths,
                )
            else:
                result = await self.tools.execute(
                    session.header.id, session.workspace, call.name, effective_arguments,
                )
        except asyncio.CancelledError:
            if mutating:
                await self._append(
                    session,
                    "sandbox_end",
                    {
                        "call_id": call.call_id,
                        "is_error": True,
                        "cancelled": True,
                        "status": "cancelled",
                    },
                    run.id,
                )
            raise
        if mutating:
            await self._append(
                session,
                "sandbox_end",
                {
                    "call_id": call.call_id,
                    "is_error": result.is_error,
                    "exit_code": result.exit_code,
                    "artifact_id": result.artifact_id,
                    "sandbox": result.metadata.get("sandbox", {}),
                },
                run.id,
            )
        return result

    @staticmethod
    def _denied_result(policy: PolicyResult, message: str):
        from .models import ToolExecutionResult

        return ToolExecutionResult(
            output=f"{message}: {'; '.join(policy.reasons)}",
            is_error=True,
            metadata={"policy": policy.model_dump(mode="json")},
        )
