from pathlib import Path

import pytest

from infra.runner.tool_worker import create_file, delete_file, move_file
from traceforge.artifacts import ArtifactStore, MAX_CONTEXT_BYTES
from traceforge.models import RiskLevel
from traceforge.security import PolicyEngine
from traceforge.tools import ToolService


def test_worker_creates_moves_and_deletes_files_without_overwriting(tmp_path: Path):
    create_file({"path": "src/module.py", "content": "print('ok')\n"}, tmp_path)
    with pytest.raises(FileExistsError):
        create_file({"path": "src/module.py", "content": "overwrite"}, tmp_path)
    move_file({"source_path": "src/module.py", "destination_path": "lib/module.py"}, tmp_path)
    assert not (tmp_path / "src/module.py").exists()
    assert (tmp_path / "lib/module.py").read_text(encoding="utf-8") == "print('ok')\n"
    with pytest.raises(FileExistsError):
        move_file({"source_path": "lib/module.py", "destination_path": "lib/module.py"}, tmp_path)
    delete_file({"path": "lib/module.py"}, tmp_path)
    assert not (tmp_path / "lib/module.py").exists()
    with pytest.raises(ValueError):
        delete_file({"path": "lib"}, tmp_path)


def test_file_tool_policy_requires_review_for_delete_and_move(tmp_path: Path):
    source = tmp_path / "source.txt"
    source.write_text("text", encoding="utf-8")
    policy = PolicyEngine()
    assert policy.evaluate("create_file", {"path": "new.txt"}, str(tmp_path)).decision == RiskLevel.ALLOW
    assert policy.evaluate("delete_file", {"path": "source.txt"}, str(tmp_path)).decision == RiskLevel.ASK
    assert policy.evaluate(
        "move_file", {"source_path": "source.txt", "destination_path": "renamed.txt"}, str(tmp_path)
    ).decision == RiskLevel.ASK
    assert policy.evaluate(
        "move_file", {"source_path": "source.txt", "destination_path": "../outside.txt"}, str(tmp_path)
    ).decision == RiskLevel.DENY


@pytest.mark.asyncio
async def test_large_read_file_keeps_full_artifact_and_limits_model_output(tmp_path: Path):
    content = "".join(f"line {index} {'x' * 100}\n" for index in range(1000))
    (tmp_path / "large.txt").write_text(content, encoding="utf-8")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    service = ToolService(None, artifacts, tmp_path / "tool_worker.py")

    result = await service.execute(
        "session", str(tmp_path), "read_file", {"path": "large.txt", "start_line": None, "end_line": None}
    )

    assert result.is_error is False
    assert result.artifact_id is not None
    assert len(result.output.encode("utf-8")) <= MAX_CONTEXT_BYTES + 100
    assert "output truncated" in result.output
    assert "line 999" in artifacts.resolve(result.artifact_id).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_file_discovery_prunes_generated_directories_and_uses_forward_slashes(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "entry.py").write_text("target = 1\n", encoding="utf-8")
    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    (ignored / "noise.py").write_text("target = 2\n", encoding="utf-8")
    service = ToolService(None, ArtifactStore(tmp_path / "artifacts"), tmp_path / "worker.py")

    listed = await service.execute("session", str(tmp_path), "list_files", {"path": ".", "max_depth": 4})
    searched = await service.execute(
        "session", str(tmp_path), "search_code", {"query": "TARGET", "path": ".", "glob": "*.py"}
    )

    assert "src/entry.py" in listed.output
    assert "node_modules" not in listed.output
    assert "src/entry.py:1:" in searched.output
    assert "noise.py" not in searched.output
