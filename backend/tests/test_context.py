from pathlib import Path
import subprocess

import pytest

from traceforge.context import BranchService, CompactionService, ContextProjector
from traceforge.models import SessionHeader
from traceforge.memory import WorkspaceHandoffStore
from traceforge.storage import JsonlSession, SessionStore

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


class CapturingHandoffAdapter(FakeModelAdapter):
    def __init__(self):
        super().__init__()
        self.transcript = ""

    async def summarize_compaction(self, transcript, previous):
        self.transcript = transcript
        return await super().summarize_compaction(transcript, previous)


class FailingCompactionAdapter(FakeModelAdapter):
    async def summarize_compaction(self, transcript, previous):
        raise RuntimeError("summary unavailable")


def attach_handoff(compactor, session, adapter):
    data_root = session.path.parent.parent if session.path.parent.name == "sessions" else session.path.parent
    compactor.handoff_store = WorkspaceHandoffStore(
        data_root / "handoffs", SessionStore(data_root / "sessions"), adapter, compactor,
    )
    return compactor.handoff_store


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
async def test_context_checkpoint_keeps_recent_raw_without_projecting_handoff(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    first = await session.append("user_message", {"content": "old " * 200})
    await session.append("assistant_message", {"content": "old answer " * 200})
    recent = await session.append("user_message", {"content": "recent"})
    await session.append("assistant_message", {"content": "recent answer"})
    adapter = FakeModelAdapter()
    compactor = CompactionService(adapter, 200, 50, 50)
    attach_handoff(compactor, session, adapter)
    compacted = await compactor.compact(session)
    assert compacted is not None
    assert compacted.type == "context_checkpoint"
    projected = ContextProjector(200, 50).project(session)
    assert not any("history summarized" in item.get("content", "") for item in projected.input_items)
    assert any("read_project_handoff" in item.get("content", "") for item in projected.input_items)
    assert any(item.get("content") == "recent" for item in projected.input_items)
    assert first.id in [entry.id for entry in session.entries]
    assert recent.id in [entry.id for entry in session.entries]
    assert (session.path.parent / compacted.payload["handoff_file"]).is_file()


@pytest.mark.asyncio
async def test_handoff_uses_git_facts_and_preserves_clarification_and_tool_evidence(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, check=True).stdout.strip()

    git("init")
    (repo / "src.py").write_text("value = 1\n", encoding="utf-8")
    git("add", "src.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "baseline")
    (repo / "src.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "new.txt").write_text("new file\n", encoding="utf-8")

    session = JsonlSession.create(
        tmp_path / "data" / "sessions" / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(repo)),
    )
    original = await session.append("user_message", {"content": "修复项目 " * 300})
    await session.append("clarification_answer", {"question": "目标？", "answer": "保留现有 API"})
    await session.append("tool_call", {"call_id": "test", "name": "run_command", "arguments": {"command": "pytest"}})
    await session.append("tool_result", {
        "call_id": "test", "tool_name": "run_command", "output": "2 passed", "exit_code": 0,
        "artifact_id": "artifact_test", "is_error": False,
    })
    await session.append("user_message", {"content": "继续"})
    await session.append("assistant_message", {"content": "下一步"})
    adapter = CapturingHandoffAdapter()

    compactor = CompactionService(adapter, 2000, 200, 100)
    attach_handoff(compactor, session, adapter)
    compacted = await compactor.compact(session)

    assert compacted is not None
    state = compactor.handoff_store.read(session.header.workspace_id)["repository_state"]
    assert state["kind"] == "git"
    assert state["head"] == git("rev-parse", "HEAD")
    assert any("src.py" in line for line in state["status_lines"])
    assert any("new.txt" in line for line in state["status_lines"])
    assert "clarification_answer 目标？: 保留现有 API" in adapter.transcript
    assert "exit_code=0" in adapter.transcript
    assert "artifact_test" in adapter.transcript
    assert compacted.payload["handoff_mode"] == "model"
    handoff = session.path.parent.parent / compacted.payload["handoff_file"]
    assert handoff.is_file()
    assert "# TraceForge 项目交接" in handoff.read_text(encoding="utf-8")
    assert state["head"] in handoff.read_text(encoding="utf-8")
    assert original.id in session.by_id
    assert "src.py" not in str(ContextProjector(2000, 200).project(session).input_items)


@pytest.mark.asyncio
async def test_handoff_file_is_still_written_when_summary_model_fails(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    await session.append("user_message", {"content": "修复错误 " * 300})
    await session.append("assistant_message", {"content": "已检查日志"})
    await session.append("user_message", {"content": "继续"})
    await session.append("assistant_message", {"content": "下一步"})

    adapter = FailingCompactionAdapter()
    compactor = CompactionService(adapter, 2000, 200, 100)
    attach_handoff(compactor, session, adapter)
    compacted = await compactor.compact(session)

    assert compacted is not None
    assert compacted.payload["handoff_mode"] == "fallback"
    handoff = session.path.parent / compacted.payload["handoff_file"]
    assert "摘要来源：fallback" in handoff.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_handoff_model_input_is_bounded_but_keeps_goal_and_recent_evidence(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    await session.append("user_message", {"content": "EARLY_GOAL 修复登录问题"})
    await session.append("assistant_message", {"content": "旧输出 " * 10000})
    await session.append("tool_call", {"call_id": "test", "name": "run_command", "arguments": {"command": "pytest"}})
    await session.append("tool_result", {
        "call_id": "test", "tool_name": "run_command", "output": "2 passed", "exit_code": 0,
    })
    await session.append("user_message", {"content": "继续"})
    await session.append("assistant_message", {"content": "处理中"})
    adapter = CapturingHandoffAdapter()

    compactor = CompactionService(adapter, 2000, 200, 100)
    attach_handoff(compactor, session, adapter)
    assert await compactor.compact(session) is not None

    assert len(adapter.transcript) <= 8000
    assert "EARLY_GOAL" in adapter.transcript
    assert "2 passed" in adapter.transcript


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
    adapter = FakeModelAdapter()
    compactor = CompactionService(adapter, 300, 50, 40)
    attach_handoff(compactor, session, adapter)
    assert await compactor.compact(session) is not None

    await session.append("user_message", {"content": "recent request"})
    await session.append("tool_call", {"call_id": "call-recent", "name": "read_file", "arguments": {}})
    await session.append(
        "tool_result",
        {"call_id": "call-recent", "tool_name": "read_file", "output": "recent output"},
    )
    await session.append("assistant_message", {"content": "recent answer"})
    assert await compactor.compact(session) is not None

    assert session.path.read_text(encoding="utf-8").count('"type":"context_checkpoint"') == 2
    assert early in session.path.read_text(encoding="utf-8")
    projected = ContextProjector(300, 50).project(session)
    assert any(item.get("content") == "recent request" for item in projected.input_items)
    assert session.validate_tool_pairs() == []


@pytest.mark.asyncio
async def test_compaction_can_cut_inside_one_long_run_at_a_model_round(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    await session.append("user_message", {"content": "检查项目并修复问题"})
    await session.append("run_state", {"status": "executing", "round": 1})
    await session.append("tool_call", {"call_id": "old", "name": "read_file", "arguments": {}})
    old_result = await session.append("tool_result", {
        "call_id": "old", "tool_name": "read_file", "output": "old evidence " * 500,
    })
    kept_round = await session.append("run_state", {"status": "executing", "round": 2})
    await session.append("tool_call", {"call_id": "recent", "name": "read_file", "arguments": {}})
    await session.append("tool_result", {
        "call_id": "recent", "tool_name": "read_file", "output": "recent evidence",
    })

    adapter = FakeModelAdapter()
    compactor = CompactionService(adapter, 2000, 200, 100)
    attach_handoff(compactor, session, adapter)
    compacted = await compactor.compact(session)

    assert compacted is not None
    assert compacted.payload["first_kept_entry_id"] == kept_round.id
    projected = ContextProjector(2000, 200).project(session)
    assert old_result.id not in projected.source_entry_ids
    assert "recent" in str(projected.input_items)
    assert session.validate_tool_pairs() == []
    assert old_result.id in session.by_id


@pytest.mark.asyncio
async def test_compaction_can_summarize_one_oversized_completed_batch(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    user = await session.append("user_message", {"content": "检查大型日志"})
    await session.append("run_state", {"status": "executing", "round": 1})
    await session.append("tool_call", {"call_id": "large", "name": "read_file", "arguments": {}})
    result = await session.append("tool_result", {
        "call_id": "large", "tool_name": "read_file", "output": "log line " * 3000,
    })

    adapter = FakeModelAdapter()
    compactor = CompactionService(adapter, 2000, 200, 100)
    attach_handoff(compactor, session, adapter)
    compacted = await compactor.compact(session)

    assert compacted is not None
    assert compacted.payload["first_kept_entry_id"] is None
    projected = ContextProjector(2000, 200).project(session)
    assert projected.source_entry_ids == [compacted.id]
    assert user.id in session.by_id and result.id in session.by_id
    assert session.validate_tool_pairs() == []


@pytest.mark.asyncio
async def test_compaction_keeps_unanswered_new_user_request(tmp_path: Path):
    session = JsonlSession.create(
        tmp_path / "s.jsonl",
        SessionHeader(id="s", workspace_id="w", workspace=str(tmp_path)),
    )
    await session.append("user_message", {"content": "old"})
    await session.append("assistant_message", {"content": "done"})
    new_request = await session.append("user_message", {"content": "new request " * 3000})
    await session.append("run_state", {"status": "executing", "round": 1})

    adapter = FakeModelAdapter()
    compactor = CompactionService(adapter, 2000, 200, 100)
    attach_handoff(compactor, session, adapter)
    compacted = await compactor.compact(session)

    assert compacted is not None
    assert compacted.payload["first_kept_entry_id"] == new_request.id
    assert new_request.id in ContextProjector(2000, 200).project(session).source_entry_ids


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
    assert "OLD_MARKER" in adapter.transcript
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
