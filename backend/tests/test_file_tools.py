from pathlib import Path

import pytest

from infra.runner.tool_worker import apply_patch, create_file, delete_file, move_file
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


def test_patch_defaults_to_one_match_and_rejects_ambiguous_or_empty_text(tmp_path: Path):
    target = tmp_path / "file.txt"
    target.write_text("old\nother\n", encoding="utf-8")
    apply_patch({"path": "file.txt", "old_text": "old", "new_text": "new"}, tmp_path)
    assert target.read_text(encoding="utf-8") == "new\nother\n"
    for arguments, error in [
        ({"old_text": "", "new_text": "overwrite"}, "non-empty"),
        ({"old_text": "e", "new_text": "x"}, "matched 2 locations"),
        ({"old_text": "new", "new_text": "x", "replace_all": "false"}, "boolean"),
    ]:
        with pytest.raises(ValueError, match=error):
            apply_patch({"path": "file.txt", **arguments}, tmp_path)
        assert target.read_text(encoding="utf-8") == "new\nother\n"


def test_create_existing_file_reports_how_to_edit_without_overwriting(tmp_path: Path):
    target = tmp_path / "file.txt"
    target.write_text("original", encoding="utf-8")
    with pytest.raises(FileExistsError, match="read_file, then apply_patch"):
        create_file({"path": "file.txt", "content": "overwrite"}, tmp_path)
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("name, arguments", [
    ("run_command", {"command": "echo test", "network": "false"}),
    ("run_command", {"command": "echo test", "timeout_seconds": -1}),
    ("create_file", {"path": "file.txt"}),
    ("apply_patch", {"path": "file.txt", "old_text": "old", "new_text": 2}),
    ("read_file", {"path": "file.txt", "start_line": True}),
    ("read_file", {"path": "file.txt", "unexpected": "value"}),
])
async def test_invalid_arguments_rejected_before_read_or_execution(tmp_path: Path, name, arguments):
    service = ToolService(None, ArtifactStore(tmp_path / "artifacts"), tmp_path / "worker.py")
    result = await service.execute("session", str(tmp_path), name, arguments)
    assert result.is_error is True
    assert "Invalid tool arguments" in result.output
    assert "Correct the arguments and retry" in result.output
    assert not (tmp_path / "file.txt").exists()


def test_file_tool_policy_requires_review_for_delete_and_move(tmp_path: Path):
    source = tmp_path / "source.txt"
    source.write_text("text", encoding="utf-8")
    policy = PolicyEngine()
    assert policy.evaluate("create_file", {"path": "new.txt"}, str(tmp_path)).decision == RiskLevel.ALLOW
    assert policy.evaluate("delete_file", {"path": "source.txt"}, str(tmp_path)).decision == RiskLevel.ASK
    assert policy.evaluate(
        "move_file", {"source_path": "source.txt", "destination_path": "renamed.txt"}, str(tmp_path)
    ).decision == RiskLevel.ASK
    external = policy.evaluate(
        "move_file", {"source_path": "source.txt", "destination_path": "../outside.txt"}, str(tmp_path)
    )
    assert external.decision == RiskLevel.ASK
    assert "external_file_write" in external.capabilities


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
