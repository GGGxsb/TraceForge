from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from infra.runner.tool_worker import create_file, delete_file
from traceforge.agent import AgentRunner, ApprovalBroker
from traceforge.context import CompactionService, ContextProjector
from traceforge.hooks import HookRegistry
from traceforge.model_adapter import ModelDelta
from traceforge.models import (
    MissingPoint,
    PermissionMode,
    RunStatus,
    SandboxCapabilities,
    TaskBrief,
    ToolCall,
    ToolExecutionResult,
    WorkspaceRecord,
)
from traceforge.security import PolicyEngine
from traceforge.storage import EventHub, SessionStore
from traceforge.task_brief import TaskBriefHook
from traceforge.tools import TOOL_DEFINITIONS
from traceforge.skills import SkillCatalog
from traceforge.artifacts import ArtifactStore
from traceforge.tools import ToolService

from .fakes import FakeModelAdapter


class ParallelReadTools:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    def definitions(self, allowed=None):
        return [item for item in TOOL_DEFINITIONS if allowed is None or item["name"] in allowed]

    async def execute(self, session_id, workspace, tool_name, arguments):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.03)
        self.active -= 1
        return ToolExecutionResult(output=f"read {arguments['path']}")


class LocalFileTools:
    class SandboxStatus:
        async def inspect(self):
            return SandboxCapabilities(backend="test", ready=True)

    sandbox = SandboxStatus()

    def definitions(self, allowed=None):
        return [item for item in TOOL_DEFINITIONS if allowed is None or item["name"] in allowed]

    async def execute(self, session_id, workspace, tool_name, arguments):
        if tool_name == "create_file":
            create_file(arguments, Path(workspace))
        elif tool_name == "read_file":
            return ToolExecutionResult(output=(Path(workspace) / arguments["path"]).read_text(encoding="utf-8"))
        elif tool_name == "delete_file":
            delete_file(arguments, Path(workspace))
        else:
            raise AssertionError(tool_name)
        return ToolExecutionResult(output=f"{tool_name} completed")


class BriefFailureAdapter(FakeModelAdapter):
    async def analyze_task_brief(self, user_text, evidence, previous):
        raise RuntimeError("brief model unavailable")


class CapturingReasoningAdapter(FakeModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            turns=[
                [
                    ModelDelta(type="reasoning_delta", text="read "),
                    ModelDelta(type="reasoning_delta", text="file"),
                    ModelDelta(type="reasoning_item", raw={
                        "id": "rs_1", "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": "read file"}],
                    }),
                    ModelDelta(type="tool_call", tool_call=ToolCall(
                        call_id="call-1", name="read_file", arguments={"path": "a.py"},
                    )),
                    ModelDelta(type="done"),
                ],
                [ModelDelta(type="text_delta", text="完成"), ModelDelta(type="done")],
            ],
        )
        self.requests: list[list[dict]] = []

    async def stream_turn(self, instructions, input_items, tools):
        self.requests.append(input_items)
        async for delta in super().stream_turn(instructions, input_items, tools):
            yield delta


