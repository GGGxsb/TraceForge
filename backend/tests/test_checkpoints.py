from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
import subprocess

import httpx
import pytest

from traceforge.checkpoints import CheckpointError, CheckpointStore
from traceforge.agent import ActiveRun
from traceforge.config import Settings
from traceforge.main import create_app
from traceforge.models import RunStatus, WorkspaceRecord
from traceforge.storage import SessionStore
from traceforge.worktrees import WorktreeManager


def _git(workspace: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(workspace), *args], capture_output=True, text=True, check=True)
    return result.stdout


def test_byte_exact_restore_and_preview(tmp_path: Path):
    workspace = tmp_path / "code"
    workspace.mkdir()
    (workspace / "src").mkdir()
    original = b"\x00\xffsource\r\n"
    (workspace / "src" / "main.bin").write_bytes(original)
    (workspace / ".env").write_text("keep secret", encoding="utf-8")
    (workspace / "dist").mkdir()
    (workspace / "dist" / "output.txt").write_text("generated", encoding="utf-8")
    session = SessionStore(tmp_path / "sessions").create(WorkspaceRecord(
        path=str(workspace), name="code", kind="directory",
    ))
    store = CheckpointStore(tmp_path / "checkpoints")
    first = store.capture(session)
    (workspace / "src" / "main.bin").write_bytes(b"changed")
    (workspace / "new.txt").write_text("new", encoding="utf-8")
    (workspace / ".env").write_text("new secret", encoding="utf-8")
    (workspace / "dist" / "output.txt").write_text("new generated", encoding="utf-8")
    second = store.capture(session)
    assert first["fingerprint"] != second["fingerprint"]

    # The source entry can be any point whose ancestry contains the checkpoint.
    async def record():
        checkpoint = await session.append("workspace_checkpoint", first)
        return await session.append("run_state", {"status": "completed"}, parent_id=checkpoint.id)
    target = asyncio.run(record())
    preview = store.preview(session, target.id)
    assert {(item["path"], item["action"]) for item in preview["changes"]} == {
        ("src/main.bin", "modify"), ("new.txt", "delete"),
    }
    with pytest.raises(CheckpointError, match="预览后"):
        store.restore(session, str(first["checkpoint_id"]), expected_current="stale")
    assert (workspace / "src" / "main.bin").read_bytes() == b"changed"
    blob = tmp_path / "checkpoints" / session.header.id / "blobs" / hashlib.sha256(original).hexdigest()
    blob_bytes = blob.read_bytes()
    blob.unlink()
    with pytest.raises(CheckpointError, match="missing or damaged"):
        store.restore(session, str(first["checkpoint_id"]), expected_current=preview["current_fingerprint"])
    assert (workspace / "src" / "main.bin").read_bytes() == b"changed"
    blob.write_bytes(blob_bytes)
    store.restore(session, str(first["checkpoint_id"]), expected_current=preview["current_fingerprint"])
    assert (workspace / "src" / "main.bin").read_bytes() == original
    assert not (workspace / "new.txt").exists()
    assert (workspace / ".env").read_text(encoding="utf-8") == "new secret"
    assert (workspace / "dist" / "output.txt").read_text(encoding="utf-8") == "new generated"


