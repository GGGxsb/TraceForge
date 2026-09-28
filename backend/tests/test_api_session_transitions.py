from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from traceforge.agent import ActiveRun
from traceforge.config import Settings
from traceforge.main import create_app
from traceforge.models import BranchSummary, PermissionMode, RunStatus

from .fakes import FakeModelAdapter


@pytest.mark.asyncio
async def test_permission_mode_is_persisted_and_cannot_change_during_run(tmp_path: Path):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    services = app.state.services
    repo = tmp_path / "repo"
    repo.mkdir()
    session = services.sessions.create(services.workspaces.register(str(repo)))
    session_id = session.header.id
    url = f"/api/sessions/{session_id}/permission-mode"
    task = asyncio.create_task(asyncio.Event().wait())
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get(f"/api/sessions/{session_id}")).json()["permission_mode"] == "request_approval"
            assert (await client.patch(url, json={"mode": "full_access"})).status_code == 403
            assert (await client.patch(url, headers={"X-TraceForge-UI": "1"}, json={"mode": "invalid"})).status_code == 422
            response = await client.patch(url, headers={"X-TraceForge-UI": "1"}, json={"mode": "full_access"})
            assert response.status_code == 200
            assert response.json()["mode"] == "full_access"
            assert session.permission_mode == PermissionMode.FULL_ACCESS
            assert (await client.get("/api/sessions")).json()[0]["permission_mode"] == "full_access"
            services.sessions._sessions.pop(session_id)
            assert services.sessions.get(session_id).permission_mode == PermissionMode.FULL_ACCESS
            services.runner.runs["active"] = ActiveRun(
                id="active", session_id=session_id, status=RunStatus.EXECUTING, task=task,
            )
            assert (await client.patch(url, headers={"X-TraceForge-UI": "1"},
                                       json={"mode": "auto_approve"})).status_code == 409
            assert services.sessions.get(session_id).permission_mode == PermissionMode.FULL_ACCESS
    finally:
        services.runner.runs.pop("active", None)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_archive_restore_and_delete_session_with_artifacts(tmp_path: Path):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    services = app.state.services
    repo = tmp_path / "repo"
    repo.mkdir()
    session = services.sessions.create(services.workspaces.register(str(repo)))
    session_id = session.header.id
    await session.append("user_message", {"content": "检查"})
    await session.append("tool_call", {"call_id": "call-1", "name": "read_file", "arguments": {"path": "a.py"}})
    await session.append("tool_result", {"call_id": "call-1", "output": "ok"})
    artifact_dir = services.artifacts_root / session_id
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "log.txt").write_text("output", encoding="utf-8")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        archived = await client.patch(f"/api/sessions/{session_id}/archive", json={"archived": True})
        assert archived.status_code == 200
        assert archived.json()["archived"] is True
        listing = (await client.get("/api/sessions")).json()
        assert listing[0]["turn_count"] == 1
        assert listing[0]["entry_count"] == 4
        assert listing[0]["archived"] is True
        assert (await client.post(f"/api/sessions/{session_id}/runs", json={"content": "继续"})).status_code == 409
        services.sessions._sessions.pop(session_id)
        assert services.sessions.get(session_id).archived is True
        restored = await client.patch(f"/api/sessions/{session_id}/archive", json={"archived": False})
        assert restored.json()["archived"] is False
        deleted = await client.delete(f"/api/sessions/{session_id}")
        assert deleted.status_code == 200
        assert (await client.get(f"/api/sessions/{session_id}")).status_code == 404
    assert not session.path.exists()
    assert not artifact_dir.exists()