class CapturingSkillAdapter(FakeModelAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.input_items = []
        self.instructions = ""

    async def stream_turn(self, instructions, input_items, tools):
        self.instructions = instructions
        self.input_items = input_items
        async for delta in super().stream_turn(instructions, input_items, tools):
            yield delta


def make_runner(tmp_path: Path, adapter: FakeModelAdapter, tools) -> tuple[AgentRunner, object]:
    sessions = SessionStore(tmp_path / "sessions")
    workspace = WorkspaceRecord(path=str(tmp_path), name="repo", kind="directory")
    session = sessions.create(workspace)
    hooks = HookRegistry()
    hooks.register(TaskBriefHook(adapter))
    runner = AgentRunner(
        sessions=sessions,
        events=EventHub(),
        hooks=hooks,
        adapter=adapter,
        projector=ContextProjector(128_000, 16_000),
        compactor=CompactionService(adapter, 128_000, 16_000, 20_000),
        tools=tools,
        policy=PolicyEngine(),
        approvals=ApprovalBroker(),
        max_rounds=5,
        approval_timeout=1,
    )
    return runner, session


@pytest.mark.asyncio
async def test_auto_approve_allows_reviewable_delete_without_prompt(tmp_path: Path):
    target = tmp_path / "remove.txt"
    target.write_text("temporary", encoding="utf-8")
    adapter = FakeModelAdapter(turns=[
        [ModelDelta(type="tool_call", tool_call=ToolCall(
            call_id="delete-1", name="delete_file", arguments={"path": "remove.txt"},
        )), ModelDelta(type="done")],
        [ModelDelta(type="text_delta", text="done"), ModelDelta(type="done")],
    ])
    runner, session = make_runner(tmp_path, adapter, LocalFileTools())
    await session.append("permission_mode", {"mode": PermissionMode.AUTO_APPROVE.value})
    run = runner.start(session.header.id, "删除临时文件")
    await run.task
    assert run.status == RunStatus.COMPLETED
    assert not target.exists()
    assert not any(entry.type == "approval_request" for entry in session.entries)
    decision = next(entry for entry in session.entries if entry.type == "approval_decision")
    assert decision.payload["automatic"] is True
    assert decision.payload["permission_mode"] == "auto_approve"


@pytest.mark.asyncio
async def test_auto_approve_cannot_override_core_path_denial(tmp_path: Path):
    adapter = FakeModelAdapter(turns=[
        [ModelDelta(type="tool_call", tool_call=ToolCall(
            call_id="read-1", name="read_file", arguments={"path": "../outside.txt"},
        )), ModelDelta(type="done")],
        [ModelDelta(type="text_delta", text="done"), ModelDelta(type="done")],
    ])
    tools = ParallelReadTools()
    runner, session = make_runner(tmp_path, adapter, tools)
    await session.append("permission_mode", {"mode": PermissionMode.AUTO_APPROVE.value})
    run = runner.start(session.header.id, "读取项目外文件")
    await run.task
    assert run.status == RunStatus.COMPLETED
    assert tools.max_active == 0
    assert not any(entry.type in {"approval_request", "approval_decision"} for entry in session.entries)
    result = next(entry for entry in session.entries if entry.type == "tool_result")
    assert result.payload["is_error"] is True


@pytest.mark.asyncio
async def test_explicit_skill_command_loads_snapshot_and_project_inventory(tmp_path: Path):
    skill_dir = tmp_path / ".pi" / "skills" / "review-code"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: review-code\ndescription: Review code when requested\n---\n\nCheck tests first.\n",
        encoding="utf-8",
    )
    adapter = CapturingSkillAdapter()
    runner, session = make_runner(tmp_path, adapter, ParallelReadTools())
    runner.skills = SkillCatalog(tmp_path / "config", home=tmp_path / "home")
    run = runner.start(session.header.id, "/skill:review-code inspect the parser")
    await run.task
    activation = next(entry for entry in session.entries if entry.type == "skill_activation")
    assert activation.payload["arguments"] == "inspect the parser"
    assert "Check tests first." in activation.payload["content"]
    assert any("review-code: Review code when requested" in item.get("content", "")
               for item in adapter.input_items)
    assert any("Check tests first." in item.get("content", "") for item in adapter.input_items)


@pytest.mark.asyncio
async def test_explicit_only_skill_cannot_be_loaded_by_model_without_user_command(tmp_path: Path):
    skill_dir = tmp_path / ".pi" / "skills" / "private-workflow"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: private-workflow\ndescription: Explicit-only workflow\n"
        "disable-model-invocation: true\n---\n\nPrivate instructions.\n",
        encoding="utf-8",
    )
    catalog = SkillCatalog(tmp_path / "config", home=tmp_path / "home")
    tools = ToolService(object(), ArtifactStore(tmp_path / "artifacts"), tmp_path / "worker.py", skills=catalog)
    adapter = FakeModelAdapter(turns=[
        [ModelDelta(type="tool_call", tool_call=ToolCall(
            call_id="skill-1", name="read_skill", arguments={"name": "private-workflow", "path": None},
        )), ModelDelta(type="done")],
        [ModelDelta(type="text_delta", text="done"), ModelDelta(type="done")],
    ])
    runner, session = make_runner(tmp_path, adapter, tools)
    runner.skills = catalog
    run = runner.start(session.header.id, "检查仓库")
    await run.task
    result = next(entry for entry in session.entries if entry.type == "tool_result")
    assert result.payload["is_error"] is True
    assert "Tool not available" in result.payload["output"]


