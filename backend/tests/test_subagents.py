from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from infra.runner.tool_worker import read_only
from traceforge.artifacts import ArtifactStore
from traceforge.checkpoints import CheckpointStore
from traceforge.config import Settings
from traceforge.main import create_app
from traceforge.model_adapter import ModelDelta, usage_sink
from traceforge.models import ExecutionResult, PermissionMode, RunStatus, SandboxCapabilities, SessionHeader, ToolCall, new_id, utc_now
from traceforge.storage import JsonlSession
from traceforge.sandbox import DockerSandbox
from traceforge.subagents import SubAgentManager
from traceforge.tools import ToolService

from .fakes import FakeModelAdapter
from .test_agent import make_runner


def call(name, arguments, call_id=None):
    return ModelDelta(type="tool_call", tool_call=ToolCall(
        call_id=call_id or new_id("call"), name=name, arguments=arguments,
    ))


def finish(summary="调查完成", findings=None, unresolved=None):
    return call("finish_subtask", {"summary": summary, "findings": findings or [], "unresolved": unresolved or []})


class NoSandbox:
    async def inspect(self):
        return SandboxCapabilities(backend="docker", ready=False, reason="test offline")

    async def execute(self, request):
        raise AssertionError("No sandbox execution is permitted")


class ReadSandbox:
    def __init__(self):
        self.requests = []

    async def inspect(self):
        return SandboxCapabilities(backend="docker", ready=True)

    async def execute(self, request):
        self.requests.append(request)
        assert request.workspace_read_only is True and request.network is False
        assert " read-only " in request.command
        payload = json.loads(base64.urlsafe_b64decode(request.command.split()[-1]))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            read_only(payload, Path(request.workspace))
        return ExecutionResult(execution_id=request.execution_id, backend="docker", stdout=output.getvalue(),
                               exit_code=0, started_at=utc_now(), ended_at=utc_now(),
                               profile={"workspace_read_only": True, "network": False})


def setup(tmp_path, adapter, sandbox=None, **manager_options):
    repo = tmp_path / "repo"
    repo.mkdir()
    tools = ToolService(sandbox or NoSandbox(), ArtifactStore(tmp_path / "data" / "artifacts"),
                        Path(__file__).resolve().parents[2] / "infra" / "runner" / "tool_worker.py")
    runner, session = make_runner(repo, adapter, tools)
    manager = SubAgentManager(tmp_path / "data" / "subagents", runner.sessions, adapter, tools,
                              CheckpointStore(tmp_path / "data" / "checkpoints"), emit=runner._append, **manager_options)
    runner.subagents = manager
    tools.subagents_enabled = True
    return runner, session, manager


async def delegate(manager, parent, *, role="explore", task="回查历史", context=None, run_id="run-test"):
    tool = await parent.append("tool_call", {"call_id": new_id("call"), "name": "delegate_task"})
    return await manager.delegate(parent, run_id, tool.payload["call_id"],
                                  {"role": role, "task": task, "context_entry_ids": context or []},
                                  context_window=128000, budget_check=lambda: True)


