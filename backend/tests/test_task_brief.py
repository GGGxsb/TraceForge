import pytest

from traceforge.models import HookContext, HookPoint, MissingPoint, RunStatus, TaskBrief
from traceforge.task_brief import READ_ONLY_TOOLS, TaskBriefHook

from .fakes import FakeModelAdapter


@pytest.mark.asyncio
async def test_task_brief_uses_read_only_discovery_then_clarifies():
    adapter = FakeModelAdapter(
        briefs=[
            TaskBrief(
                needs_clarification=True,
                reason="repo fact missing",
                missing_points=[MissingPoint(description="framework", discoverable_from_repo=True)],
            ),
            TaskBrief(
                needs_clarification=True,
                reason="user choice missing",
                missing_points=[MissingPoint(description="deployment target", discoverable_from_repo=False)],
            ),
        ]
    )
    hook = TaskBriefHook(adapter)
    state = {"brief_dirty": True, "brief_phase": "briefing", "discovery_rounds": 0}
    first = await hook.handle(
        HookContext(
            session_id="s",
            run_id="r",
            point=HookPoint.BEFORE_MODEL_REQUEST,
            status=RunStatus.BRIEFING,
            user_text="build it",
            state=state,
        )
    )
    assert first.allowed_tools == READ_ONLY_TOOLS
    state.update(first.state_updates)
    dirty = await hook.handle(
        HookContext(
            session_id="s",
            run_id="r",
            point=HookPoint.AFTER_TOOL_BATCH,
            status=RunStatus.DISCOVERING,
            evidence=[{"tool": "read_file", "result": "evidence"}],
            state=state,
        )
    )
    state.update(dirty.state_updates)
    second = await hook.handle(
        HookContext(
            session_id="s",
            run_id="r",
            point=HookPoint.BEFORE_MODEL_REQUEST,
            status=RunStatus.DISCOVERING,
            user_text="build it",
            evidence=[{"tool": "read_file", "result": "evidence"}],
            state=state,
        )
    )
    assert second.allowed_tools == set()
    assert second.state_updates["brief_phase"] == "clarifying"


@pytest.mark.asyncio
async def test_task_brief_stops_discovery_after_three_rounds():
    brief = TaskBrief(
        needs_clarification=True,
        reason="still unknown",
        missing_points=[MissingPoint(description="repo fact", discoverable_from_repo=True)],
    )
    hook = TaskBriefHook(FakeModelAdapter(briefs=[brief]))
    outcome = await hook.handle(
        HookContext(
            session_id="s",
            run_id="r",
            point=HookPoint.BEFORE_MODEL_REQUEST,
            status=RunStatus.DISCOVERING,
            user_text="build it",
            evidence=[{"round": 3}],
            state={"brief_dirty": True, "brief_phase": "discovering", "discovery_rounds": 3},
        )
    )
    assert outcome.allowed_tools == set()
    assert outcome.state_updates["brief_phase"] == "clarifying"


@pytest.mark.asyncio
async def test_user_only_blocker_takes_priority_over_secondary_repo_fact():
    brief = TaskBrief(
        needs_clarification=True,
        reason="company policy is missing",
        missing_points=[
            MissingPoint(id="repo", description="existing enforcement points", discoverable_from_repo=True),
            MissingPoint(id="user", description="company permission matrix", discoverable_from_repo=False),
        ],
    )
    hook = TaskBriefHook(FakeModelAdapter(briefs=[brief]))
    outcome = await hook.handle(
        HookContext(
            session_id="s",
            run_id="r",
            point=HookPoint.BEFORE_MODEL_REQUEST,
            status=RunStatus.BRIEFING,
            user_text="按公司的权限矩阵修改访问控制",
            state={"brief_dirty": True, "discovery_rounds": 0},
        )
    )
    assert outcome.state_updates["brief_phase"] == "clarifying"
    assert outcome.state_updates["ask_gap_ids"] == ["user"]
    assert outcome.allowed_tools == set()
    assert len(outcome.state_updates["task_brief"]["missing_points"]) == 2


@pytest.mark.asyncio
async def test_pure_capability_question_skips_brief_but_action_request_does_not():
    adapter = FakeModelAdapter()
    hook = TaskBriefHook(adapter)
    capability = await hook.handle(
        HookContext(
            session_id="s",
            run_id="r",
            point=HookPoint.USER_MESSAGE_COMMITTED,
            status=RunStatus.BRIEFING,
            user_text="你能帮我查看这个项目的代码吗？",
        )
    )
    assert capability.state_updates["conversation_only"] is True
    assert capability.state_updates["brief_dirty"] is False
    assert adapter.brief_calls == 0
    action = await hook.handle(
        HookContext(
            session_id="s",
            run_id="r",
            point=HookPoint.USER_MESSAGE_COMMITTED,
            status=RunStatus.BRIEFING,
            user_text="你能帮我查看这个项目的代码并修复报错吗？",
        )
    )
    assert action.state_updates["brief_dirty"] is True


@pytest.mark.asyncio
async def test_short_checkin_skips_brief_model():
    adapter = FakeModelAdapter()
    hook = TaskBriefHook(adapter)
    outcome = await hook.handle(
        HookContext(
            session_id="s",
            run_id="r",
            point=HookPoint.USER_MESSAGE_COMMITTED,
            status=RunStatus.BRIEFING,
            user_text="在吗？",
        )
    )
    assert outcome.state_updates["conversation_only"] is True
    assert adapter.brief_calls == 0


@pytest.mark.asyncio
async def test_low_impact_or_resolved_request_opens_full_toolset():
    hook = TaskBriefHook(FakeModelAdapter(briefs=[TaskBrief(needs_clarification=False, reason="ready")]))
    outcome = await hook.handle(
        HookContext(
            session_id="s",
            run_id="r",
            point=HookPoint.BEFORE_MODEL_REQUEST,
            status=RunStatus.BRIEFING,
            user_text="use sensible defaults",
            state={"brief_dirty": True, "discovery_rounds": 0},
        )
    )
    assert outcome.allowed_tools is None
    assert outcome.state_updates["brief_phase"] == "executing"


@pytest.mark.asyncio
async def test_brief_cannot_block_without_a_concrete_missing_point():
    adapter = FakeModelAdapter(briefs=[TaskBrief(needs_clarification=True, reason="vague", missing_points=[])])
    hook = TaskBriefHook(adapter)
    outcome = await hook.handle(
        HookContext(
            session_id="s",
            run_id="r",
            point=HookPoint.BEFORE_MODEL_REQUEST,
            status=RunStatus.BRIEFING,
            user_text="你好",
            state={"brief_dirty": True},
        )
    )
    assert outcome.state_updates["brief_phase"] == "executing"
    assert outcome.state_updates["task_brief"]["needs_clarification"] is False
