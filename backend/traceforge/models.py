from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class RunStatus(StrEnum):
    IDLE = "idle"
    BRIEFING = "briefing"
    DISCOVERING = "discovering"
    CLARIFYING = "clarifying"
    EXECUTING = "executing"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskLevel(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionMode(StrEnum):
    REQUEST_APPROVAL = "request_approval"
    AUTO_APPROVE = "auto_approve"
    FULL_ACCESS = "full_access"


class HookPoint(StrEnum):
    USER_MESSAGE_COMMITTED = "user_message_committed"
    BEFORE_MODEL_REQUEST = "before_model_request"
    AFTER_MODEL_RESPONSE = "after_model_response"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_RESULT = "after_tool_result"
    AFTER_TOOL_BATCH = "after_tool_batch"
    BEFORE_NEXT_MODEL_REQUEST = "before_next_model_request"
    BEFORE_COMPACTION = "before_compaction"
    AFTER_COMPACTION = "after_compaction"
    BEFORE_BRANCH_SWITCH = "before_branch_switch"
    AFTER_BRANCH_SWITCH = "after_branch_switch"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"


class SessionHeader(BaseModel):
    type: Literal["session"] = "session"
    version: int = 1
    id: str
    workspace_id: str
    workspace: str
    created_at: str = Field(default_factory=utc_now)
    kind: Literal["session", "subagent"] = "session"
    parent_session_id: str | None = None
    parent_run_id: str | None = None
    spawn_entry_id: str | None = None
    role: Literal["explore", "reviewer"] | None = None
    snapshot_id: str | None = None


class SessionEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    id: str = Field(default_factory=lambda: new_id("entry"))
    parent_id: str | None = None
    seq: int = 0
    run_id: str | None = None
    timestamp: str = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)


class MissingPoint(BaseModel):
    id: str = Field(default_factory=lambda: new_id("gap"))
    description: str
    question: str = ""
    options: list[str] = Field(default_factory=list)
    discoverable_from_repo: bool = False
    impact: str = ""


class TaskBrief(BaseModel):
    needs_clarification: bool = False
    reason: str = ""
    missing_points: list[MissingPoint] = Field(default_factory=list)


class ClarificationNode(BaseModel):
    id: str = Field(default_factory=lambda: new_id("question"))
    parent_question_id: str | None = None
    gap_id: str
    question: str
    answer: str | None = None
    status: Literal["pending", "answered", "superseded"] = "pending"
    source_message_id: str


class HookContext(BaseModel):
    session_id: str
    run_id: str
    point: HookPoint
    status: RunStatus
    active_leaf_id: str | None = None
    user_text: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)


class EntryDraft(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class HookOutcome(BaseModel):
    context_sections: dict[str, str] = Field(default_factory=dict)
    entry_drafts: list[EntryDraft] = Field(default_factory=list)
    allowed_tools: set[str] | None = None
    risk_floor: RiskLevel | None = None
    action: Literal["continue", "pause", "abort"] = "continue"
    state_updates: dict[str, Any] = Field(default_factory=dict)
    invalidate_projection: bool = False


class PolicyResult(BaseModel):
    decision: RiskLevel
    capabilities: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    affected_paths: list[str] = Field(default_factory=list)
    network: bool = False
    fingerprint: str = ""


class ToolCall(BaseModel):
    call_id: str
    name: str
    arguments: dict[str, Any]


class ToolExecutionResult(BaseModel):
    output: str
    is_error: bool = False
    exit_code: int | None = None
    artifact_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SandboxCapabilities(BaseModel):
    backend: str
    ready: bool
    reason: str = ""
    supports_network_toggle: bool = False
    supports_resource_limits: bool = False


class ExecutionRequest(BaseModel):
    execution_id: str = Field(default_factory=lambda: new_id("exec"))
    workspace: str
    command: str
    timeout_seconds: int = 120
    network: bool = False
    env: dict[str, str] = Field(default_factory=dict)
    workspace_read_only: bool = False


class ExecutionResult(BaseModel):
    execution_id: str
    backend: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    cancelled: bool = False
    started_at: str
    ended_at: str
    profile: dict[str, Any] = Field(default_factory=dict)


class WorkspaceRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("workspace"))
    path: str
    name: str
    kind: Literal["git", "directory"]
    created_at: str = Field(default_factory=utc_now)


class RunEvent(BaseModel):
    session_id: str
    run_id: str | None = None
    seq: int
    type: str
    timestamp: str = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)


class CompactionSummary(BaseModel):
    goal: str = ""
    constraints: list[str] = Field(default_factory=list)
    completed: list[str] = Field(default_factory=list)
    in_progress: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    read_files: list[str] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    commands_and_tests: list[str] = Field(default_factory=list)
    errors_and_blockers: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


class BranchSummary(CompactionSummary):
    branch_goal: str = ""
    recommended_next_step: str = ""
