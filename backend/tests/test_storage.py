from pathlib import Path

import pytest

from traceforge.models import SessionEntry, SessionHeader
from traceforge.storage import JsonlSession


@pytest.mark.asyncio
async def test_jsonl_tree_branches_without_rewriting_history(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    session = JsonlSession.create(
        path,
        SessionHeader(id="session_test", workspace_id="workspace_test", workspace=str(tmp_path)),
    )
    root = await session.append("user_message", {"content": "root"})
    first = await session.append("assistant_message", {"content": "first"})
    old_bytes = path.read_bytes()
    branch = await session.append("branch_switch", {"from_id": first.id}, parent_id=root.id)
    await session.append("user_message", {"content": "second branch"})

    assert path.read_bytes().startswith(old_bytes)
    assert session.get_branch()[-2].id == branch.id
    assert len(session.get_tree()[0]["children"]) == 2


@pytest.mark.asyncio
async def test_turn_tree_groups_full_agent_run_and_preserves_branch_targets(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    session = JsonlSession.create(
        path,
        SessionHeader(id="session_test", workspace_id="workspace_test", workspace=str(tmp_path)),
    )
    first = await session.append("user_message", {"content": "检查代码"}, run_id="run-1")
    await session.append("run_state", {"status": "executing"}, run_id="run-1")
    await session.append("tool_call", {"call_id": "call-1", "name": "read_file", "arguments": {}}, run_id="run-1")
    await session.append("tool_result", {"call_id": "call-1", "output": "source"}, run_id="run-1")
    await session.append("assistant_message", {"content": "检查完成"}, run_id="run-1")
    first_end = await session.append("run_state", {"status": "completed"}, run_id="run-1")
    second = await session.append("user_message", {"content": "继续实现"}, run_id="run-2")
    await session.append("assistant_message", {"content": "实现完成"}, run_id="run-2")
    await session.append("run_state", {"status": "completed"}, run_id="run-2")
    await session.append("branch_switch", {"target_id": first_end.id}, parent_id=first_end.id)
    third = await session.append("user_message", {"content": "换个方案"}, run_id="run-3")

    turn_tree = session.get_turn_tree()
    assert len(turn_tree) == 1
    assert turn_tree[0]["entry"]["id"] == first.id
    assert turn_tree[0]["target_entry_id"] == first_end.id
    assert turn_tree[0]["tool_count"] == 1
    assert turn_tree[0]["assistant_preview"] == "检查完成"
    assert [node["entry"]["id"] for node in turn_tree[0]["children"]] == [second.id, third.id]
    assert len(session.entries) == 11
    assert path.read_text(encoding="utf-8").count('"type":"tool_call"') == 1


@pytest.mark.asyncio
async def test_turn_tree_hides_run_preludes_and_keeps_previous_terminal_status(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "session.jsonl",
        SessionHeader(id="session_test", workspace_id="workspace_test", workspace=str(tmp_path)),
    )
    await session.append("run_state", {"status": "briefing"}, run_id="run-1")
    first = await session.append("user_message", {"content": "first"}, run_id="run-1")
    first_end = await session.append("run_state", {"status": "completed"}, run_id="run-1")
    await session.append("run_state", {"status": "briefing"}, run_id="run-2")
    second = await session.append("user_message", {"content": "second"}, run_id="run-2")
    await session.append("run_state", {"status": "completed"}, run_id="run-2")

    roots = session.get_turn_tree()
    assert len(roots) == 1
    assert roots[0]["entry"]["id"] == first.id
    assert roots[0]["target_entry_id"] == first_end.id
    assert roots[0]["status"] == "completed"
    assert roots[0]["event_count"] == 2
    assert [node["entry"]["id"] for node in roots[0]["children"]] == [second.id]


@pytest.mark.asyncio
async def test_clarification_answers_are_separate_conversation_turns(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "session.jsonl",
        SessionHeader(id="session_test", workspace_id="workspace_test", workspace=str(tmp_path)),
    )
    await session.append("user_message", {"content": "部署服务"}, run_id="run-1")
    await session.append("clarification_question", {"question": "平台？"}, run_id="run-1")
    await session.append("run_state", {"status": "completed"}, run_id="run-1")
    first_answer = await session.append("clarification_answer", {"answer": "Linux"})
    second_answer = await session.append("clarification_answer", {"answer": "Python 3.12"})
    completed = await session.append("run_state", {"status": "completed"}, run_id="run-2")

    root = session.get_turn_tree()[0]
    assert root["question"] == "平台？"
    assert root["children"][0]["entry"]["id"] == first_answer.id
    assert root["children"][0]["target_entry_id"] == first_answer.id
    assert root["children"][0]["children"][0]["entry"]["id"] == second_answer.id
    assert root["children"][0]["children"][0]["target_entry_id"] == completed.id


@pytest.mark.asyncio
async def test_recovery_adds_synthetic_result_for_interrupted_tool(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    session = JsonlSession.create(
        path,
        SessionHeader(id="session_test", workspace_id="workspace_test", workspace=str(tmp_path)),
    )
    await session.append("tool_call", {"call_id": "call-1", "name": "read_file", "arguments": {}})
    recovered = await session.recover_unmatched_tool_calls()
    assert recovered[0].payload["synthetic"] is True
    assert session.validate_tool_pairs() == []


def test_loader_ignores_partial_final_line(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    session = JsonlSession.create(
        path,
        SessionHeader(id="session_test", workspace_id="workspace_test", workspace=str(tmp_path)),
    )
    with path.open("ab") as handle:
        handle.write(b'{"type":"broken"')
    loaded = JsonlSession.load(path)
    assert loaded.header.id == session.header.id
    assert loaded.entries == []
    assert loaded.recovery_issues


@pytest.mark.asyncio
async def test_append_after_partial_line_remains_recoverable(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    JsonlSession.create(
        path,
        SessionHeader(id="session_test", workspace_id="workspace_test", workspace=str(tmp_path)),
    )
    with path.open("ab") as handle:
        handle.write(b'{"type":"broken"')
    loaded = JsonlSession.load(path)
    appended = await loaded.append("user_message", {"content": "survives"})
    reloaded = JsonlSession.load(path)
    assert reloaded.by_id[appended.id].payload["content"] == "survives"


def test_duplicate_ids_and_orphans_are_preserved_as_recovery_nodes(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    JsonlSession.create(
        path,
        SessionHeader(id="session_test", workspace_id="workspace_test", workspace=str(tmp_path)),
    )
    duplicate = SessionEntry(type="user_message", id="same", seq=1, payload={"content": "first"})
    second = SessionEntry(type="assistant_message", id="same", seq=2, payload={"content": "second"})
    orphan = SessionEntry(type="assistant_message", id="orphan", parent_id="missing", seq=3)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(duplicate.model_dump_json() + "\n")
        handle.write(second.model_dump_json() + "\n")
        handle.write(orphan.model_dump_json() + "\n")
    loaded = JsonlSession.load(path)
    assert len(loaded.entries) == 3
    assert len({entry.id for entry in loaded.entries}) == 3
    assert any("重复节点" in issue for issue in loaded.recovery_issues)
    assert any(node["orphaned"] for node in loaded.get_tree())
    assert any(node["orphaned"] for node in loaded.get_turn_tree())


def test_corrupt_parent_cycle_is_shown_as_recovery_roots(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    JsonlSession.create(
        path,
        SessionHeader(id="session_test", workspace_id="workspace_test", workspace=str(tmp_path)),
    )
    first = SessionEntry(type="user_message", id="first", parent_id="second", seq=1)
    second = SessionEntry(type="assistant_message", id="second", parent_id="first", seq=2)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(first.model_dump_json() + "\n")
        handle.write(second.model_dump_json() + "\n")

    loaded = JsonlSession.load(path)
    tree = loaded.get_tree()
    assert len(tree) == 1
    assert tree[0]["entry"]["id"] == "first"
    assert tree[0]["orphaned"] is True
    assert tree[0]["children"][0]["entry"]["id"] == "second"
    assert [entry.id for entry in loaded.get_branch()] == ["first", "second"]


@pytest.mark.asyncio
async def test_recovery_closes_pending_approval(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    session = JsonlSession.create(
        path,
        SessionHeader(id="session_test", workspace_id="workspace_test", workspace=str(tmp_path)),
    )
    await session.append(
        "approval_request",
        {"approval_id": "approval-1", "policy": {"fingerprint": "fp"}},
    )
    recovered = await session.recover_pending_approvals()
    assert recovered[0].payload["decision"] == "cancelled"
    assert recovered[0].payload["synthetic"] is True


@pytest.mark.asyncio
async def test_recovery_marks_interrupted_run_failed_without_changing_completed_runs(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    session = JsonlSession.create(
        path,
        SessionHeader(id="session_test", workspace_id="workspace_test", workspace=str(tmp_path)),
    )
    await session.append("run_state", {"status": "executing"}, run_id="run-1")
    recovered = await session.recover_interrupted_run()
    assert recovered is not None
    assert recovered.payload["status"] == "failed"
    assert recovered.payload["synthetic"] is True
    assert recovered.run_id == "run-1"
    assert await session.recover_interrupted_run() is None
