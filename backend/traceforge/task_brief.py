from __future__ import annotations

import hashlib
import json
import re

from .hooks import AgentHook
from .model_adapter import ModelAdapter
from .models import EntryDraft, HookContext, HookOutcome, HookPoint, RunStatus, TaskBrief


READ_ONLY_TOOLS = {"list_files", "search_code", "read_file", "git_status", "git_diff", "read_skill",
                   "search_history", "read_history_entry", "read_project_handoff", "delegate_task"}
GREETING_ONLY = re.compile(r"^(?:你好|您好|嗨|哈喽|hello|hi|hey)[!！。,.，\s]*$", re.I)
SMALLTALK_ONLY = re.compile(r"^(?:在吗|在不在|谢谢|多谢|辛苦了)[?？!！。,.，\s]*$")
CAPABILITY_ONLY = re.compile(
    r"^(?:你|您)?(?:能|可以|能够)(?:帮我)?(?:查看|阅读|浏览|看看)(?:一下)?"
    r"(?:这个|当前|本)?(?:项目|仓库)(?:的)?(?:代码|文件)?(?:吗|么)[?？。！!\s]*$"
)


class TaskBriefHook(AgentHook):
    name = "task_brief"
    points = {
        HookPoint.USER_MESSAGE_COMMITTED,
        HookPoint.BEFORE_MODEL_REQUEST,
        HookPoint.AFTER_TOOL_BATCH,
    }
    priority = 100
    timeout_seconds = 45.0
    failure_mode = "open"

    def __init__(self, adapter: ModelAdapter) -> None:
        self.adapter = adapter

    async def handle(self, context: HookContext) -> HookOutcome:
        if context.point == HookPoint.USER_MESSAGE_COMMITTED:
            if (
                GREETING_ONLY.fullmatch(context.user_text.strip())
                or SMALLTALK_ONLY.fullmatch(context.user_text.strip())
                or CAPABILITY_ONLY.fullmatch(context.user_text.strip())
            ):
                brief = TaskBrief(needs_clarification=False, reason="普通对话或能力询问，不需要需求澄清")
                return HookOutcome(
                    state_updates={
                        "brief_dirty": False,
                        "brief_phase": RunStatus.EXECUTING.value,
                        "conversation_only": True,
                        "ask_gap_ids": [],
                        "task_brief": brief.model_dump(mode="json"),
                    },
                    entry_drafts=[EntryDraft(type="task_brief", payload={"brief": brief.model_dump(mode="json"), "phase": RunStatus.EXECUTING.value})],
                )
            return HookOutcome(
                state_updates={
                    "brief_dirty": True,
                    "brief_phase": RunStatus.BRIEFING.value,
                    "discovery_rounds": 0,
                    "brief_hash": "",
                    "conversation_only": False,
                    "ask_gap_ids": [],
                }
            )

        if context.point == HookPoint.AFTER_TOOL_BATCH:
            if context.state.get("brief_phase") != RunStatus.DISCOVERING.value or not context.evidence:
                return HookOutcome()
            return HookOutcome(state_updates={"brief_dirty": True}, invalidate_projection=True)

        if context.state.get("conversation_only"):
            return HookOutcome(allowed_tools=set())

        if context.point != HookPoint.BEFORE_MODEL_REQUEST or not context.state.get("brief_dirty", False):
            brief = context.state.get("task_brief")
            return self._outcome_for_existing(brief, context.state) if brief else HookOutcome()

        fingerprint = hashlib.sha256(
            json.dumps(
                {"user": context.user_text, "evidence": context.evidence},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if fingerprint == context.state.get("brief_hash"):
            return HookOutcome(state_updates={"brief_dirty": False})

        previous_raw = context.state.get("task_brief")
        previous = TaskBrief.model_validate(previous_raw) if previous_raw else None
        workspace = context.state.get("workspace")
        request_with_workspace = (
            f"已选工作区：{workspace}\n用户请求：{context.user_text}" if workspace else context.user_text
        )
        if context.state.get("project_handoff_available"):
            request_with_workspace += (
                "\n项目有可用的 .traceforge/handoff.md 交接文件，但文件内容尚未读取。"
                "如果用户要求继续既往工作，应先用只读工具 read_project_handoff 调查，"
                "不要仅因缺少上轮进度就向用户提问。"
            )
        brief = await self.adapter.analyze_task_brief(request_with_workspace, context.evidence, previous)
        if brief.needs_clarification and not brief.missing_points:
            brief = brief.model_copy(update={"needs_clarification": False})
        state_updates: dict = {
            "task_brief": brief.model_dump(mode="json"),
            "brief_dirty": False,
            "brief_hash": fingerprint,
        }
        user_blocker = next(
            (point for point in brief.missing_points if not point.discoverable_from_repo), None
        )
        rounds = int(context.state.get("discovery_rounds", 0))
        if brief.needs_clarification and user_blocker is not None:
            phase = RunStatus.CLARIFYING
            state_updates.update({"brief_phase": phase.value, "ask_gap_ids": [user_blocker.id]})
            allowed_tools: set[str] | None = set()
        elif brief.needs_clarification and rounds < 3:
            phase = RunStatus.DISCOVERING
            state_updates.update({"brief_phase": phase.value, "discovery_rounds": rounds + 1, "ask_gap_ids": []})
            allowed_tools = READ_ONLY_TOOLS
        elif brief.needs_clarification:
            phase = RunStatus.CLARIFYING
            state_updates.update({"brief_phase": phase.value, "ask_gap_ids": [brief.missing_points[0].id]})
            allowed_tools = set()
        else:
            phase = RunStatus.EXECUTING
            state_updates.update({"brief_phase": phase.value, "ask_gap_ids": []})
            allowed_tools = None

        section = self._render(brief, phase)
        return HookOutcome(
            context_sections={"task_brief": section},
            entry_drafts=[EntryDraft(type="task_brief", payload={"brief": brief.model_dump(mode="json"), "phase": phase})],
            allowed_tools=allowed_tools,
            state_updates=state_updates,
            invalidate_projection=True,
        )

    def _outcome_for_existing(self, raw: dict, state: dict) -> HookOutcome:
        brief = TaskBrief.model_validate(raw)
        phase = RunStatus(state.get("brief_phase", RunStatus.EXECUTING.value))
        allowed = READ_ONLY_TOOLS if phase == RunStatus.DISCOVERING else set() if phase == RunStatus.CLARIFYING else None
        return HookOutcome(context_sections={"task_brief": self._render(brief, phase)}, allowed_tools=allowed)

    @staticmethod
    def _render(brief: TaskBrief, phase: RunStatus) -> str:
        gaps = "\n".join(
            f"- {point.description}（影响：{point.impact or '未说明'}；可从仓库发现：{'是' if point.discoverable_from_repo else '否'}）"
            for point in brief.missing_points
        ) or "- 无"
        if phase == RunStatus.DISCOVERING:
            directive = "先使用只读工具查明可发现的缺口，不要修改文件。"
        elif phase == RunStatus.CLARIFYING:
            directive = "等待用户回答结构化澄清问题；不要调用工具，也不要开始实现。"
        else:
            directive = "需求信息足够，可以继续执行。"
        return f"需求澄清门控：{brief.reason or '无阻塞原因'}\n缺口：\n{gaps}\n行动：{directive}"
