from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evals"))

from code_tasks import CodeTask  # noqa: E402
from analyze_code_results import render_report, tool_error_breakdown, trajectory_signals  # noqa: E402
from complex_tasks import COMPLEX_TASKS  # noqa: E402
from run_code_tasks import (  # noqa: E402
    apply_worktree_changes,
    git,
    make_repo,
    summarize_results,
    trajectory,
    validate_fixture,
)
from traceforge.models import SessionEntry  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize("task", COMPLEX_TASKS, ids=lambda task: task.id)
async def test_complex_fixture_is_fail_then_pass(task: CodeTask):
    checked = await validate_fixture(task, None)
    assert checked["empty_exit_code"] != 0
    assert checked["reference_exit_code"] == 0
    assert len(checked["base_commit"]) == 40


@pytest.mark.asyncio
async def test_rejects_hidden_check_that_passes_on_empty_patch():
    task = CodeTask(id="bad_case", prompt="Do something", files={"value.py": "x = 1\n"},
                    hidden_test="import unittest\nclass Test(unittest.TestCase):\n"
                                "    def test_empty(self): self.assertTrue(True)\n")
    with pytest.raises(ValueError, match="passes on empty patch"):
        await validate_fixture(task, None)


def test_clean_room_patch_includes_deleted_and_untracked_files(tmp_path: Path):
    task = CodeTask(id="patch", prompt="Fix", files={"old.py": "value = 1\n", "delete.py": "x = 1\n"})
    source = make_repo(tmp_path / "source", task)
    target = make_repo(tmp_path / "target", task)
    base = str(git(source, "rev-parse", "HEAD")).strip()
    assert base == str(git(target, "rev-parse", "HEAD")).strip()
    (source / "old.py").write_text("value = 2\n", encoding="utf-8")
    (source / "delete.py").unlink()
    (source / "new.py").write_text("new = True\n", encoding="utf-8")
    assert apply_worktree_changes(source, target, base) == ["delete.py", "new.py", "old.py"]
    assert (target / "old.py").read_text(encoding="utf-8") == "value = 2\n"
    assert not (target / "delete.py").exists()
    assert (target / "new.py").read_text(encoding="utf-8") == "new = True\n"
    git(target, "add", "-N", "--all")
    patch = str(git(target, "diff", "--binary", "HEAD"))
    assert "new.py" in patch and "delete.py" in patch


def test_trajectory_and_repeated_success_summary():
    entries = [
        SessionEntry(type="tool_call", seq=1, payload={"call_id": "r", "name": "read_file",
                                                       "arguments": {"path": "a.py"}}),
        SessionEntry(type="tool_result", seq=2, payload={"call_id": "r", "exit_code": 0}),
        SessionEntry(type="tool_call", seq=3, payload={"call_id": "w", "name": "apply_patch",
                                                       "arguments": {"patch": "x"}}),
        SessionEntry(type="tool_result", seq=4, payload={"call_id": "w", "exit_code": 0}),
        SessionEntry(type="tool_call", seq=5, payload={"call_id": "t", "name": "run_command",
                                                       "arguments": {"command": "python -m unittest -q"}}),
        SessionEntry(type="tool_result", seq=6, payload={"call_id": "t", "exit_code": 0}),
        SessionEntry(type="tool_call", seq=7, payload={"call_id": "r2", "name": "read_file",
                                                       "arguments": {"path": "a.py"}}),
    ]
    trace = trajectory(entries)
    assert trace["read_calls"] == 2
    assert trace["write_calls"] == 1
    assert trace["test_calls"] == 1
    assert trace["repeated_tool_calls"] == 1
    assert trace["successful_test_after_edit"]
    rows = [
        {"task": "a", "passed": True, "failure_category": "passed", "elapsed_seconds": 10,
         "model_usage": {"total_tokens": 100}},
        {"task": "a", "passed": False, "failure_category": "hidden_test_failed",
         "elapsed_seconds": 11, "model_usage": {"total_tokens": 120}},
        {"task": "b", "passed": True, "failure_category": "passed", "elapsed_seconds": 12,
         "model_usage": {"total_tokens": 80}},
    ]
    summary = summarize_results(rows)
    assert summary["passes"] == 2
    assert summary["tasks_all_repeats_passed"] == 1
    assert summary["successful_median_tokens"] == 90


def test_offline_report_flags_trajectory_for_review(tmp_path: Path):
    artifact = tmp_path / "trace"
    artifact.mkdir()
    (artifact / "trajectory.json").write_text(
        '{"timeline":[{"seq":1,"type":"tool_call","name":"apply_patch"}]}',
        encoding="utf-8",
    )
    (artifact / "session.jsonl").write_text(
        '{"type":"tool_result","payload":{"tool_name":"create_file","is_error":true,'
        '"output":"FileExistsError: already exists"}}\n',
        encoding="utf-8",
    )
    result = {
        "task": "example", "repeat": 1, "passed": False,
        "failure_category": "hidden_test_failed", "elapsed_seconds": 3,
        "model_usage": {"total_tokens": 100}, "write_calls": 1,
        "successful_test_after_edit": False, "artifacts": str(artifact),
    }
    assert "首次显式编辑前未见仓库读取" in trajectory_signals(result)
    assert tool_error_breakdown(result)["create_file：已有文件仍调用创建"] == 1
    markdown = render_report({"model": "test", "suite": "complex", "results": [result]},
                             tmp_path / "result.json")
    assert "0/1" in markdown
    assert "hidden_test_failed" in markdown
    assert "首次显式编辑前未见仓库读取" in markdown
    assert "工具错误总数：1" in markdown
