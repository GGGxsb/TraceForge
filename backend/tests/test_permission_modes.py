from __future__ import annotations

import os
from pathlib import Path

import pytest

from traceforge.artifacts import ArtifactStore
from traceforge.models import PermissionMode
from traceforge.tools import ToolService


class UnavailableSandbox:
    async def inspect(self):
        raise AssertionError("sandbox should not be used in full access mode")

    async def execute(self, request):
        raise AssertionError("sandbox should not be used in full access mode")


@pytest.mark.asyncio
async def test_full_access_uses_host_for_external_files_and_commands(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("external content", encoding="utf-8")
    worker = Path(__file__).resolve().parents[2] / "infra" / "runner" / "tool_worker.py"
    tools = ToolService(UnavailableSandbox(), ArtifactStore(tmp_path / "artifacts"), worker)

    limited = await tools.execute("session", str(workspace), "read_file", {"path": str(outside)})
    assert limited.is_error is True

    read = await tools.execute("session", str(workspace), "read_file", {"path": str(outside)},
                               PermissionMode.FULL_ACCESS)
    assert read.is_error is False
    assert "external content" in read.output

    created = tmp_path / "created-outside.txt"
    write = await tools.execute("session", str(workspace), "create_file",
                                {"path": str(created), "content": "created on host"},
                                PermissionMode.FULL_ACCESS)
    assert write.is_error is False, write.output
    assert write.metadata["sandbox"]["backend"] == "host"
    assert created.read_text(encoding="utf-8") == "created on host"

    command = "Write-Output 'host-ready'" if os.name == "nt" else "printf host-ready"
    run = await tools.execute("session", str(workspace), "run_command", {"command": command},
                              PermissionMode.FULL_ACCESS)
    assert run.is_error is False, run.output
    assert "host-ready" in run.output
    assert run.metadata["sandbox"]["backend"] == "host"
