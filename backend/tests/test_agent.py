from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from infra.runner.tool_worker import create_file, delete_file
from traceforge.agent import AgentRunner, ApprovalBroker
from traceforge.context import CompactionService, ContextProjector
from traceforge.memory import WorkspaceHandoffStore
from traceforge.hooks import HookRegistry
from traceforge.model_adapter import ModelDelta
from traceforge.models import (
    MissingPoint,
    PermissionMode,
    RunStatus,
    ExecutionResult,
    HookOutcome,
    HookPoint,
    SandboxCapabilities,
    TaskBrief,
    ToolCall,
    ToolExecutionResult,
    WorkspaceRecord,
    utc_now,
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


class RecordedSandbox:
    def __init__(self) -> None:
        self.requests = []

    async def inspect(self):
        return SandboxCapabilities(backend="test", ready=True)

    async def execute(self, request):
        self.requests.append(request)
        return ExecutionResult(
            execution_id=request.execution_id, backend="test", stdout="fetched",
            exit_code=0, started_at=utc_now(), ended_at=utc_now(),
        )


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
async def test_running_agent_compacts_after_tool_batch_before_next_model_call(tmp_path: Path):
    (tmp_path / "large.txt").write_text("x" * 6000, encoding="utf-8")
    adapter = FakeModelAdapter(turns=[
        [ModelDelta(type="tool_call", tool_call=ToolCall(
            call_id="read-1", name="read_file", arguments={"path": "large.txt"},
        )), ModelDelta(type="done")],
        [ModelDelta(type="text_delta", text="完成"), ModelDelta(type="done")],
    ])
    runner, session = make_runner(tmp_path, adapter, LocalFileTools())
    await session.append("user_message", {"content": "旧问题 " * 300})
    await session.append("assistant_message", {"content": "旧回答 " * 300})
    runner.projector = ContextProjector(2300, 200)
    runner.compactor = CompactionService(adapter, 2300, 200, 300)
    runner.compactor.handoff_store = WorkspaceHandoffStore(
        tmp_path / "handoffs", runner.sessions, adapter, runner.compactor,
    )

    class RecordingHook:
        name = "record_compaction_order"
        points = {
            HookPoint.BEFORE_MODEL_REQUEST,
            HookPoint.AFTER_TOOL_BATCH,
            HookPoint.BEFORE_COMPACTION,
            HookPoint.AFTER_COMPACTION,
        }
        priority = 100
        timeout_seconds = 1
        failure_mode = "closed"

        def __init__(self):
            self.seen = []

        async def handle(self, context):
            self.seen.append(context.point)
            return HookOutcome()

    hook = RecordingHook()
    runner.hooks.register(hook)
    run = runner.start(session.header.id, "读取 large.txt")
    await run.task

    assert run.status == RunStatus.COMPLETED
    assert hook.seen == [
        HookPoint.BEFORE_MODEL_REQUEST,
        HookPoint.AFTER_TOOL_BATCH,
        HookPoint.BEFORE_COMPACTION,
        HookPoint.AFTER_COMPACTION,
        HookPoint.BEFORE_MODEL_REQUEST,
    ]
    assert len([entry for entry in session.entries if entry.type == "context_checkpoint"]) == 1


@pytest.mark.asyncio
async def test_auto_approve_asks_for_detected_delete_risk(tmp_path: Path):
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
    queue = runner.events.subscribe(session.header.id)
    run = runner.start(session.header.id, "删除临时文件")
    while True:
        event = await asyncio.wait_for(queue.get(), timeout=3)
        if event.type == "approval_request":
            runner.approvals.decide(
                session.header.id, event.payload["approval_id"], "allow_once",
                event.payload["policy"]["fingerprint"],
            )
            break
    await run.task
    runner.events.unsubscribe(session.header.id, queue)
    assert run.status == RunStatus.COMPLETED
    assert not target.exists()
    assert any(entry.type == "approval_request" for entry in session.entries)
    decision = next(entry for entry in session.entries if entry.type == "approval_decision")
    assert decision.payload["decision"] == "allow_once"
    assert not decision.payload.get("automatic")


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
async def test_full_access_agent_can_read_outside_project_without_approval(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside content", encoding="utf-8")
    adapter = FakeModelAdapter(turns=[
        [ModelDelta(type="tool_call", tool_call=ToolCall(
            call_id="read-1", name="read_file", arguments={"path": str(outside)},
        )), ModelDelta(type="done")],
        [ModelDelta(type="text_delta", text="done"), ModelDelta(type="done")],
    ])
    tools = ToolService(None, ArtifactStore(tmp_path / "artifacts"), tmp_path / "worker.py")
    runner, session = make_runner(workspace, adapter, tools)
    await session.append("permission_mode", {"mode": PermissionMode.FULL_ACCESS.value})
    run = runner.start(session.header.id, "读取项目外的测试文件")
    await run.task
    assert run.status == RunStatus.COMPLETED
    assert not any(entry.type == "approval_request" for entry in session.entries)
    result = next(entry for entry in session.entries if entry.type == "tool_result")
    assert result.payload["is_error"] is False
    assert "outside content" in result.payload["output"]


@pytest.mark.asyncio
async def test_existing_file_error_is_returned_then_recovered_with_patch(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")

    class RecoveryAdapter(FakeModelAdapter):
        def __init__(self):
            super().__init__(turns=[
                [ModelDelta(type="tool_call", tool_call=ToolCall(
                    call_id="create-existing", name="create_file",
                    arguments={"path": "app.py", "content": "overwrite"},
                )), ModelDelta(type="done")],
                [ModelDelta(type="tool_call", tool_call=ToolCall(
                    call_id="patch-existing", name="apply_patch",
                    arguments={"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"},
                )), ModelDelta(type="done")],
                [ModelDelta(type="text_delta", text="done"), ModelDelta(type="done")],
            ])
            self.requests = []

        async def stream_turn(self, instructions, input_items, tools):
            self.requests.append(input_items)
            async for delta in super().stream_turn(instructions, input_items, tools):
                yield delta

    adapter = RecoveryAdapter()
    worker = Path(__file__).resolve().parents[2] / "infra" / "runner" / "tool_worker.py"
    tools = ToolService(None, ArtifactStore(tmp_path / "artifacts"), worker)
    runner, session = make_runner(tmp_path, adapter, tools)
    await session.append("permission_mode", {"mode": PermissionMode.FULL_ACCESS.value})
    run = runner.start(session.header.id, "把 app.py 中的 value 改为 2")
    await run.task
    assert run.status == RunStatus.COMPLETED
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    results = [entry.payload for entry in session.entries if entry.type == "tool_result"]
    assert [item["is_error"] for item in results] == [True, False]
    assert "read_file, then apply_patch" in results[0]["output"]
    next_turn_results = [item for item in adapter.requests[1] if item.get("type") == "function_call_output"]
    assert len(next_turn_results) == 1
    assert next_turn_results[0]["call_id"] == "create-existing"
    assert "No changes made" in next_turn_results[0]["output"]


@pytest.mark.asyncio
async def test_invalid_network_type_never_reaches_approval_or_execution(tmp_path: Path):
    adapter = FakeModelAdapter(turns=[
        [ModelDelta(type="tool_call", tool_call=ToolCall(
            call_id="bad-network", name="run_command",
            arguments={"command": "echo test", "network": "false"},
        )), ModelDelta(type="done")],
        [ModelDelta(type="text_delta", text="done"), ModelDelta(type="done")],
    ])
    sandbox = RecordedSandbox()
    tools = ToolService(sandbox, ArtifactStore(tmp_path / "artifacts"), tmp_path / "worker.py")
    runner, session = make_runner(tmp_path, adapter, tools)
    run = runner.start(session.header.id, "检查工具参数")
    await run.task
    assert run.status == RunStatus.COMPLETED
    assert sandbox.requests == []
    assert not any(entry.type == "approval_request" for entry in session.entries)
    result = next(entry for entry in session.entries if entry.type == "tool_result")
    assert result.payload["is_error"] is True
    assert "Invalid tool arguments" in result.payload["output"]


@pytest.mark.asyncio
async def test_auto_mode_allows_low_risk_network_in_sandbox(tmp_path: Path):
    adapter = FakeModelAdapter(turns=[
        [ModelDelta(type="tool_call", tool_call=ToolCall(
            call_id="curl-1", name="run_command", arguments={"command": "curl https://example.com/status"},
        )), ModelDelta(type="done")],
        [ModelDelta(type="text_delta", text="done"), ModelDelta(type="done")],
    ])
    sandbox = RecordedSandbox()
    tools = ToolService(sandbox, ArtifactStore(tmp_path / "artifacts"), tmp_path / "worker.py")
    runner, session = make_runner(tmp_path, adapter, tools)
    await session.append("permission_mode", {"mode": PermissionMode.AUTO_APPROVE.value})
    run = runner.start(session.header.id, "读取公开状态")
    await run.task
    assert run.status == RunStatus.COMPLETED
    assert not any(entry.type == "approval_request" for entry in session.entries)
    assert len(sandbox.requests) == 1
    assert sandbox.requests[0].network is True


@pytest.mark.asyncio
async def test_request_mode_asks_for_every_network_call_even_after_session_grant(tmp_path: Path):
    adapter = FakeModelAdapter(turns=[
        [ModelDelta(type="tool_call", tool_call=ToolCall(
            call_id="curl-1", name="run_command", arguments={"command": "curl https://example.com/one"},
        )), ModelDelta(type="done")],
        [ModelDelta(type="tool_call", tool_call=ToolCall(
            call_id="curl-2", name="run_command", arguments={"command": "curl https://example.com/two"},
        )), ModelDelta(type="done")],
        [ModelDelta(type="text_delta", text="done"), ModelDelta(type="done")],
    ])
    sandbox = RecordedSandbox()
    tools = ToolService(sandbox, ArtifactStore(tmp_path / "artifacts"), tmp_path / "worker.py")
    runner, session = make_runner(tmp_path, adapter, tools)
    queue = runner.events.subscribe(session.header.id)
    run = runner.start(session.header.id, "读取两个公开地址")
    approvals = 0
    while approvals < 2:
        event = await asyncio.wait_for(queue.get(), timeout=3)
        if event.type == "approval_request":
            approvals += 1
            runner.approvals.decide(
                session.header.id, event.payload["approval_id"], "allow_session",
                event.payload["policy"]["fingerprint"],
            )
    await run.task
    runner.events.unsubscribe(session.header.id, queue)
    assert run.status == RunStatus.COMPLETED
    assert len(sandbox.requests) == 2
    assert all(request.network for request in sandbox.requests)


@pytest.mark.asyncio
async def test_request_mode_approved_external_file_edit_uses_scoped_host_worker(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = tmp_path / "outside.txt"
    second = tmp_path / "second-outside.txt"
    adapter = FakeModelAdapter(turns=[
        [ModelDelta(type="tool_call", tool_call=ToolCall(
            call_id="create-outside", name="create_file",
            arguments={"path": str(target), "content": "approved outside"},
        )), ModelDelta(type="done")],
        [ModelDelta(type="tool_call", tool_call=ToolCall(
            call_id="create-second-outside", name="create_file",
            arguments={"path": str(second), "content": "approved again"},
        )), ModelDelta(type="done")],
        [ModelDelta(type="text_delta", text="done"), ModelDelta(type="done")],
    ])
    worker = Path(__file__).resolve().parents[2] / "infra" / "runner" / "tool_worker.py"
    tools = ToolService(RecordedSandbox(), ArtifactStore(tmp_path / "artifacts"), worker)
    runner, session = make_runner(workspace, adapter, tools)
    queue = runner.events.subscribe(session.header.id)
    run = runner.start(session.header.id, "创建项目外测试文件")
    approvals = 0
    while approvals < 2:
        event = await asyncio.wait_for(queue.get(), timeout=3)
        if event.type == "approval_request":
            approvals += 1
            assert "external_file_write" in event.payload["policy"]["capabilities"]
            runner.approvals.decide(
                session.header.id, event.payload["approval_id"], "allow_session",
                event.payload["policy"]["fingerprint"],
            )
    await run.task
    runner.events.unsubscribe(session.header.id, queue)
    assert run.status == RunStatus.COMPLETED
    assert target.read_text(encoding="utf-8") == "approved outside"
    assert second.read_text(encoding="utf-8") == "approved again"
    started = [entry for entry in session.entries if entry.type == "sandbox_start"]
    assert [entry.payload["backend"] for entry in started] == ["host", "host"]
    assert [entry.payload["external_file_scope"] for entry in started] == [[str(target)], [str(second)]]


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
async def test_running_messages_steer_after_batch_and_follow_up_in_new_run(tmp_path: Path):
    (tmp_path / "a.py").write_text("pass\n", encoding="utf-8")
    class BlockingReadTools(ParallelReadTools):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(self, session_id, workspace, tool_name, arguments):
            self.started.set()
            await self.release.wait()
            return ToolExecutionResult(output="read complete")

    class CapturingAdapter(FakeModelAdapter):
        def __init__(self):
            super().__init__(turns=[
                [ModelDelta(type="tool_call", tool_call=ToolCall(
                    call_id="read-1", name="read_file", arguments={"path": "a.py"},
                )), ModelDelta(type="done")],
                [ModelDelta(type="text_delta", text="first run complete"), ModelDelta(type="done")],
                [ModelDelta(type="text_delta", text="follow-up complete"), ModelDelta(type="done")],
            ])
            self.requests = []

        async def stream_turn(self, instructions, input_items, tools):
            self.requests.append(input_items)
            async for delta in super().stream_turn(instructions, input_items, tools):
                yield delta

    adapter = CapturingAdapter()
    tools = BlockingReadTools()
    runner, session = make_runner(tmp_path, adapter, tools)
    first = runner.start(session.header.id, "检查项目")
    await asyncio.wait_for(tools.started.wait(), 2)
    steer = await runner.queue_message(session.header.id, first.id, "请同时检查类型", "after_tool_batch")
    follow = await runner.queue_message(session.header.id, first.id, "完成后给我总结", "after_run")
    assert [entry.id for entry in runner.pending_messages(session)] == [steer.id, follow.id]
    tools.release.set()
    await asyncio.wait_for(first.task, 2)
    for _ in range(100):
        next_run = next((item for item in runner.runs.values() if item.id != first.id), None)
        if next_run:
            await asyncio.wait_for(next_run.task, 2)
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("follow-up run did not start")
    assert first.status == RunStatus.COMPLETED
    assert next_run.status == RunStatus.COMPLETED
    assert runner.pending_messages(session) == []
    assert len(adapter.requests) == 3
    assert "请同时检查类型" not in str(adapter.requests[0])
    assert "请同时检查类型" in str(adapter.requests[1])
    assert "完成后给我总结" not in str(adapter.requests[1])
    assert "完成后给我总结" in str(adapter.requests[2])
    delivered = [entry.payload["queued_message_id"] for entry in session.entries
                 if entry.type == "user_message" and entry.payload.get("queued_message_id")]
    assert delivered == [steer.id, follow.id]
    assert session.validate_tool_pairs() == []


@pytest.mark.asyncio
async def test_queued_message_can_be_cancelled_and_survives_reload(tmp_path: Path):
    (tmp_path / "a.py").write_text("pass\n", encoding="utf-8")
    adapter = FakeModelAdapter(turns=[
        [ModelDelta(type="tool_call", tool_call=ToolCall(
            call_id="read-1", name="read_file", arguments={"path": "a.py"},
        )), ModelDelta(type="done")],
        [ModelDelta(type="text_delta", text="done"), ModelDelta(type="done")],
    ])

    class WaitingTools(ParallelReadTools):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(self, session_id, workspace, tool_name, arguments):
            self.started.set()
            await self.release.wait()
            return ToolExecutionResult(output="done")

    tools = WaitingTools()
    runner, session = make_runner(tmp_path, adapter, tools)
    run = runner.start(session.header.id, "读取")
    await asyncio.wait_for(tools.started.wait(), 2)
    queued = await runner.queue_message(session.header.id, run.id, "稍后执行", "after_run")
    reloaded = SessionStore(tmp_path / "sessions").get(session.header.id)
    assert [entry.id for entry in runner.pending_messages(reloaded)] == [queued.id]
    await runner.cancel_queued_message(session.header.id, queued.id)
    tools.release.set()
    await run.task
    await asyncio.sleep(0)
    assert runner.pending_messages(session) == []
    assert not any(entry.type == "user_message" and entry.payload.get("queued_message_id") == queued.id
                   for entry in session.entries)


@pytest.mark.asyncio
async def test_steering_waits_when_tool_round_limit_prevents_next_model_request(tmp_path: Path):
    (tmp_path / "a.py").write_text("pass\n", encoding="utf-8")
    adapter = FakeModelAdapter(turns=[
        [ModelDelta(type="tool_call", tool_call=ToolCall(
            call_id="read-1", name="read_file", arguments={"path": "a.py"},
        )), ModelDelta(type="done")],
    ])

    class WaitingTools(ParallelReadTools):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(self, session_id, workspace, tool_name, arguments):
            self.started.set()
            await self.release.wait()
            return ToolExecutionResult(output="done")

    tools = WaitingTools()
    runner, session = make_runner(tmp_path, adapter, tools)
    runner.max_rounds = 1
    run = runner.start(session.header.id, "读取")
    await asyncio.wait_for(tools.started.wait(), 2)
    queued = await runner.queue_message(session.header.id, run.id, "补充检查", "after_tool_batch")
    tools.release.set()
    await run.task
    assert run.status == RunStatus.COMPLETED
    assert [entry.id for entry in runner.pending_messages(session)] == [queued.id]
    assert not any(entry.type == "user_message" and entry.payload.get("queued_message_id") == queued.id
                   for entry in session.entries)


@pytest.mark.asyncio
async def test_steering_during_text_response_gets_another_model_turn(tmp_path: Path):
    class PausingAdapter(FakeModelAdapter):
        def __init__(self):
            super().__init__(turns=[
                [ModelDelta(type="text_delta", text="initial"), ModelDelta(type="done")],
                [ModelDelta(type="text_delta", text="revised"), ModelDelta(type="done")],
            ])
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.requests = []

        async def stream_turn(self, instructions, input_items, tools):
            self.requests.append(input_items)
            if len(self.requests) == 1:
                self.started.set()
                await self.release.wait()
            async for delta in super().stream_turn(instructions, input_items, tools):
                yield delta

    adapter = PausingAdapter()
    runner, session = make_runner(tmp_path, adapter, ParallelReadTools())
    run = runner.start(session.header.id, "先回答")
    await asyncio.wait_for(adapter.started.wait(), 2)
    queued = await runner.queue_message(session.header.id, run.id, "再考虑边界情况", "after_tool_batch")
    adapter.release.set()
    await asyncio.wait_for(run.task, 2)
    assert run.status == RunStatus.COMPLETED
    assert len(adapter.requests) == 2
    assert "再考虑边界情况" in str(adapter.requests[1])
    assert runner.pending_messages(session) == []
    assert sum(entry.payload.get("queued_message_id") == queued.id for entry in session.entries
               if entry.type == "user_message") == 1


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