@pytest.mark.asyncio
async def test_history_delegation_returns_verified_original_evidence_and_preserves_reasoning(tmp_path):
    class HistoryAdapter(FakeModelAdapter):
        def __init__(self):
            super().__init__()
            self.child_requests = []
            self.parent_requests = []
            self.child_steps = 0

        async def stream_turn(self, instructions, input_items, tools):
            child = any(tool["name"] == "finish_subtask" for tool in tools)
            if not child:
                self.parent_requests.append(input_items)
                if len(self.parent_requests) == 1:
                    yield call("delegate_task", {"role": "explore", "task": "搜索 UNIQUE_FACT 并查阅原文说明之前的决定", "context_entry_ids": []})
                else:
                    yield ModelDelta(type="text_delta", text="根据引用的原文，之前选择了方案 B。")
                return
            self.child_requests.append(input_items)
            self.child_steps += 1
            sink = usage_sink.get()
            if sink:
                await sink({"total_tokens": 20, "input_tokens": 10, "output_tokens": 10,
                            "cached_input_tokens": 0, "reasoning_tokens": 0})
            if self.child_steps == 1:
                yield ModelDelta(type="reasoning_item", raw={"id": "rs-child", "type": "reasoning", "content": [{"type": "reasoning_text", "text": "检索原文"}]})
                yield call("search_history", {"query": "UNIQUE_FACT", "scope": "workspace", "max_results": 10})
            elif self.child_steps == 2:
                outputs = [json.loads(item["output"]) for item in input_items if item.get("type") == "function_call_output"]
                ref = next(item for item in outputs[-1] if item["type"] == "assistant_message")
                yield call("read_history_entry", {"session_id": ref["session_id"], "entry_id": ref["entry_id"], "start_char": 0, "max_chars": 20000})
            else:
                ref = json.loads([item["output"] for item in input_items if item.get("type") == "function_call_output"][-1])
                assert "方案 B" in ref["content"]
                yield finish("原文确认选择了方案 B", [{"description": "历史决定", "file": None, "line": None,
                                                      "evidence": [{"session_id": ref["session_id"], "entry_id": ref["entry_id"]}]}])

    adapter = HistoryAdapter()
    runner, parent, manager = setup(tmp_path, adapter)
    old = runner.sessions.create(type("Workspace", (), {"id": parent.header.workspace_id, "path": parent.workspace})())
    original = await old.append("assistant_message", {"content": "UNIQUE_FACT：选择方案 B。RAW_ONLY_SENTINEL"})
    run = runner.start(parent.header.id, "找回之前的决定")
    await run.task
    assert run.status == RunStatus.COMPLETED
    result = next(entry for entry in parent.entries if entry.type == "tool_result")
    report = json.loads(result.payload["output"])
    assert report["status"] == "completed"
    assert report["findings"][0]["evidence"] == [{"session_id": old.header.id, "entry_id": original.id}]
    assert "RAW_ONLY_SENTINEL" not in json.dumps(adapter.parent_requests, ensure_ascii=False)
    assert "RAW_ONLY_SENTINEL" not in json.dumps(adapter.child_requests[0], ensure_ascii=False)
    assert any(item.get("id") == "rs-child" for item in adapter.child_requests[1])
    child = manager.get(parent.header.id, report["child_session_id"])
    assert child.header.parent_run_id == run.id and child.header.kind == "subagent"
    assert any("RAW_ONLY_SENTINEL" in entry.payload.get("output", "") for entry in child.entries if entry.type == "tool_result")
    assert len(runner.sessions.list()) == 2
    assert run.state["usage"]["total_tokens"] == 60
    assert not (Path(parent.workspace) / ".traceforge" / "handoff.md").exists()


@pytest.mark.asyncio
async def test_subagent_cannot_inherit_full_access_or_invoke_hidden_tools(tmp_path):
    adapter = FakeModelAdapter(turns=[
        [call("run_command", {"command": "touch escaped", "network": True}),
         call("create_file", {"path": "escaped", "content": "x"}),
         call("delegate_task", {"role": "explore", "task": "nested", "context_entry_ids": []}),
         call("read_file", {"path": "../outside.txt"}),
         call("read_history_entry", {"session_id": "session_fake", "entry_id": "entry_fake"})],
        [finish(unresolved=["无法执行不允许的操作"])],
    ])
    _, parent, manager = setup(tmp_path, adapter)
    await parent.append("permission_mode", {"mode": PermissionMode.FULL_ACCESS.value})
    result = await delegate(manager, parent)
    child = manager.get(parent.header.id, json.loads(result.output)["child_session_id"])
    errors = [entry for entry in child.entries if entry.type == "tool_result" and entry.payload["is_error"]]
    assert len(errors) == 5
    assert not (Path(parent.workspace) / "escaped").exists()
    assert all(entry.type != "approval_request" for entry in child.entries)
    assert not any(entry.type == "sandbox_start" for entry in child.entries)


