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
async def test_subagent_sandbox_mounts_workspace_read_only_and_hides_linux_home(tmp_path: Path, monkeypatch):
    captured = []

    async def fake_exec(*args, **kwargs):
        captured.append(args)
        return FakeProcess()

    monkeypatch.setattr("traceforge.sandbox.asyncio.create_subprocess_exec", fake_exec)
    request = ExecutionRequest(workspace=str(tmp_path), command="python3 -c 'print(1)'", workspace_read_only=True)
    await DockerSandbox("traceforge-runner:local").execute(request)
    docker_args = captured.pop()
    assert docker_args[docker_args.index("--mount") + 1].endswith(",readonly")
    await BubblewrapSandbox().execute(request)
    args = captured.pop()
    assert "--bind" not in args
    assert not any(args[index:index + 3] == ("--ro-bind", "/", "/") for index in range(len(args)))
    assert "/home" not in args and "/root" not in args


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
async def test_every_docker_execution_trusts_only_current_mount(tmp_path: Path, monkeypatch):
    captured = []

    async def fake_exec(*args, **kwargs):
        captured.append(args)
        return FakeProcess()

    monkeypatch.setattr("traceforge.sandbox.asyncio.create_subprocess_exec", fake_exec)
    sandbox = DockerSandbox("traceforge-runner:local")
    for _ in range(2):
        await sandbox.execute(ExecutionRequest(
            workspace=str(tmp_path), command="git status --short",
            env={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_VALUE_0": "*"},
        ))
    for args in captured:
        env = dict(args[index + 1].split("=", 1) for index, value in enumerate(args) if value == "--env")
        assert env["GIT_CONFIG_COUNT"] == "2"
        assert env["GIT_CONFIG_VALUE_0"] == ""
        assert env["GIT_CONFIG_VALUE_1"] == "/workspace"
        assert env["GIT_CONFIG_KEY_0"] == env["GIT_CONFIG_KEY_1"] == "safe.directory"
        assert "*" not in env.values()


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
        {"path": "nested/file.txt", "old_text": "old", "new_text": "new"},
    )
    assert result.is_error is False
    assert expected_worker in sandbox.request.command
    encoded = sandbox.request.command.split()[-1]
    assert json.loads(base64.urlsafe_b64decode(encoded))["path"] == "nested/file.txt"
    assert json.loads(base64.urlsafe_b64decode(encoded))["replace_all"] is False
