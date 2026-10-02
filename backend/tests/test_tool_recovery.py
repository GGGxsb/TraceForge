"""Opt-in integration checks against the real local Docker runner."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from traceforge.artifacts import ArtifactStore
from traceforge.sandbox import DockerSandbox
from traceforge.tools import ToolService


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("TRACEFORGE_TEST_DOCKER") != "1", reason="Requires local Docker runner")
async def test_real_docker_git_and_file_error_recovery_across_fresh_containers(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
    sandbox = DockerSandbox("traceforge-runner:local")
    capabilities = await sandbox.inspect()
    assert capabilities.ready, capabilities.reason
    worker = Path(__file__).resolve().parents[2] / "infra" / "runner" / "tool_worker.py"
    tools = ToolService(sandbox, ArtifactStore(tmp_path / "artifacts"), worker)

    created = await tools.execute("session", str(workspace), "create_file",
                                  {"path": "app.py", "content": "value = 1\n"})
    assert not created.is_error, created.output
    duplicate = await tools.execute("session", str(workspace), "create_file",
                                    {"path": "app.py", "content": "overwrite"})
    assert duplicate.is_error
    assert "read_file, then apply_patch" in duplicate.output
    assert (workspace / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    patched = await tools.execute("session", str(workspace), "apply_patch",
                                  {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"})
    assert not patched.is_error, patched.output
    for _ in range(2):
        result = await tools.execute("session", str(workspace), "run_command", {
            "command": "git status --short && git diff --no-ext-diff && python3 -c 'from app import value; assert value == 2'",
        })
        assert not result.is_error, result.output
        assert "app.py" in result.output
        assert result.metadata["sandbox"]["profile"]["git_safe_directory"] == "/workspace"
        assert result.metadata["sandbox"]["profile"]["network"] is False