@pytest.mark.asyncio
async def test_agent_replays_model_reasoning_on_follow_up_request(tmp_path: Path):
    adapter = CapturingReasoningAdapter()
    runner, session = make_runner(tmp_path, adapter, ParallelReadTools())
    queue = runner.events.subscribe(session.header.id)
    run = runner.start(session.header.id, "读取 a.py")
    await run.task
    streamed = []
    while not queue.empty():
        event = queue.get_nowait()
        if event.type == "reasoning_delta":
            streamed.append(event.payload["delta"])
    runner.events.unsubscribe(session.header.id, queue)
    assert run.status.value == "completed"
    assert "".join(streamed) == "read file"
    assert len(adapter.requests) == 2
    assert [item.get("type") for item in adapter.requests[1][-3:]] == [
        "reasoning", "function_call", "function_call_output",
    ]
    assert any(entry.type == "model_reasoning" for entry in session.entries)


@pytest.mark.asyncio
async def test_agent_persists_clarification_question_and_stops(tmp_path: Path):
    adapter = FakeModelAdapter(
        briefs=[
            TaskBrief(
                needs_clarification=True,
                reason="deployment target missing",
                missing_points=[
                    MissingPoint(
                        description="deployment",
                        question="目标部署平台是什么？",
                        options=["Windows", "Linux"],
                        discoverable_from_repo=False,
                    )
                ],
            )
        ],
        turns=[[ModelDelta(type="text_delta", text="目标部署平台是什么？"), ModelDelta(type="done")]],
    )
    runner, session = make_runner(tmp_path, adapter, ParallelReadTools())
    run = runner.start(session.header.id, "实现服务")
    await run.task
    questions = [entry for entry in session.entries if entry.type == "clarification_question"]
    assert questions[0].payload["gap_id"]
    assert questions[0].payload["question"] == "目标部署平台是什么？"
    assert questions[0].payload["options"] == ["Windows", "Linux"]
    assert questions[0].payload["status"] == "pending"
    assert not any(entry.type == "assistant_message" for entry in session.entries)
    assert len(adapter.turns) == 1
    assert run.status.value == "completed"


@pytest.mark.asyncio
async def test_agent_asks_only_primary_user_blocker_per_round(tmp_path: Path):
    adapter = FakeModelAdapter(briefs=[TaskBrief(
        needs_clarification=True,
        reason="missing legal choice",
        missing_points=[
            MissingPoint(id="repo", description="current license file", question="现有许可证在哪？", discoverable_from_repo=True),
            MissingPoint(id="user", description="approved license", question="法务批准哪种许可证？", discoverable_from_repo=False),
            MissingPoint(id="extra", description="copyright owner", question="版权持有人是谁？", discoverable_from_repo=False),
        ],
    )])
    runner, session = make_runner(tmp_path, adapter, ParallelReadTools())
    run = runner.start(session.header.id, "按法务批准的许可证更新项目")
    await run.task
    questions = [entry for entry in session.entries if entry.type == "clarification_question"]
    assert [entry.payload["question"] for entry in questions] == ["法务批准哪种许可证？"]
    brief = next(entry for entry in session.entries if entry.type == "task_brief")
    assert len(brief.payload["brief"]["missing_points"]) == 3


@pytest.mark.asyncio
async def test_greeting_skips_brief_and_replies_without_clarification(tmp_path: Path):
    adapter = FakeModelAdapter(turns=[[ModelDelta(type="text_delta", text="你好！"), ModelDelta(type="done")]])
    runner, session = make_runner(tmp_path, adapter, ParallelReadTools())
    run = runner.start(session.header.id, "你好")
    await run.task
    assert adapter.brief_calls == 0
    assert not any(entry.type == "clarification_question" for entry in session.entries)
    assert any(entry.type == "assistant_message" and entry.payload["content"] == "你好！" for entry in session.entries)


@pytest.mark.asyncio
async def test_read_only_tool_calls_execute_concurrently_and_persist_in_order(tmp_path: Path):
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    adapter = FakeModelAdapter(
        briefs=[TaskBrief()],
        turns=[
            [
                ModelDelta(
                    type="tool_call",
                    tool_call=ToolCall(call_id="call-1", name="read_file", arguments={"path": "one.txt"}),
                ),
                ModelDelta(
                    type="tool_call",
                    tool_call=ToolCall(call_id="call-2", name="read_file", arguments={"path": "two.txt"}),
                ),
                ModelDelta(type="done"),
            ],
            [ModelDelta(type="text_delta", text="done"), ModelDelta(type="done")],
        ],
    )
    tools = ParallelReadTools()
    runner, session = make_runner(tmp_path, adapter, tools)
    run = runner.start(session.header.id, "read both")
    assert runner.active_for_session(session.header.id) is run
    await run.task
    assert runner.active_for_session(session.header.id) is None
    assert tools.max_active == 2
    calls_and_results = [
        (entry.type, entry.payload.get("call_id"))
        for entry in session.get_branch()
        if entry.type in {"tool_call", "tool_result"}
    ]
    assert calls_and_results == [
        ("tool_call", "call-1"),
        ("tool_call", "call-2"),
        ("tool_result", "call-1"),
        ("tool_result", "call-2"),
    ]
    assert session.validate_tool_pairs() == []
    assert run.status.value == "completed"
    assert any(entry.type == "assistant_message" and entry.payload["content"] == "done" for entry in session.entries)


