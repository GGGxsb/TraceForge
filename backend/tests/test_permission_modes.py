from __future__ import annotations

import os
from pathlib import Path

import pytest

from traceforge.artifacts import ArtifactStore
from traceforge.host import HostExecutor
from traceforge.models import ExecutionRequest, PermissionMode
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


@pytest.mark.asyncio
async def test_full_access_host_process_honors_timeout(tmp_path: Path):
    command = "Start-Sleep -Seconds 10" if os.name == "nt" else "sleep 10"
    result = await HostExecutor().execute(ExecutionRequest(
        workspace=str(tmp_path), command=command, timeout_seconds=1, network=True,
    ))
    assert result.timed_out is True
    assert result.backend == "host"


@pytest.mark.asyncio
async def test_scoped_external_edit_requires_exact_approved_path(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    worker = Path(__file__).resolve().parents[2] / "infra" / "runner" / "tool_worker.py"
    tools = ToolService(UnavailableSandbox(), ArtifactStore(tmp_path / "artifacts"), worker)
    target = tmp_path / "approved.txt"
    args = {"path": str(target), "content": "approved"}
    unapproved = await tools.execute("session", str(workspace), "create_file", args)
    assert unapproved.is_error is True
    assert not target.exists()
    wrong_scope = await tools.execute("session", str(workspace), "create_file", args,
                                      allowed_external_paths=[str(tmp_path / "other.txt")])
    assert wrong_scope.is_error is True
    assert not target.exists()
    approved = await tools.execute("session", str(workspace), "create_file", args,
                                   allowed_external_paths=[str(target)])
    assert approved.is_error is False, approved.output
    assert approved.metadata["sandbox"]["backend"] == "host"
    assert target.read_text(encoding="utf-8") == "approved"