@pytest.mark.asyncio
async def test_search_does_not_authorize_other_projects_or_unread_citations(tmp_path):
    adapter = FakeModelAdapter(turns=[
        [call("search_history", {"query": "FACT", "scope": "workspace", "max_results": 20})],
        [finish(findings=[{"description": "未经读取的结论", "file": None, "line": None,
                           "evidence": [{"session_id": "foreign", "entry_id": "fake"}]}])],
        [finish(unresolved=["证据不充分"])],
    ])
    runner, parent, manager = setup(tmp_path, adapter)
    await parent.append("user_message", {"content": "FACT 当前项目"})
    foreign_repo = tmp_path / "foreign"
    foreign_repo.mkdir()
    foreign = runner.sessions.create(type("Workspace", (), {"id": "foreign-project", "path": str(foreign_repo)})())
    await foreign.append("user_message", {"content": "FACT FOREIGN_SENTINEL"})
    result = await delegate(manager, parent)
    child = manager.get(parent.header.id, json.loads(result.output)["child_session_id"])
    outputs = [entry.payload["output"] for entry in child.entries if entry.type == "tool_result"]
    assert all("FOREIGN_SENTINEL" not in output for output in outputs)
    assert any("must first be read" in output for output in outputs)


@pytest.mark.asyncio
async def test_snapshot_contains_uncommitted_code_and_excludes_credentials(tmp_path):
    sandbox = ReadSandbox()
    adapter = FakeModelAdapter(turns=[
        [call("read_file", {"path": "app.py"})],
        [finish(findings=[{"description": "核对当前代码", "file": "app.py", "line": 1, "evidence": []}])],
    ])
    _, parent, manager = setup(tmp_path, adapter, sandbox)
    workspace = Path(parent.workspace)
    (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
    (workspace / ".env.local").write_text("secret=123", encoding="utf-8")
    result = await delegate(manager, parent, role="reviewer")
    assert not result.is_error, result.output
    report = json.loads(result.output)
    child = manager.get(parent.header.id, report["child_session_id"])
    code = Path(child.header.workspace)
    assert (code / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert not (code / ".env.local").exists()
    assert ".env.local" not in manager.checkpoints.manifest(parent, report["snapshot_id"])["files"]
    assert not (manager.checkpoints.root / parent.header.id / "blobs" / hashlib.sha256(b"secret=123").hexdigest()).exists()
    (workspace / "app.py").write_text("value = 3\n", encoding="utf-8")
    assert (code / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert sandbox.requests[0].workspace_read_only is True
    assert report["requires_current_code_check"] is True and report["snapshot_id"]


@pytest.mark.asyncio
async def test_unavailable_sandbox_fails_code_reads_without_host_fallback(tmp_path):
    adapter = FakeModelAdapter(turns=[[call("read_file", {"path": "app.py"})], [finish(unresolved=["沙箱不可用"])]])
    _, parent, manager = setup(tmp_path, adapter)
    (Path(parent.workspace) / "app.py").write_text("x = 1", encoding="utf-8")
    result = await delegate(manager, parent)
    child = manager.get(parent.header.id, json.loads(result.output)["child_session_id"])
    assert any("Sandbox unavailable" in entry.payload.get("output", "") for entry in child.entries)
    result = await delegate(manager, parent, role="reviewer", run_id="other-run")
    assert result.is_error and "Sandbox unavailable" in result.output


@pytest.mark.asyncio
async def test_selected_context_must_be_on_active_branch_and_calls_are_bounded(tmp_path):
    _, parent, manager = setup(tmp_path, FakeModelAdapter(turns=[[finish()]]))
    root = await parent.append("user_message", {"content": "root"})
    hidden = await parent.append("user_message", {"content": "inactive"})
    await parent.append("user_message", {"content": "active"}, parent_id=root.id)
    denied = await delegate(manager, parent, context=[hidden.id])
    assert denied.is_error
    first = await delegate(manager, parent)
    second = await delegate(manager, parent)
    third = await delegate(manager, parent)
    assert not first.is_error and not second.is_error
    assert third.is_error and "limit reached" in third.output


@pytest.mark.asyncio
async def test_timeout_and_context_limits_terminate_child_without_compacting_handoff(tmp_path):
    class SlowAdapter(FakeModelAdapter):
        async def stream_turn(self, *args):
            await asyncio.sleep(10)
            yield finish()

    _, parent, manager = setup(tmp_path, SlowAdapter(), timeout_seconds=0.1)
    timed = await delegate(manager, parent)
    assert json.loads(timed.output)["status"] == "timeout"
    manager.adapter = FakeModelAdapter(turns=[[finish()]])
    manager.timeout_seconds = 10
    tool = await parent.append("tool_call", {"call_id": "short-context", "name": "delegate_task"})
    limited = await manager.delegate(parent, "other-run", tool.payload["call_id"],
                                     {"role": "explore", "task": "调查", "context_entry_ids": []},
                                     context_window=1024, budget_check=lambda: True)
    assert json.loads(limited.output)["status"] == "partial"
    assert not (Path(parent.workspace) / ".traceforge" / "handoff.md").exists()


@pytest.mark.asyncio
async def test_parent_cancel_stops_child_and_preserves_paired_tools(tmp_path):
    ready = asyncio.Event()

    class WaitingAdapter(FakeModelAdapter):
        async def stream_turn(self, instructions, input_items, tools):
            if any(tool["name"] == "delegate_task" for tool in tools):
                yield call("delegate_task", {"role": "explore", "task": "回查历史", "context_entry_ids": []})
            else:
                ready.set()
                await asyncio.Event().wait()

    runner, parent, manager = setup(tmp_path, WaitingAdapter())
    run = runner.start(parent.header.id, "调查之前的决定")
    await asyncio.wait_for(ready.wait(), 3)
    await runner.cancel(run.id)
    assert run.status == RunStatus.CANCELLED
    spawn = next(entry for entry in parent.entries if entry.type == "subagent_spawn")
    child = manager.get(parent.header.id, spawn.payload["child_session_id"])
    report = next(entry for entry in child.entries if entry.type == "subagent_result")
    assert report.payload["status"] == "cancelled"
    outputs = [entry for entry in parent.entries if entry.type == "tool_result"]
    assert len(outputs) == 1 and outputs[0].payload["is_error"] is True


@pytest.mark.asyncio
async def test_restart_recovers_unfinished_child_exactly_once(tmp_path):
    _, parent, manager = setup(tmp_path, FakeModelAdapter())
    child_id = new_id("subagent")
    directory = manager._directory(parent.header.id, child_id)
    directory.mkdir(parents=True)
    child = JsonlSession.create(directory / "session.jsonl", SessionHeader(
        id=child_id, workspace_id=parent.header.workspace_id, workspace=parent.workspace,
        kind="subagent", parent_session_id=parent.header.id, parent_run_id="run-old", role="explore",
    ))
    await child.append("tool_call", {"call_id": "pending", "name": "read_file", "arguments": {"path": "a.py"}})
    await manager.recover()
    await manager.recover()
    recovered = manager.get(parent.header.id, child_id)
    assert len([entry for entry in recovered.entries if entry.type == "subagent_result"]) == 1
    assert len([entry for entry in recovered.entries if entry.type == "tool_result"]) == 1
    assert recovered.entries[-1].payload["status"] == "failed"


@pytest.mark.asyncio
async def test_parent_report_is_bounded_but_child_retains_full_report(tmp_path):
    full_summary = "详细说明" * 1500
    adapter = FakeModelAdapter(turns=[[finish(full_summary, unresolved=["未解决" * 330] * 12)]])
    _, parent, manager = setup(tmp_path, adapter)
    result = await delegate(manager, parent)
    assert len(result.output) <= 8000
    report = json.loads(result.output)
    assert report["report_truncated"] and report["omitted_unresolved"] > 0
    child = manager.get(parent.header.id, report["child_session_id"])
    full = next(entry.payload for entry in child.entries if entry.type == "subagent_result")
    assert full["summary"] == full_summary and len(full["unresolved"]) == 12
    saved = manager.tools.artifacts.root / child.header.id / f"{result.artifact_id}.log"
    assert full_summary in saved.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_partial_report_retains_read_evidence_without_claiming_completion(tmp_path):
    class PartialAdapter(FakeModelAdapter):
        async def stream_turn(self, instructions, input_items, tools):
            if not any(item.get("type") == "function_call_output" for item in input_items):
                yield call("search_history", {"query": "PARTIAL_FACT", "scope": "session", "max_results": 10})
            else:
                ref = json.loads(next(item["output"] for item in input_items if item.get("type") == "function_call_output"))[0]
                yield call("read_history_entry", {"session_id": ref["session_id"], "entry_id": ref["entry_id"]})

    _, parent, manager = setup(tmp_path, PartialAdapter(), max_turns=2)
    original = await parent.append("user_message", {"content": "PARTIAL_FACT 用户决定"})
    result = await delegate(manager, parent)
    report = json.loads(result.output)
    assert report["status"] == "partial"
    assert report["findings"][0]["evidence"] == [{"session_id": parent.header.id, "entry_id": original.id}]
    assert "尚未形成结论" in report["findings"][0]["description"]


@pytest.mark.asyncio
async def test_git_diff_snapshot_excludes_tracked_secrets(tmp_path):
    adapter = FakeModelAdapter(turns=[[call("git_diff", {})], [finish()]])
    _, parent, manager = setup(tmp_path, adapter)
    repo = Path(parent.workspace)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    (repo / ".env.local").write_text("key=old-secret\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "fixture"], check=True)
    (repo / "app.py").write_text("value = 2\n", encoding="utf-8")
    (repo / ".env.local").write_text("key=TOP_SECRET_SENTINEL\n", encoding="utf-8")
    result = await delegate(manager, parent)
    child = manager.get(parent.header.id, json.loads(result.output)["child_session_id"])
    git_output = next(entry.payload["output"] for entry in child.entries if entry.type == "tool_result" and entry.payload["tool_name"] == "git_diff")
    assert "+value = 2" in git_output
    assert "TOP_SECRET_SENTINEL" not in git_output and ".env.local" not in git_output
    assert not (Path(child.header.workspace) / ".env.local").exists()


@pytest.mark.asyncio
async def test_child_api_is_parent_scoped_and_session_delete_cleans_logs(tmp_path):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    services = app.state.services
    services.model_settings.adapter.replace(FakeModelAdapter(turns=[[finish()]]))
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = services.workspaces.register(str(repo))
    parent = services.sessions.create(workspace)
    other = services.sessions.create(workspace)
    result = await delegate(services.runner.subagents, parent)
    child_id = json.loads(result.output)["child_session_id"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        own = await client.get(f"/api/sessions/{parent.header.id}/subagents/{child_id}")
        assert own.status_code == 200 and own.json()["header"]["kind"] == "subagent"
        foreign = await client.get(f"/api/sessions/{other.header.id}/subagents/{child_id}")
        assert foreign.status_code == 404
        assert len((await client.get("/api/sessions")).json()) == 2
        deleted = await client.delete(f"/api/sessions/{parent.header.id}", headers={"X-TraceForge-UI": "1"})
        assert deleted.status_code == 200
    assert not services.runner.subagents._directory(parent.header.id).exists()
    assert not (services.artifacts_root / child_id).exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("TRACEFORGE_TEST_DOCKER") != "1", reason="Requires local Docker runner")
async def test_real_subagent_reader_and_os_read_only_mount(tmp_path):
    sandbox = DockerSandbox("traceforge-runner:local")
    adapter = FakeModelAdapter(turns=[
        [call("read_file", {"path": "app.py"})],
        [finish(findings=[{"description": "读取当前快照", "file": "app.py", "line": 1, "evidence": []}])],
    ])
    _, parent, manager = setup(tmp_path, adapter, sandbox)
    (Path(parent.workspace) / "app.py").write_text("value = 2\n", encoding="utf-8")
    result = await delegate(manager, parent, role="reviewer")
    assert not result.is_error, result.output
    child = manager.get(parent.header.id, json.loads(result.output)["child_session_id"])
    from traceforge.models import ExecutionRequest
    attempt = await sandbox.execute(ExecutionRequest(
        workspace=child.header.workspace, workspace_read_only=True,
        command="python3 -c 'from pathlib import Path; Path(\"app.py\").write_text(\"overwrite\")'",
    ))
    assert attempt.exit_code != 0 and "Read-only file system" in attempt.stderr
    assert (Path(child.header.workspace) / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    network = await sandbox.execute(ExecutionRequest(
        workspace=child.header.workspace, workspace_read_only=True,
        command="python3 -c 'import socket; socket.create_connection((\"1.1.1.1\", 443), timeout=2)'",
    ))
    assert network.exit_code != 0