def test_git_staging_is_restored_but_changed_head_blocks_restore(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init")
    source = workspace / "main.py"
    source.write_text("base\n", encoding="utf-8")
    _git(workspace, "add", "main.py")
    _git(workspace, "-c", "user.name=TraceForge", "-c", "user.email=traceforge@example.invalid",
         "commit", "-m", "base")
    session = SessionStore(tmp_path / "sessions").create(WorkspaceRecord(
        path=str(workspace), name="repo", kind="git",
    ))
    store = CheckpointStore(tmp_path / "checkpoints")
    base = store.capture(session)
    source.write_text("staged\n", encoding="utf-8")
    _git(workspace, "add", "main.py")
    staged = store.capture(session)
    store.restore(session, str(base["checkpoint_id"]))
    assert source.read_text(encoding="utf-8") == "base\n"
    assert _git(workspace, "diff", "--cached") == ""
    store.restore(session, str(staged["checkpoint_id"]))
    assert source.read_text(encoding="utf-8") == "staged\n"
    assert "+staged" in _git(workspace, "diff", "--cached")
    _git(workspace, "-c", "user.name=TraceForge", "-c", "user.email=traceforge@example.invalid",
         "commit", "-m", "next")
    with pytest.raises(CheckpointError, match="Git HEAD"):
        store.restore(session, str(base["checkpoint_id"]))
    entry = asyncio.run(session.append("workspace_checkpoint", base))
    assert store.preview(session, entry.id)["available"] is False


def test_restore_handles_file_directory_swaps_without_deleting_unmanaged_files(tmp_path: Path):
    workspace = tmp_path / "code"
    workspace.mkdir()
    path = workspace / "module"
    path.write_text("single file\n", encoding="utf-8")
    session = SessionStore(tmp_path / "sessions").create(WorkspaceRecord(
        path=str(workspace), name="code", kind="directory",
    ))
    store = CheckpointStore(tmp_path / "checkpoints")
    single_file = store.capture(session)
    path.unlink()
    path.mkdir()
    (path / "part.py").write_text("nested file\n", encoding="utf-8")
    directory = store.capture(session)

    store.restore(session, str(single_file["checkpoint_id"]))
    assert path.read_text(encoding="utf-8") == "single file\n"
    store.restore(session, str(directory["checkpoint_id"]))
    assert (path / "part.py").read_text(encoding="utf-8") == "nested file\n"

    (path / ".env").write_text("preserve me", encoding="utf-8")
    with pytest.raises(CheckpointError, match="Unmanaged file blocks restore"):
        store.restore(session, str(single_file["checkpoint_id"]))
    assert (path / ".env").read_text(encoding="utf-8") == "preserve me"
    assert (path / "part.py").read_text(encoding="utf-8") == "nested file\n"


def test_managed_worktrees_stay_outside_repository_when_data_dir_is_nested(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    sessions = SessionStore(workspace / ".traceforge-data" / "sessions")
    session = sessions.create(WorkspaceRecord(path=str(workspace), name="repo", kind="git"))
    manager = WorktreeManager(workspace / ".traceforge-data" / "worktrees")
    managed_path = Path(manager.allocate(session))
    assert not managed_path.is_relative_to(workspace)


@pytest.mark.asyncio
async def test_branch_switch_restores_files_and_manual_rollback_keeps_dialogue(tmp_path: Path):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    services = app.state.services
    workspace = tmp_path / "repo"
    workspace.mkdir()
    code = workspace / "main.py"
    code.write_text("root\n", encoding="utf-8")
    session = services.sessions.create(services.workspaces.register(str(workspace)))
    await session.append("user_message", {"content": "root"})
    root_snapshot = services.checkpoints.capture(session)
    await session.append("workspace_checkpoint", root_snapshot)
    root_end = await session.append("run_state", {"status": "completed"})
    code.write_text("first branch\n", encoding="utf-8")
    (workspace / "first.txt").write_text("first", encoding="utf-8")
    await session.append("user_message", {"content": "first"})
    first_snapshot = services.checkpoints.capture(session)
    await session.append("workspace_checkpoint", first_snapshot)
    await session.append("run_state", {"status": "completed"})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        preview = (await client.get(f"/api/sessions/{session.header.id}/branch-preview",
                                    params={"target_entry_id": root_end.id})).json()
        assert preview["available"] is True
        assert len(preview["changes"]) == 2
        switched = await client.post(f"/api/sessions/{session.header.id}/branch", json={
            "target_entry_id": root_end.id, "include_summary": False,
            "expected_current": preview["current_fingerprint"],
        })
        assert switched.status_code == 200, switched.text
        assert code.read_text(encoding="utf-8") == "root\n"
        assert not (workspace / "first.txt").exists()
        leaving = next(item for item in reversed(session.entries) if item.type == "branch_switch")
        first_tip = leaving.payload["from_id"]
        assert session.by_id[first_tip].type == "workspace_checkpoint"
        code.write_text("second branch\n", encoding="utf-8")
        await session.append("user_message", {"content": "second"})
        second_snapshot = services.checkpoints.capture(session)
        await session.append("workspace_checkpoint", second_snapshot)
        second_end = await session.append("run_state", {"status": "completed"})
        returned = await client.post(f"/api/sessions/{session.header.id}/branch", json={
            "target_entry_id": first_tip, "include_summary": False, "mode": "resume",
        })
        assert returned.status_code == 200, returned.text
        assert code.read_text(encoding="utf-8") == "first branch\n"
        assert (workspace / "first.txt").read_text(encoding="utf-8") == "first"
        rollback = await client.post(f"/api/sessions/{session.header.id}/rollback", json={
            "target_entry_id": second_end.id,
        })
        assert rollback.status_code == 200, rollback.text
        assert code.read_text(encoding="utf-8") == "second branch\n"
        assert not (workspace / "first.txt").exists()
        assert rollback.json()["type"] == "workspace_restore"
        assert services.checkpoints.checkpoint_for(session, session.active_leaf_id) == second_snapshot["checkpoint_id"]


@pytest.mark.asyncio
async def test_failed_branch_switch_restores_source_code_and_active_branch(tmp_path: Path, monkeypatch):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    services = app.state.services
    workspace = tmp_path / "repo"
    workspace.mkdir()
    code = workspace / "main.py"
    code.write_text("target\n", encoding="utf-8")
    session = services.sessions.create(services.workspaces.register(str(workspace)))
    await session.append("user_message", {"content": "target"})
    await session.append("workspace_checkpoint", services.checkpoints.capture(session))
    target = await session.append("run_state", {"status": "completed"})
    code.write_text("source\n", encoding="utf-8")
    await session.append("user_message", {"content": "source"})
    source_checkpoint = services.checkpoints.capture(session)
    await session.append("workspace_checkpoint", source_checkpoint)
    source_end = await session.append("run_state", {"status": "completed"})

    original_append = session.append
    failed = False

    async def fail_once_after_context_switch(entry_type, *args, **kwargs):
        nonlocal failed
        if entry_type == "workspace_restore" and not failed:
            failed = True
            raise OSError("simulated restore record write failure")
        return await original_append(entry_type, *args, **kwargs)

    monkeypatch.setattr(session, "append", fail_once_after_context_switch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/sessions/{session.header.id}/branch", json={
            "target_entry_id": target.id, "include_summary": False,
        })
        assert response.status_code == 409, response.text
        assert failed
        assert code.read_text(encoding="utf-8") == "source\n"
        assert session.get_branch()[-1].payload["reason"] == "branch_switch_failed"
        assert services.checkpoints.status(session)["state"] == "aligned"
        assert source_end.id in {entry.id for entry in session.get_branch()}


@pytest.mark.asyncio
async def test_git_fork_uses_independent_worktree_and_can_resume_both_sides(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init")
    original = workspace / "main.py"
    original.write_text("base\n", encoding="utf-8")
    _git(workspace, "add", "main.py")
    _git(workspace, "-c", "user.name=TraceForge", "-c", "user.email=traceforge@example.invalid",
         "commit", "-m", "base")
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    services = app.state.services
    session = services.sessions.create(services.workspaces.register(str(workspace)))
    await session.append("user_message", {"content": "base"})
    await session.append("workspace_checkpoint", services.checkpoints.capture(session))
    base_tip = await session.append("run_state", {"status": "completed"})
    original.write_text("source branch\n", encoding="utf-8")
    await session.append("user_message", {"content": "source"})
    await session.append("workspace_checkpoint", services.checkpoints.capture(session))
    await session.append("run_state", {"status": "completed"})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        fork = await client.post(f"/api/sessions/{session.header.id}/branch", json={
            "target_entry_id": base_tip.id, "include_summary": False, "mode": "fork",
        })
        assert fork.status_code == 200, fork.text
        fork_workspace = Path(session.workspace)
        assert fork_workspace != workspace
        assert (fork_workspace / "main.py").read_text(encoding="utf-8") == "base\n"
        assert original.read_text(encoding="utf-8") == "source branch\n"
        assert (await client.get(f"/api/sessions/{session.header.id}/file",
                                 params={"path": "main.py"})).text == "base\n"
        (fork_workspace / "main.py").write_text("fork branch\n", encoding="utf-8")
        _git(fork_workspace, "add", "main.py")
        _git(fork_workspace, "-c", "user.name=TraceForge", "-c", "user.email=traceforge@example.invalid",
             "commit", "-m", "fork commit")
        await session.append("user_message", {"content": "fork"})
        await session.append("workspace_checkpoint", services.checkpoints.capture(session))
        fork_tip = await session.append("run_state", {"status": "completed"})
        source_tip = next(entry.payload["from_id"] for entry in session.entries
                          if entry.type == "branch_switch" and entry.payload["target_id"] == base_tip.id)
        cross_commit = (await client.get(f"/api/sessions/{session.header.id}/branch-preview",
                                         params={"target_entry_id": source_tip})).json()
        assert cross_commit["available"] is True
        assert cross_commit["git_head_change"]
        rollback_preview = (await client.get(f"/api/sessions/{session.header.id}/branch-preview",
                                            params={"target_entry_id": source_tip,
                                                    "operation": "rollback"})).json()
        assert rollback_preview["available"] is False
        back = await client.post(f"/api/sessions/{session.header.id}/branch", json={
            "target_entry_id": source_tip, "include_summary": False, "mode": "resume",
        })
        assert back.status_code == 200, back.text
        assert Path(session.workspace) == workspace
        assert original.read_text(encoding="utf-8") == "source branch\n"
        again = await client.post(f"/api/sessions/{session.header.id}/branch", json={
            "target_entry_id": fork_tip.id, "include_summary": False, "mode": "resume",
        })
        assert again.status_code == 200, again.text
        assert Path(session.workspace) == fork_workspace
        assert (fork_workspace / "main.py").read_text(encoding="utf-8") == "fork branch\n"
        reopened = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data",
                                      model_config_dir=tmp_path / "config"))
        assert Path(reopened.state.services.sessions.get(session.header.id).workspace) == fork_workspace
        fork_head = _git(fork_workspace, "rev-parse", "HEAD").strip()
        (fork_workspace / "main.py").write_text("uncommitted\n", encoding="utf-8")
        blocked = await client.delete(f"/api/sessions/{session.header.id}")
        assert blocked.status_code == 400
        assert fork_workspace.exists()
        (fork_workspace / "main.py").write_text("fork branch\n", encoding="utf-8")
        removed = await client.delete(f"/api/sessions/{session.header.id}")
        assert removed.status_code == 200, removed.text
        assert not fork_workspace.exists()
        preserved = removed.json()["preserved_branches"]
        assert len(preserved) == 1
        assert _git(workspace, "rev-parse", f"refs/heads/{preserved[0]}").strip() == fork_head


