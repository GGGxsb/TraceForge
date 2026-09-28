from pathlib import Path

import pytest

from traceforge.context import BranchService, CompactionService, ContextProjector
from traceforge.models import SessionHeader
from traceforge.storage import JsonlSession

from .fakes import FakeModelAdapter


class FailingSummaryAdapter(FakeModelAdapter):
    async def summarize_branch(self, transcript):
        raise RuntimeError("summary unavailable")


class CapturingSummaryAdapter(FakeModelAdapter):
    def __init__(self):
        super().__init__()
        self.transcript = ""

    async def summarize_branch(self, transcript):
        self.transcript = transcript
        return await super().summarize_branch(transcript)


@pytest.mark.asyncio
async def test_clarification_answers_keep_their_questions_in_model_context(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    await session.append("user_message", {"content": "实现部署脚本"})
    await session.append("clarification_question", {"question_id": "q-platform", "question": "目标平台？"})
    await session.append("clarification_answer", {
        "question_id": "q-platform", "question": "目标平台？", "answer": "Linux"
    })
    projected = ContextProjector(128_000, 16_000).project(session)
    assert {"role": "user", "content": "针对「目标平台？」的回答：Linux"} in projected.input_items


@pytest.mark.asyncio
async def test_compaction_projects_summary_and_recent_raw_entries(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    first = await session.append("user_message", {"content": "old " * 200})
    await session.append("assistant_message", {"content": "old answer " * 200})
    recent = await session.append("user_message", {"content": "recent"})
    await session.append("assistant_message", {"content": "recent answer"})
    compactor = CompactionService(FakeModelAdapter(), 200, 50, 50)
    compacted = await compactor.compact(session)
    assert compacted is not None
    projected = ContextProjector(200, 50).project(session)
    assert any(item.get("role") == "developer" for item in projected.input_items)
    assert any(item.get("content") == "recent" for item in projected.input_items)
    assert first.id in [entry.id for entry in session.entries]
    assert recent.id in [entry.id for entry in session.entries]


@pytest.mark.asyncio
async def test_branch_summary_uses_lca_and_preserves_old_branch(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    root = await session.append("user_message", {"content": "root"})
    old = await session.append("assistant_message", {"content": "branch work"})
    entry = await BranchService(FakeModelAdapter()).switch(session, root.id)
    assert entry.parent_id == root.id
    assert entry.payload["lca_id"] == root.id
    assert old.id in session.by_id
    assert entry.payload["source_entry_ids"] == [old.id]


@pytest.mark.asyncio
async def test_multiple_compactions_keep_raw_jsonl_and_valid_recent_tool_pair(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    early = "early evidence " * 300
    await session.append("user_message", {"content": early})
    await session.append("assistant_message", {"content": "first answer " * 300})
    await session.append("user_message", {"content": "middle request"})
    await session.append("assistant_message", {"content": "middle answer"})
    compactor = CompactionService(FakeModelAdapter(), 300, 50, 40)
    assert await compactor.compact(session) is not None

    await session.append("user_message", {"content": "recent request"})
    await session.append("tool_call", {"call_id": "call-recent", "name": "read_file", "arguments": {}})
    await session.append(
        "tool_result",
        {"call_id": "call-recent", "tool_name": "read_file", "output": "recent output"},
    )
    await session.append("assistant_message", {"content": "recent answer"})
    assert await compactor.compact(session) is not None

    assert session.path.read_text(encoding="utf-8").count('"type":"compaction"') == 2
    assert early in session.path.read_text(encoding="utf-8")
    projected = ContextProjector(300, 50).project(session)
    assert any(item.get("content") == "recent request" for item in projected.input_items)
    assert session.validate_tool_pairs() == []


@pytest.mark.asyncio
async def test_reasoning_item_is_replayed_before_tool_call_and_result(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    await session.append("user_message", {"content": "检查文件"})
    reasoning = {"id": "rs_1", "type": "reasoning", "content": [{"type": "reasoning_text", "text": "inspect"}]}
    await session.append("model_reasoning", {"item": reasoning})
    await session.append("tool_call", {"call_id": "call-1", "name": "read_file", "arguments": {"path": "a.py"}})
    await session.append("tool_result", {"call_id": "call-1", "tool_name": "read_file", "output": "ok"})

    projected = ContextProjector(128_000, 16_000).project(session)
    assert str(tmp_path) in projected.instructions
    assert projected.input_items[-3:] == [
        reasoning,
        {"type": "function_call", "call_id": "call-1", "name": "read_file", "arguments": '{"path": "a.py"}'},
        {"type": "function_call_output", "call_id": "call-1", "output": "ok"},
    ]


@pytest.mark.asyncio
async def test_can_return_to_an_old_branch_after_multiple_switches(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    root = await session.append("user_message", {"content": "root"})
    old_leaf = await session.append("assistant_message", {"content": "first branch"})
    first_switch = await BranchService(FakeModelAdapter()).switch(session, root.id)
    await session.append("user_message", {"content": "second branch"})
    await session.append("assistant_message", {"content": "second work"})
    returned = await BranchService(FakeModelAdapter()).switch(session, old_leaf.id)
    assert first_switch.id in session.by_id
    assert returned.parent_id == old_leaf.id
    assert returned.payload["lca_id"] == root.id
    assert session.active_leaf_id == returned.id


@pytest.mark.asyncio
async def test_turn_tree_switches_between_sibling_branches_and_projects_backfill(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    await session.append("user_message", {"content": "Build a parser"})
    root_end = await session.append("run_state", {"status": "completed"})
    first_turn = await session.append("user_message", {"content": "Use approach A"})
    await session.append("assistant_message", {"content": "Approach A finished"})
    first_end = await session.append("run_state", {"status": "completed"})

    branches = BranchService(FakeModelAdapter())
    await branches.switch(session, root_end.id)
    second_turn = await session.append("user_message", {"content": "Use approach B"})
    await session.append("assistant_message", {"content": "Approach B finished"})
    second_end = await session.append("run_state", {"status": "completed"})

    tree = session.get_turn_tree()
    assert {node["entry"]["id"] for node in tree[0]["children"]} == {
        first_turn.id, second_turn.id,
    }

    backfill = await branches.switch(session, first_end.id)
    assert backfill.type == "branch_summary"
    assert backfill.payload["lca_id"] == root_end.id
    active_ids = {entry.id for entry in session.get_branch()}
    assert first_turn.id in active_ids
    assert second_turn.id not in active_ids
    assert second_end.id in session.by_id
    projected = ContextProjector(128_000, 16_000).project(session)
    assert any(item.get("role") == "developer" and "离开分支摘要" in item.get("content", "")
               for item in projected.input_items)

    await branches.switch(session, second_end.id)
    assert second_turn.id in {entry.id for entry in session.get_branch()}
    assert first_turn.id not in {entry.id for entry in session.get_branch()}


@pytest.mark.asyncio
async def test_any_turn_can_be_forked_including_current_leaf(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    await session.append("user_message", {"content": "root"})
    root_end = await session.append("run_state", {"status": "completed"})
    middle = await session.append("user_message", {"content": "middle"})
    middle_end = await session.append("run_state", {"status": "completed"})
    old_leaf = await session.append("user_message", {"content": "old leaf"})
    await session.append("run_state", {"status": "completed"})

    branches = BranchService(FakeModelAdapter())
    await branches.switch(session, middle_end.id)
    middle_fork = await session.append("user_message", {"content": "middle fork"})
    await session.append("run_state", {"status": "completed"})
    await branches.switch(session, root_end.id)
    root_fork = await session.append("user_message", {"content": "root fork"})
    root_fork_end = await session.append("run_state", {"status": "completed"})

    marker = await branches.switch(session, root_fork_end.id)
    assert marker.type == "branch_switch"
    assert marker.parent_id == root_fork_end.id
    current_fork = await session.append("user_message", {"content": "current fork"})
    await session.append("run_state", {"status": "completed"})
    await branches.switch(session, root_fork_end.id)
    second_current_fork = await session.append("user_message", {"content": "another current fork"})
    await session.append("run_state", {"status": "completed"})

    tree = session.get_turn_tree()
    assert {node["entry"]["id"] for node in tree[0]["children"]} == {middle.id, root_fork.id}
    middle_node = next(node for node in tree[0]["children"] if node["entry"]["id"] == middle.id)
    assert {node["entry"]["id"] for node in middle_node["children"]} == {old_leaf.id, middle_fork.id}
    root_fork_node = next(node for node in tree[0]["children"] if node["entry"]["id"] == root_fork.id)
    assert {node["entry"]["id"] for node in root_fork_node["children"]} == {
        current_fork.id, second_current_fork.id,
    }
    assert session.validate_tool_pairs() == []


@pytest.mark.asyncio
async def test_return_to_existing_branch_without_transferring_summary(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    await session.append("user_message", {"content": "root"})
    root_end = await session.append("run_state", {"status": "completed"})
    first_turn = await session.append("user_message", {"content": "first path"})
    first_end = await session.append("run_state", {"status": "completed"})

    branches = BranchService(FakeModelAdapter())
    await branches.switch(session, root_end.id)
    second_turn = await session.append("user_message", {"content": "second path"})
    second_end = await session.append("run_state", {"status": "completed"})
    tree = session.get_turn_tree()
    first_node = next(node for node in tree[0]["children"] if node["entry"]["id"] == first_turn.id)
    assert {tip["entry_id"] for tip in first_node["branch_tips"]} == {first_end.id}
    assert first_node["branch_tips"][0]["active"] is False

    # A declined transfer must skip the summary model and leave only an
    # auditable branch-resume marker on the selected branch.
    branches.adapter = FailingSummaryAdapter()
    resumed = await branches.switch(
        session, first_end.id, include_summary=False, mode="resume",
    )
    assert resumed.type == "branch_resume"
    assert resumed.parent_id == first_end.id
    assert resumed.payload["summary_mode"] == "skipped"
    assert not any(entry.type == "branch_summary" for entry in session.get_branch())
    projected = ContextProjector(128_000, 16_000).project(session)
    assert not any("离开分支摘要" in item.get("content", "") for item in projected.input_items)
    assert second_turn.id not in {entry.id for entry in session.get_branch()}
    assert second_end.id in session.by_id
    tips = session.get_turn_tree()[0]["children"]
    current_node = next(node for node in tips if node["entry"]["id"] == first_turn.id)
    assert current_node["branch_tips"] == [{"entry_id": resumed.id, "seq": resumed.seq, "active": True}]
    second_node = next(node for node in tips if node["entry"]["id"] == second_turn.id)
    assert any(tip["entry_id"] == second_end.id for tip in second_node["branch_tips"])


@pytest.mark.asyncio
async def test_failed_branch_summary_preserves_goal_pending_work_and_errors(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    root = await session.append("user_message", {"content": "Root task"})
    await session.append("assistant_message", {"content": "Root done"})
    await session.append("user_message", {"content": "Implement scanner"})
    await session.append("tool_call", {
        "call_id": "call-1", "name": "run_command", "arguments": {"command": "pytest"},
    })
    await session.append("tool_result", {
        "call_id": "call-1", "tool_name": "run_command", "output": "3 failed", "is_error": True,
    })

    backfill = await BranchService(FailingSummaryAdapter()).switch(session, root.id)
    summary = backfill.payload["summary"]
    assert summary["branch_goal"] == "Implement scanner"
    assert summary["pending"] == ["Implement scanner"]
    assert summary["commands_and_tests"] == ["pytest"]
    assert summary["errors_and_blockers"] == ["run_command: 3 failed"]
    projected = ContextProjector(128_000, 16_000).project(session)
    assert any("3 failed" in item.get("content", "") for item in projected.input_items)


@pytest.mark.asyncio
async def test_branch_switch_uses_deterministic_fallback_when_summary_model_fails(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    root = await session.append("user_message", {"content": "root"})
    await session.append(
        "tool_call",
        {"call_id": "call-1", "name": "run_command", "arguments": {"command": "pytest"}},
    )
    await session.append(
        "tool_result",
        {"call_id": "call-1", "tool_name": "run_command", "output": "1 passed"},
    )
    summary = await BranchService(FailingSummaryAdapter()).switch(session, root.id)
    assert summary.type == "branch_summary"
    assert summary.payload["summary"]["recommended_next_step"]
    assert summary.payload["summary"]["commands_and_tests"] == ["pytest"]
    switch = next(entry for entry in session.entries if entry.type == "branch_switch")
    assert switch.payload["summary_mode"] == "fallback"


@pytest.mark.asyncio
async def test_branch_summary_reuses_checkpoint_and_limits_model_input(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    root = await session.append("user_message", {"content": "root"})
    await session.append("assistant_message", {"content": "OLD_MARKER " * 1000})
    await session.append(
        "compaction",
        {"summary": {"goal": "CHECKPOINT_MARKER"}, "first_kept_entry_id": root.id},
    )
    for _ in range(8):
        await session.append("assistant_message", {"content": "recent detail " * 80})
    await session.append("assistant_message", {"content": "RECENT_MARKER"})
    adapter = CapturingSummaryAdapter()

    summary = await BranchService(adapter, max_transcript_chars=1600).switch(session, root.id)

    assert len(adapter.transcript) <= 1600
    assert "CHECKPOINT_MARKER" in adapter.transcript
    assert "RECENT_MARKER" in adapter.transcript
    assert "OLD_MARKER" not in adapter.transcript
    assert len(summary.payload["source_entry_ids"]) == 11


@pytest.mark.asyncio
async def test_switching_to_tool_call_completes_pair_on_new_branch(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    await session.append("user_message", {"content": "inspect"})
    call = await session.append("tool_call", {"call_id": "call-1", "name": "read_file", "arguments": {"path": "a.py"}})
    await session.append("tool_result", {"call_id": "call-1", "tool_name": "read_file", "output": "old branch"})

    summary = await BranchService(FakeModelAdapter()).switch(session, call.id)

    assert summary.type == "branch_summary"
    assert summary.parent_id != call.id
    assert session.by_id[summary.parent_id].payload["synthetic"] is True
    assert session.validate_tool_pairs() == []
