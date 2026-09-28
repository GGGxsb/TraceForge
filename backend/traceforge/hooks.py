from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass, field
from typing import Protocol

from .models import HookContext, HookOutcome, HookPoint, RiskLevel


class AgentHook(Protocol):
    name: str
    points: set[HookPoint]
    priority: int
    timeout_seconds: float
    failure_mode: str

    async def handle(self, context: HookContext) -> HookOutcome: ...


def _risk_rank(value: RiskLevel | None) -> int:
    return {None: -1, RiskLevel.ALLOW: 0, RiskLevel.ASK: 1, RiskLevel.DENY: 2}[value]


@dataclass(slots=True)
class HookDispatchResult:
    context_sections: dict[str, str] = field(default_factory=dict)
    entry_drafts: list = field(default_factory=list)
    allowed_tools: set[str] | None = None
    risk_floor: RiskLevel | None = None
    action: str = "continue"
    state_updates: dict = field(default_factory=dict)
    invalidate_projection: bool = False
    errors: list[str] = field(default_factory=list)


class HookRegistry:
    def __init__(self) -> None:
        self._hooks: list[AgentHook] = []

    def register(self, hook: AgentHook) -> None:
        required = ("name", "points", "priority", "timeout_seconds", "failure_mode", "handle")
        missing = [name for name in required if not hasattr(hook, name)]
        if missing:
            raise TypeError(f"Invalid hook; missing: {', '.join(missing)}")
        if hook.failure_mode not in {"open", "closed"}:
            raise ValueError(f"Invalid hook failure mode: {hook.failure_mode}")
        if any(existing.name == hook.name for existing in self._hooks):
            raise ValueError(f"Hook already registered: {hook.name}")
        self._hooks.append(hook)
        self._hooks.sort(key=lambda item: (item.priority, item.name))

    async def dispatch(self, context: HookContext) -> HookDispatchResult:
        merged = HookDispatchResult()
        for hook in self._hooks:
            if context.point not in hook.points:
                continue
            try:
                outcome = await asyncio.wait_for(hook.handle(context), timeout=hook.timeout_seconds)
            except Exception as exc:  # noqa: BLE001 - plugins are an isolation boundary
                message = f"{hook.name}: {type(exc).__name__}: {exc}"
                merged.errors.append(message)
                if hook.failure_mode == "closed":
                    merged.action = "abort"
                    merged.risk_floor = RiskLevel.DENY
                    break
                continue
            merged.context_sections.update(outcome.context_sections)
            merged.entry_drafts.extend(outcome.entry_drafts)
            if outcome.allowed_tools is not None:
                merged.allowed_tools = (
                    set(outcome.allowed_tools)
                    if merged.allowed_tools is None
                    else merged.allowed_tools.intersection(outcome.allowed_tools)
                )
            if _risk_rank(outcome.risk_floor) > _risk_rank(merged.risk_floor):
                merged.risk_floor = outcome.risk_floor
            if outcome.action == "abort":
                merged.action = "abort"
            elif outcome.action == "pause" and merged.action != "abort":
                merged.action = "pause"
            merged.state_updates.update(outcome.state_updates)
            merged.invalidate_projection = merged.invalidate_projection or outcome.invalidate_projection
        return merged


def load_configured_hook(spec: str) -> AgentHook:
    """Load trusted local Python code from ``module:attribute`` configuration."""
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"Hook must use module:attribute syntax: {spec}")
    candidate = getattr(importlib.import_module(module_name), attribute)
    if isinstance(candidate, type):
        return candidate()
    if callable(candidate) and not hasattr(candidate, "handle"):
        return candidate()
    return candidate