@pytest.mark.asyncio
async def test_legacy_branch_without_checkpoint_is_rejected(tmp_path: Path):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    services = app.state.services
    workspace = tmp_path / "repo"
    workspace.mkdir()
    session = services.sessions.create(services.workspaces.register(str(workspace)))
    old = await session.append("user_message", {"content": "old"})
    await session.append("user_message", {"content": "new"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/sessions/{session.header.id}/branch", json={
            "target_entry_id": old.id, "include_summary": False,
        })
        assert response.status_code == 409
        assert "没有代码检查点" in response.text
    assert session.active_leaf_id != old.id


@pytest.mark.asyncio
async def test_incomplete_restore_is_recovered_after_restart(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    code = workspace / "main.py"
    code.write_text("before\n", encoding="utf-8")
    sessions = SessionStore(tmp_path / "sessions")
    session = sessions.create(WorkspaceRecord(path=str(workspace), name="repo", kind="directory"))
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    before = checkpoints.capture(session)
    await session.append("workspace_checkpoint", before)
    code.write_text("target\n", encoding="utf-8")
    target = checkpoints.capture(session)
    await session.append("workspace_restore_begin", {
        "transaction_id": "restore_crashed", "from_checkpoint_id": before["checkpoint_id"],
        "to_checkpoint_id": target["checkpoint_id"], "reason": "manual_rollback",
    })
    code.write_text("partial restore\n", encoding="utf-8")
    reopened = SessionStore(tmp_path / "sessions")
    await reopened.recover_all(checkpoints)
    recovered = reopened.get(session.header.id)
    assert code.read_text(encoding="utf-8") == "before\n"
    assert any(entry.type == "workspace_restore" and entry.payload.get("reason") == "server_recovery"
               for entry in recovered.entries)
    assert any(entry.type == "workspace_checkpoint" and entry.payload.get("reason") == "before_restore_recovery"
               for entry in recovered.entries)


@pytest.mark.asyncio
async def test_recovery_never_removes_existing_worktree_on_resume(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init")
    (workspace / "main.py").write_text("base\n", encoding="utf-8")
    _git(workspace, "add", "main.py")
    _git(workspace, "-c", "user.name=TraceForge", "-c", "user.email=traceforge@example.invalid",
         "commit", "-m", "base")
    settings = replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config")
    services = create_app(settings).state.services
    session = services.sessions.create(services.workspaces.register(str(workspace)))
    checkpoint = services.checkpoints.capture(session)
    root = await session.append("workspace_checkpoint", checkpoint)
    fork_path = services.worktrees.create(session, _git(workspace, "rev-parse", "HEAD").strip())
    await session.append("workspace_binding", {"path": fork_path,
                                                "checkpoint_id": checkpoint["checkpoint_id"]})
    await session.append("branch_resume", {"mode": "resume"}, parent_id=root.id)
    assert Path(session.workspace) == workspace
    await session.append("workspace_restore_begin", {
        "transaction_id": "resume_crashed", "from_checkpoint_id": checkpoint["checkpoint_id"],
        "to_checkpoint_id": checkpoint["checkpoint_id"], "target_entry_id": root.id,
        "reason": "branch_switch", "from_workspace": str(workspace),
        "to_workspace": fork_path, "isolated": True, "created_worktree": False,
    }, run_id="resume_crash")
    reopened = SessionStore(settings.data_dir / "sessions")
    await reopened.recover_all(services.checkpoints, services.worktrees)
    assert Path(fork_path).exists()
    assert (Path(fork_path) / "main.py").read_text(encoding="utf-8") == "base\n"
    recovered = reopened.get(session.header.id)
    assert any(entry.type == "workspace_restore" and entry.payload.get("transaction_id") == "resume_crashed"
               for entry in recovered.entries)


@pytest.mark.asyncio
async def test_terminal_run_records_checkpoint_before_final_state(tmp_path: Path):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    services = app.state.services
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "main.py").write_text("updated\n", encoding="utf-8")
    session = services.sessions.create(services.workspaces.register(str(workspace)))
    await session.append("user_message", {"content": "edit"})
    run = ActiveRun(id="run-checkpoint", session_id=session.header.id, status=RunStatus.EXECUTING)
    await services.runner._set_status(run, session, RunStatus.COMPLETED)
    assert [entry.type for entry in session.entries[-2:]] == ["workspace_checkpoint", "run_state"]
    assert services.checkpoints.checkpoint_for(session, session.active_leaf_id) == session.entries[-2].payload["checkpoint_id"]


@pytest.mark.asyncio
async def test_other_session_changes_require_restore_or_explicit_adoption(tmp_path: Path):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    services = app.state.services
    workspace = tmp_path / "repo"
    workspace.mkdir()
    code = workspace / "main.py"
    code.write_text("session A\n", encoding="utf-8")
    record = services.workspaces.register(str(workspace))
    first = services.sessions.create(record)
    await first.append("user_message", {"content": "first"})
    snapshot = services.checkpoints.capture(first)
    await first.append("workspace_checkpoint", snapshot)
    first_end = await first.append("run_state", {"status": "completed"})
    code.write_text("session B\n", encoding="utf-8")
    second = services.sessions.create(record)
    await second.append("user_message", {"content": "second"})
    await second.append("workspace_checkpoint", services.checkpoints.capture(second))
    await second.append("run_state", {"status": "completed"})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        status = (await client.get(f"/api/sessions/{first.header.id}/workspace-status")).json()
        assert status["state"] == "diverged"
        assert status["restorable"] is True
        assert (await client.post(f"/api/sessions/{first.header.id}/runs",
                                  json={"content": "continue"})).status_code == 409
        assert (await client.post(f"/api/sessions/{first.header.id}/branch", json={
            "target_entry_id": first_end.id, "include_summary": False,
        })).status_code == 409
        stale = await client.post(f"/api/sessions/{first.header.id}/adopt-workspace", json={
            "expected_current": "stale",
        })
        assert stale.status_code == 409
        adopted = await client.post(f"/api/sessions/{first.header.id}/adopt-workspace", json={
            "expected_current": status["current_fingerprint"],
        })
        assert adopted.status_code == 200, adopted.text
        assert adopted.json()["payload"]["reason"] == "adopt_workspace"
        assert (await client.get(f"/api/sessions/{first.header.id}/workspace-status")).json()["state"] == "aligned"
        assert code.read_text(encoding="utf-8") == "session B\n"
