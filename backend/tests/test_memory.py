from dataclasses import replace
from pathlib import Path
import subprocess

import pytest

from traceforge.config import Settings
from traceforge.context import ContextProjector
from traceforge.main import create_app
from traceforge.models import HookContext, HookPoint, RunStatus
from traceforge.models import CompactionSummary

from .fakes import FakeModelAdapter


class CountingSummaryAdapter(FakeModelAdapter):
    def __init__(self):
        super().__init__()
        self.transcripts: list[str] = []

    async def summarize_compaction(self, transcript, previous):
        self.transcripts.append(transcript)
        return await super().summarize_compaction(transcript, previous)


def _app(tmp_path: Path):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data",
                             model_config_dir=tmp_path / "config"))
    app.state.services.model_settings.adapter.replace(FakeModelAdapter())
    return app.state.services


@pytest.mark.asyncio
async def test_new_project_without_handoff_returns_normal_absent_state(tmp_path: Path):
    services = _app(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    session = services.sessions.create(services.workspaces.register(str(repo)))
    result = await services.runner.tools.execute(session.header.id, session.workspace, "read_project_handoff", {})
    assert result.is_error is False
    assert result.metadata["handoff_available"] is False
    assert "inspecting current files" in result.output
    assert not (repo / ".traceforge" / "handoff.md").exists()


@pytest.mark.asyncio
async def test_invalid_history_ids_return_search_recovery_instruction(tmp_path: Path):
    services = _app(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    session = services.sessions.create(services.workspaces.register(str(repo)))
    for requested_session, entry_id in [("x", "x"), (session.header.id, "x")]:
        result = await services.runner.tools.execute(
            session.header.id, session.workspace, "read_history_entry",
            {"session_id": requested_session, "entry_id": entry_id},
        )
        assert result.is_error is True
        assert "Call search_history" in result.output


@pytest.mark.asyncio
async def test_new_session_gets_handoff_notice_and_reads_file_on_demand(tmp_path: Path):
    services = _app(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = services.workspaces.register(str(repo))
    first = services.sessions.create(workspace)
    hook = next(item for item in services.hooks._hooks if item.name == "workspace_handoff")
    await first.append("user_message", {"content": "修复解析器"}, run_id="run-1")
    await first.append("assistant_message", {"content": "已检查 parser.py，准备修改"}, run_id="run-1")
    await hook.handle(HookContext(session_id=first.header.id, run_id="run-1",
                                  point=HookPoint.RUN_FINISHED, status=RunStatus.COMPLETED))
    path = repo / ".traceforge" / "handoff.md"
    assert path.is_file()
    assert services.handoff_store.read(workspace.id)["revision"] == 1

    second = services.sessions.create(workspace)
    await second.append("user_message", {"content": "继续工作"}, run_id="run-2")
    outcome = await hook.handle(HookContext(session_id=second.header.id, run_id="run-2",
                                            point=HookPoint.USER_MESSAGE_COMMITTED,
                                            status=RunStatus.BRIEFING))
    assert len(outcome.entry_drafts) == 1
    draft = outcome.entry_drafts[0]
    assert draft.type == "project_handoff"
    assert "search_history" in draft.payload["content"]
    assert "read_project_handoff" in draft.payload["content"]
    assert "history summarized" not in draft.payload["content"]
    assert "README.md" not in draft.payload["content"]
    assert outcome.state_updates["project_handoff_available"] is True
    await second.append(draft.type, draft.payload, run_id="run-2")
    assert any("交接文件" in item.get("content", "")
               for item in ContextProjector(128000, 16000).project(second).input_items)
    handoff = await services.runner.tools.execute(second.header.id, second.workspace,
                                                  "read_project_handoff", {})
    assert handoff.is_error is False
    assert "history summarized" in handoff.output
    assert "traceforge-meta:" not in handoff.output
    await second.append("assistant_message", {"content": "继续检查 parser.py"}, run_id="run-2")
    await hook.handle(HookContext(session_id=second.header.id, run_id="run-2",
                                  point=HookPoint.RUN_FINISHED, status=RunStatus.COMPLETED))
    assert services.handoff_store.read(workspace.id)["revision"] == 2
    assert len(list(repo.glob(".traceforge/handoff*.md"))) == 1


@pytest.mark.asyncio
async def test_history_search_sees_original_branch_records_but_stays_in_project(tmp_path: Path):
    services = _app(tmp_path)
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    project_a = services.workspaces.register(str(repo_a))
    project_b = services.workspaces.register(str(repo_b))
    old = services.sessions.create(project_a)
    root = await old.append("user_message", {"content": "root"})
    hidden = await old.append("user_message", {"content": "unique-parser-fact 原始细节"})
    await old.append("assistant_message", {"content": "另一条分支"}, parent_id=root.id)
    fresh = services.sessions.create(project_a)
    foreign = services.sessions.create(project_b)
    await foreign.append("user_message", {"content": "unique-parser-fact 其他项目"})

    found = await services.runner.tools.execute(
        fresh.header.id, fresh.workspace, "search_history",
        {"query": "unique-parser-fact", "scope": "workspace", "max_results": 10},
    )
    assert found.is_error is False
    assert hidden.id in found.output
    assert '"on_active_branch": false' in found.output
    assert foreign.header.id not in found.output
    full = await services.runner.tools.execute(
        fresh.header.id, fresh.workspace, "read_history_entry",
        {"session_id": old.header.id, "entry_id": hidden.id, "start_char": 0, "max_chars": 20000},
    )
    assert "原始细节" in full.output
    denied = await services.runner.tools.execute(
        fresh.header.id, fresh.workspace, "read_history_entry",
        {"session_id": foreign.header.id, "entry_id": foreign.entries[0].id,
         "start_char": 0, "max_chars": 20000},
    )
    assert denied.is_error is True


@pytest.mark.asyncio
async def test_project_handoff_does_not_dirty_git_and_marks_stale_code(tmp_path: Path):
    services = _app(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    source = repo / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test",
                    "-c", "user.email=test@example.com", "commit", "-m", "initial"],
                   check=True, capture_output=True)
    workspace = services.workspaces.register(str(repo))
    session = services.sessions.create(workspace)
    await session.append("user_message", {"content": "检查 app.py"}, run_id="run-1")
    await session.append("assistant_message", {"content": "检查完成"}, run_id="run-1")
    await services.handoff_store.update_from_run(session, "run-1")
    status = subprocess.run(["git", "-C", str(repo), "status", "--short",
                             "--untracked-files=all"], check=True, capture_output=True, text=True)
    assert status.stdout.strip() == ""
    current = await services.runner.tools.execute(session.header.id, session.workspace,
                                                  "read_project_handoff", {})
    assert "不一致" not in current.output
    source.write_text("value = 2\n", encoding="utf-8")
    stale = await services.runner.tools.execute(session.header.id, session.workspace,
                                                "read_project_handoff", {})
    assert "不同" in stale.output


@pytest.mark.asyncio
async def test_new_session_imports_previous_compaction_when_project_file_is_missing(tmp_path: Path):
    services = _app(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = services.workspaces.register(str(repo))
    old = services.sessions.create(workspace)
    await old.append("compaction", {
        "summary": CompactionSummary(goal="继续修复 lexer", next_steps=["检查 token.py"]).model_dump(),
        "repository_state": {"workspace": str(repo)}, "source_entry_ids": [],
    })
    new = services.sessions.create(workspace)
    await new.append("user_message", {"content": "继续"})
    notice = services.handoff_store.notice_for(new)
    assert notice is not None and "read_project_handoff" in notice[0]
    assert "继续修复 lexer" not in notice[0]
    handoff = await services.runner.tools.execute(new.header.id, new.workspace,
                                                  "read_project_handoff", {})
    assert "继续修复 lexer" in handoff.output
    assert services.handoff_store.read(workspace.id)["summary_mode"] == "migration"


@pytest.mark.asyncio
async def test_handoff_notice_survives_rolling_context_projection(tmp_path: Path):
    services = _app(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    session = services.sessions.create(services.workspaces.register(str(repo)))
    await session.append("user_message", {"content": "开始"})
    notice = await session.append("project_handoff", {"content": "有交接文件，按需 read_project_handoff"})
    recent = await session.append("user_message", {"content": "新任务"})
    await session.append("compaction", {"summary": {"goal": "新任务"},
                                        "first_kept_entry_id": recent.id})
    projected = ContextProjector(128000, 16000).project(session)
    assert notice.id in projected.source_entry_ids
    assert any("read_project_handoff" in item.get("content", "")
               for item in projected.input_items)


@pytest.mark.asyncio
async def test_compaction_reuses_one_model_summary_and_run_finish_only_summarizes_delta(tmp_path: Path):
    services = _app(tmp_path)
    adapter = CountingSummaryAdapter()
    services.model_settings.adapter.replace(adapter)
    repo = tmp_path / "repo"
    repo.mkdir()
    session = services.sessions.create(services.workspaces.register(str(repo)))
    await session.append("user_message", {"content": "OLD_HISTORY " * 300}, run_id="run-0")
    await session.append("assistant_message", {"content": "旧工作 " * 300}, run_id="run-0")
    await services.handoff_store.update_from_run(session, "run-0")
    assert len(adapter.transcripts) == 1
    await session.append("user_message", {"content": "新任务 " * 300}, run_id="run-1")
    await session.append("assistant_message", {"content": "已完成初步检查 " * 300}, run_id="run-1")
    compactor = services.runner.compactor
    compactor.context_window = 200
    compactor.reserve_tokens = 50
    compactor.keep_recent_tokens = 10
    compacted = await compactor.compact(session, "run-1")
    assert compacted is not None
    assert compacted.payload["first_kept_entry_id"] is None
    assert len(adapter.transcripts) == 2
    assert services.handoff_store.read(session.header.workspace_id)["summary_mode"] == "model"
    assert compacted.type == "context_checkpoint"
    assert "summary" not in compacted.payload
    assert await services.handoff_store.update_from_run(session, "run-1") is None
    assert len(adapter.transcripts) == 2
    await session.append("assistant_message", {"content": "DELTA_AFTER_COMPACTION 验证已通过"}, run_id="run-1")
    await services.handoff_store.update_from_run(session, "run-1")
    assert len(adapter.transcripts) == 3
    assert "DELTA_AFTER_COMPACTION" in adapter.transcripts[-1]
    assert "OLD_HISTORY" not in adapter.transcripts[-1]


@pytest.mark.asyncio
async def test_missing_handoff_reexposes_lossless_history(tmp_path: Path):
    services = _app(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    session = services.sessions.create(services.workspaces.register(str(repo)))
    early = await session.append("user_message", {"content": "EARLY_DETAIL " * 300})
    await session.append("assistant_message", {"content": "已检查" * 200})
    await session.append("user_message", {"content": "继续"})
    await session.append("assistant_message", {"content": "下一步"})
    compactor = services.runner.compactor
    compactor.keep_recent_tokens = 10
    checkpoint = await compactor.compact(session)
    assert checkpoint is not None and checkpoint.type == "context_checkpoint"
    assert early.id not in ContextProjector(128000, 16000).project(session).source_entry_ids
    services.handoff_store.path_for(session.header.workspace_id).unlink()
    recovered = ContextProjector(128000, 16000).project(session)
    assert early.id in recovered.source_entry_ids
    assert checkpoint.id not in recovered.source_entry_ids


@pytest.mark.asyncio
async def test_corrupt_handoff_reexposes_lossless_history(tmp_path: Path):
    services = _app(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    session = services.sessions.create(services.workspaces.register(str(repo)))
    early = await session.append("user_message", {"content": "EARLY_DETAIL " * 300})
    await session.append("assistant_message", {"content": "已检查" * 200})
    await session.append("user_message", {"content": "继续"})
    await session.append("assistant_message", {"content": "下一步"})
    compactor = services.runner.compactor
    compactor.keep_recent_tokens = 10
    checkpoint = await compactor.compact(session)
    assert checkpoint is not None
    services.handoff_store.path_for(session.header.workspace_id).write_text("broken", encoding="utf-8")
    recovered = ContextProjector(128000, 16000).project(session)
    assert early.id in recovered.source_entry_ids
    assert checkpoint.id not in recovered.source_entry_ids


@pytest.mark.asyncio
async def test_failed_handoff_write_never_creates_context_checkpoint(tmp_path: Path):
    services = _app(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    session = services.sessions.create(services.workspaces.register(str(repo)))
    await session.append("user_message", {"content": "旧任务 " * 300})
    await session.append("assistant_message", {"content": "旧结果 " * 300})
    await session.append("user_message", {"content": "继续"})
    await session.append("assistant_message", {"content": "正在做"})
    (repo / ".traceforge").write_text("occupied", encoding="utf-8")
    with pytest.raises(ValueError, match="real directory"):
        await services.runner.compactor.compact(session)
    assert not any(entry.type == "context_checkpoint" for entry in session.entries)