@pytest.mark.asyncio
async def test_active_session_cannot_be_archived_or_deleted(tmp_path: Path):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    services = app.state.services
    repo = tmp_path / "repo"
    repo.mkdir()
    session = services.sessions.create(services.workspaces.register(str(repo)))
    task = asyncio.create_task(asyncio.Event().wait())
    services.runner.runs["active"] = ActiveRun(
        id="active", session_id=session.header.id, status=RunStatus.EXECUTING, task=task,
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.patch(
                f"/api/sessions/{session.header.id}/archive", json={"archived": True},
            )).status_code == 409
            assert (await client.delete(f"/api/sessions/{session.header.id}")).status_code == 409
        assert session.path.exists()
        assert session.archived is False
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_tree_api_returns_turn_nodes_but_session_keeps_raw_entries(tmp_path: Path):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    services = app.state.services
    repo = tmp_path / "repo"
    repo.mkdir()
    session = services.sessions.create(services.workspaces.register(str(repo)))
    first = await session.append("user_message", {"content": "检查"}, run_id="run-1")
    await session.append("tool_call", {"call_id": "call-1", "name": "read_file", "arguments": {}}, run_id="run-1")
    await session.append("tool_result", {"call_id": "call-1", "output": "ok"}, run_id="run-1")
    await session.append("assistant_message", {"content": "完成"}, run_id="run-1")
    terminal = await session.append("run_state", {"status": "completed"}, run_id="run-1")
    await session.append("user_message", {"content": "继续"}, run_id="run-2")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        tree = (await client.get(f"/api/sessions/{session.header.id}/tree")).json()
        raw_tree = (await client.get(f"/api/sessions/{session.header.id}/tree?granularity=entry")).json()
        detail = (await client.get(f"/api/sessions/{session.header.id}")).json()
    assert len(tree) == 1
    assert tree[0]["entry"]["id"] == first.id
    assert tree[0]["target_entry_id"] == terminal.id
    assert len(tree[0]["children"]) == 1
    assert tree[0]["event_count"] == 5
    assert raw_tree[0]["children"][0]["entry"]["type"] == "tool_call"
    assert len(detail["entries"]) == 6


@pytest.mark.asyncio
async def test_branch_api_can_resume_a_saved_tip_without_summary(tmp_path: Path):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    services = app.state.services
    repo = tmp_path / "repo"
    repo.mkdir()
    session = services.sessions.create(services.workspaces.register(str(repo)))
    await session.append("user_message", {"content": "root"})
    root_checkpoint = services.checkpoints.capture(session)
    await session.append("workspace_checkpoint", root_checkpoint)
    root_end = await session.append("run_state", {"status": "completed"})
    first = await session.append("user_message", {"content": "first branch"})
    first_checkpoint = services.checkpoints.capture(session)
    await session.append("workspace_checkpoint", first_checkpoint)
    first_end = await session.append("run_state", {"status": "completed"})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        fork = await client.post(f"/api/sessions/{session.header.id}/branch", json={
            "target_entry_id": root_end.id, "include_summary": False, "mode": "fork",
        })
        assert fork.status_code == 200
        assert fork.json()["type"] == "branch_resume"
        second = await session.append("user_message", {"content": "second branch"})
        second_checkpoint = services.checkpoints.capture(session)
        await session.append("workspace_checkpoint", second_checkpoint)
        second_end = await session.append("run_state", {"status": "completed"})
        tree = (await client.get(f"/api/sessions/{session.header.id}/tree")).json()
        first_node = next(node for node in tree[0]["children"] if node["entry"]["id"] == first.id)
        assert session.by_id[first_node["branch_tips"][0]["entry_id"]].type == "workspace_checkpoint"

        resumed = await client.post(f"/api/sessions/{session.header.id}/branch", json={
            "target_entry_id": first_node["branch_tips"][0]["entry_id"],
            "include_summary": False, "mode": "resume",
        })
        assert resumed.status_code == 200
        assert resumed.json()["type"] == "branch_resume"
        assert resumed.json()["payload"]["summary_mode"] == "skipped"

    assert second_end.id in session.by_id
    assert not any(entry.type == "branch_summary" for entry in session.entries)
    assert first_end.id in {entry.id for entry in session.get_branch()}
    assert session.get_branch()[-1].type == "workspace_restore"


