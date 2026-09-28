import asyncio

import pytest

from traceforge.hooks import HookRegistry
from traceforge.models import HookContext, HookOutcome, HookPoint, RiskLevel, RunStatus


class RestrictiveHook:
    def __init__(self, name, priority, tools, risk=None):
        self.name = name
        self.priority = priority
        self.points = {HookPoint.BEFORE_MODEL_REQUEST}
        self.timeout_seconds = 1
        self.failure_mode = "open"
        self.tools = tools
        self.risk = risk

    async def handle(self, context):
        return HookOutcome(allowed_tools=set(self.tools), risk_floor=self.risk)


@pytest.mark.asyncio
async def test_hook_merge_only_reduces_tools_and_raises_risk():
    registry = HookRegistry()
    registry.register(RestrictiveHook("first", 10, {"read_file", "apply_patch"}, RiskLevel.ALLOW))
    registry.register(RestrictiveHook("second", 20, {"read_file"}, RiskLevel.ASK))
    outcome = await registry.dispatch(
        HookContext(
            session_id="s",
            run_id="r",
            point=HookPoint.BEFORE_MODEL_REQUEST,
            status=RunStatus.BRIEFING,
        )
    )
    assert outcome.allowed_tools == {"read_file"}
    assert outcome.risk_floor == RiskLevel.ASK


class FailingHook:
    name = "failing"
    priority = 1
    points = {HookPoint.BEFORE_MODEL_REQUEST}
    timeout_seconds = 0.01

    def __init__(self, failure_mode: str):
        self.failure_mode = failure_mode

    async def handle(self, context):
        await asyncio.sleep(0.1)
        return HookOutcome()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "action", "risk"),
    [("open", "continue", None), ("closed", "abort", RiskLevel.DENY)],
)
async def test_hook_timeout_respects_failure_mode(mode, action, risk):
    registry = HookRegistry()
    registry.register(FailingHook(mode))
    result = await registry.dispatch(
        HookContext(
            session_id="s",
            run_id="r",
            point=HookPoint.BEFORE_MODEL_REQUEST,
            status=RunStatus.BRIEFING,
        )
    )
    assert result.action == action
    assert result.risk_floor == risk
    assert result.errors