@pytest.mark.asyncio
async def test_mixed_tool_batch_records_all_calls_before_any_result(tmp_path: Path):
    adapter = FakeModelAdapter(turns=[
        [
            ModelDelta(type="tool_call", tool_call=ToolCall(
                call_id="write-1", name="create_file", arguments={"path": "a.txt", "content": "hello"},
            )),
            ModelDelta(type="tool_call", tool_call=ToolCall(
                call_id="read-1", name="read_file", arguments={"path": "a.txt"},
            )),
            ModelDelta(type="done"),
        ],
        [ModelDelta(type="text_delta", text="done"), ModelDelta(type="done")],
    ])
    runner, session = make_runner(tmp_path, adapter, LocalFileTools())
    run = runner.start(session.header.id, "创建并读取文件")
    await run.task
    assert run.status.value == "completed"
    assert [(entry.type, entry.payload.get("call_id")) for entry in session.entries
            if entry.type in {"tool_call", "tool_result"}] == [
        ("tool_call", "write-1"),
        ("tool_call", "read-1"),
        ("tool_result", "write-1"),
        ("tool_result", "read-1"),
    ]
    assert session.validate_tool_pairs() == []


@pytest.mark.asyncio
async def test_approval_broker_validates_fingerprint_and_times_out():
    broker = ApprovalBroker()
    pending = asyncio.create_task(broker.request("approval", "session", "fingerprint", 1))
    await asyncio.sleep(0)
    with pytest.raises(PermissionError):
        broker.decide("session", "approval", "allow_once", "wrong")
    broker.decide("session", "approval", "allow_session", "fingerprint")
    assert await pending == "allow_session"
    assert broker.is_granted("session", "fingerprint")
    assert await broker.request("timeout", "session", "another", 0) == "timeout"


@pytest.mark.asyncio
async def test_task_brief_failure_is_recorded_and_main_agent_continues(tmp_path: Path):
    adapter = BriefFailureAdapter()
    runner, session = make_runner(tmp_path, adapter, ParallelReadTools())
    run = runner.start(session.header.id, "continue even if the brief model fails")
    await run.task
    assert run.status.value == "completed"
    assert any(entry.type == "hook_state" for entry in session.entries)
    assert any(entry.type == "assistant_message" for entry in session.entries)


@pytest.mark.asyncio
async def test_agent_creates_file_waits_for_approval_then_deletes_and_finishes(tmp_path: Path):
    adapter = FakeModelAdapter(
        turns=[
            [
                ModelDelta(
                    type="tool_call",
                    tool_call=ToolCall(call_id="create-1", name="create_file", arguments={"path": "new.txt", "content": "hello"}),
                ),
                ModelDelta(type="done"),
            ],
            [
                ModelDelta(
                    type="tool_call",
                    tool_call=ToolCall(call_id="delete-1", name="delete_file", arguments={"path": "new.txt"}),
                ),
                ModelDelta(type="done"),
            ],
            [ModelDelta(type="text_delta", text="任务完成"), ModelDelta(type="done")],
        ]
    )
    runner, session = make_runner(tmp_path, adapter, LocalFileTools())
    queue = runner.events.subscribe(session.header.id)
    run = runner.start(session.header.id, "创建后删除测试文件")

    while True:
        event = await asyncio.wait_for(queue.get(), timeout=3)
        if event.type == "approval_request":
            runner.approvals.decide(
                session.header.id,
                event.payload["approval_id"],
                "allow_once",
                event.payload["policy"]["fingerprint"],
            )
            break
    await run.task
    runner.events.unsubscribe(session.header.id, queue)

    assert run.status.value == "completed"
    assert not (tmp_path / "new.txt").exists()
    assert session.validate_tool_pairs() == []
    assert [entry.payload["decision"] for entry in session.entries if entry.type == "approval_decision"] == ["allow_once"]
    assert any(entry.type == "assistant_message" and entry.payload["content"] == "任务完成" for entry in session.entries)