@pytest.mark.asyncio
async def test_run_waits_for_branch_switch_before_extending_active_tree(tmp_path: Path):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    services = app.state.services
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = services.workspaces.register(str(repo))
    session = services.sessions.create(workspace)
    checkpoint = services.checkpoints.capture(session)
    await session.append("workspace_checkpoint", checkpoint)
    root = await session.append("user_message", {"content": "first request"})
    await session.append("assistant_message", {"content": "first answer"})

    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowSummary:
        async def summarize_branch(self, transcript: str) -> BranchSummary:
            entered.set()
            await release.wait()
            return BranchSummary(branch_goal="first request", completed=["first answer"])

    services.branches.adapter = SlowSummary()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        branch_request = asyncio.create_task(
            client.post(f"/api/sessions/{session.header.id}/branch", json={"target_entry_id": root.id})
        )
        await asyncio.wait_for(entered.wait(), 2)
        run_request = asyncio.create_task(
            client.post(f"/api/sessions/{session.header.id}/runs", json={"content": "second request"})
        )
        await asyncio.sleep(0.05)
        assert not run_request.done()
        assert not any(entry.payload.get("content") == "second request" for entry in session.entries)

        release.set()
        branch_response, run_response = await asyncio.gather(branch_request, run_request)
        assert branch_response.status_code == 200
        assert run_response.status_code == 200
        await services.runner.runs[run_response.json()["run_id"]].task

    branch = session.get_branch()
    summary = next(entry for entry in branch if entry.type == "branch_summary")
    second_user = next(
        entry for entry in branch if entry.type == "user_message" and entry.payload["content"] == "second request"
    )
    assert summary.seq < second_user.seq
    assert session.find_lca(summary.id, second_user.id) == summary.id
    assert session.validate_tool_pairs() == []


@pytest.mark.asyncio
async def test_clarification_answer_does_not_append_during_active_run(tmp_path: Path):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    services = app.state.services
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = services.workspaces.register(str(repo))
    session = services.sessions.create(workspace)
    await session.append("clarification_question", {"question_id": "q-1", "question": "Which target?"})

    wait_forever = asyncio.Event()
    task = asyncio.create_task(wait_forever.wait())
    services.runner.runs["run-active"] = ActiveRun(
        id="run-active", session_id=session.header.id, status=RunStatus.EXECUTING, task=task
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/clarifications/q-1/answer",
                json={"session_id": session.header.id, "answer": "Windows"},
            )
        assert response.status_code == 409
        assert not any(entry.type == "clarification_answer" for entry in session.entries)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_clarifications_are_answered_individually_before_agent_resumes(tmp_path: Path):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    services = app.state.services
    services.model_settings.adapter.replace(FakeModelAdapter())
    repo = tmp_path / "repo"
    repo.mkdir()
    session = services.sessions.create(services.workspaces.register(str(repo)))
    request = await session.append("user_message", {"content": "实现部署脚本"})
    for question_id, question in [("q-platform", "目标平台？"), ("q-runtime", "运行时版本？")]:
        await session.append("clarification_question", {
            "question_id": question_id,
            "question": question,
            "source_message_id": request.id,
            "status": "pending",
        })

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/clarifications/q-platform/answer",
            json={"session_id": session.header.id, "answer": "Linux"},
        )
        assert first.status_code == 200
        assert first.json()["run_id"] is None
        assert first.json()["remaining"] == 1
        assert not services.runner.runs

        duplicate = await client.post(
            "/api/clarifications/q-platform/answer",
            json={"session_id": session.header.id, "answer": "Windows"},
        )
        assert duplicate.status_code == 409

        second = await client.post(
            "/api/clarifications/q-runtime/answer",
            json={"session_id": session.header.id, "answer": "Python 3.12"},
        )
        assert second.status_code == 200
        assert second.json()["remaining"] == 0
        await services.runner.runs[second.json()["run_id"]].task

    answers = [item for item in session.entries if item.type == "clarification_answer"]
    assert [(item.payload["question"], item.payload["answer"]) for item in answers] == [
        ("目标平台？", "Linux"), ("运行时版本？", "Python 3.12")
    ]
    assert [item.payload["content"] for item in session.entries if item.type == "user_message"] == ["实现部署脚本"]
    assert any(item.type == "assistant_message" for item in session.entries)


@pytest.mark.asyncio
async def test_terminal_status_does_not_release_session_until_task_exits(tmp_path: Path):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    services = app.state.services
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = services.workspaces.register(str(repo))
    session = services.sessions.create(workspace)
    root = await session.append("user_message", {"content": "existing request"})

    wait_forever = asyncio.Event()
    task = asyncio.create_task(wait_forever.wait())
    services.runner.runs["finishing-run"] = ActiveRun(
        id="finishing-run", session_id=session.header.id, status=RunStatus.COMPLETED, task=task
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/sessions/{session.header.id}/branch", json={"target_entry_id": root.id}
            )
        assert response.status_code == 409
        assert session.active_leaf_id == root.id
        assert not any(entry.type == "branch_switch" for entry in session.entries)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
