import base64
import json
from pathlib import Path

import pytest

from traceforge.artifacts import ArtifactStore
from traceforge.models import ExecutionRequest, ExecutionResult, SandboxCapabilities, utc_now
from traceforge.sandbox import BubblewrapSandbox, DockerSandbox
from traceforge.tools import ToolService


class FakeProcess:
    returncode = 0

    async def communicate(self):
        return b"ok", b""

    def kill(self):
        self.returncode = -9


class CapturingSandbox:
    def __init__(self, backend: str) -> None:
        self.backend = backend
        self.request = None

    async def inspect(self):
        return SandboxCapabilities(backend=self.backend, ready=True)

    async def execute(self, request):
        self.request = request
        return ExecutionResult(
            execution_id=request.execution_id,
            backend=self.backend,
            exit_code=0,
            started_at=utc_now(),
            ended_at=utc_now(),
        )

    async def cancel(self, execution_id):
        return None


@pytest.mark.asyncio
async def test_docker_execution_uses_hardened_profile(tmp_path: Path, monkeypatch):
    captured: list[tuple[str, ...]] = []

    async def fake_exec(*args, **kwargs):
        captured.append(args)
        return FakeProcess()

    monkeypatch.setattr("traceforge.sandbox.asyncio.create_subprocess_exec", fake_exec)
    result = await DockerSandbox("traceforge-runner:local").execute(
        ExecutionRequest(workspace=str(tmp_path), command="pytest", network=False)
    )
    args = captured[0]
    assert "--read-only" in args
    assert ("--cap-drop", "ALL") == args[args.index("--cap-drop") : args.index("--cap-drop") + 2]
    assert ("--network", "none") == args[args.index("--network") : args.index("--network") + 2]
    assert "no-new-privileges:true" in args
    assert not any("docker.sock" in value for value in args)
    assert result.profile["read_only_root"] is True


@pytest.mark.asyncio
async def test_bubblewrap_execution_unshares_namespaces_and_applies_limits(tmp_path: Path, monkeypatch):
    captured: list[tuple[str, ...]] = []

    async def fake_exec(*args, **kwargs):
        captured.append(args)
        return FakeProcess()

    monkeypatch.setattr("traceforge.sandbox.asyncio.create_subprocess_exec", fake_exec)
    result = await BubblewrapSandbox().execute(
        ExecutionRequest(workspace=str(tmp_path), command="pytest", network=False)
    )
    args = captured[0]
    assert "--unshare-all" in args
    assert "--share-net" not in args
    assert "--ro-bind" in args
    assert "--tmpfs" in args
    assert "ulimit -v 1048576" in args[-1]
    assert "ulimit -u 256" in args[-1]
    assert result.profile["read_only_root"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "expected_worker"),
    [("docker", "/opt/traceforge/tool_worker.py"), ("bubblewrap", "tool_worker.py")],
)
async def test_patch_worker_path_matches_sandbox_backend(tmp_path: Path, backend: str, expected_worker: str):
    target = tmp_path / "nested" / "file.txt"
    target.parent.mkdir()
    target.write_text("old", encoding="utf-8")
    sandbox = CapturingSandbox(backend)
    worker = tmp_path / "infra" / "tool_worker.py"
    service = ToolService(sandbox, ArtifactStore(tmp_path / "artifacts"), worker)
    result = await service.execute(
        "session",
        str(tmp_path),
        "apply_patch",
        {"path": "nested/file.txt", "old_text": "old", "new_text": "new", "replace_all": False},
    )
    assert result.is_error is False
    assert expected_worker in sandbox.request.command
    encoded = sandbox.request.command.split()[-1]
    assert json.loads(base64.urlsafe_b64decode(encoded))["path"] == "nested/file.txt"
