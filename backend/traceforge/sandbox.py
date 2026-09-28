from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Protocol

from .models import ExecutionRequest, ExecutionResult, SandboxCapabilities, utc_now


class SandboxExecutor(Protocol):
    async def inspect(self) -> SandboxCapabilities: ...

    async def execute(self, request: ExecutionRequest) -> ExecutionResult: ...

    async def cancel(self, execution_id: str) -> None: ...


async def _run_capture(args: list[str], *, timeout: float = 10) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124, "", "Timed out"
    return process.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


class DockerSandbox:
    def __init__(self, image: str) -> None:
        self.image = image
        self._running: dict[str, str] = {}

    async def inspect(self) -> SandboxCapabilities:
        if not shutil.which("docker"):
            return SandboxCapabilities(backend="docker", ready=False, reason="Docker CLI 未安装")
        code, stdout, stderr = await _run_capture(
            ["docker", "info", "--format", "{{.OSType}}|{{.ServerVersion}}"]
        )
        if code != 0:
            return SandboxCapabilities(
                backend="docker",
                ready=False,
                reason=f"Docker Desktop 未运行或 Linux Engine 不可用：{stderr.strip()}",
            )
        if not stdout.strip().lower().startswith("linux|"):
            return SandboxCapabilities(
                backend="docker",
                ready=False,
                reason="Docker Desktop 当前不是 Linux/WSL2 Engine，请切换到 Linux containers",
            )
        code, _, _ = await _run_capture(["docker", "image", "inspect", self.image])
        if code != 0:
            return SandboxCapabilities(
                backend="docker",
                ready=False,
                reason=f"Runner 镜像不存在：{self.image}。请运行 docker build -t {self.image} infra/runner",
            )
        return SandboxCapabilities(
            backend="docker",
            ready=True,
            supports_network_toggle=True,
            supports_resource_limits=True,
        )

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        started = utc_now()
        name = f"traceforge-{request.execution_id.replace('_', '-')[:48]}"
        self._running[request.execution_id] = name
        args = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--workdir",
            "/workspace",
            "--user",
            "10001:10001",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=128m",
            "--tmpfs",
            "/home/runner:rw,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=128m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "256",
            "--memory",
            "1g",
            "--cpus",
            "2",
            "--network",
            "bridge" if request.network else "none",
            "--mount",
            f"type=bind,source={Path(request.workspace).resolve()},target=/workspace",
        ]
        for key, value in request.env.items():
            args.extend(["--env", f"{key}={value}"])
        args.extend([self.image, "bash", "-lc", request.command])
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        cancelled = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=request.timeout_seconds)
        except TimeoutError:
            timed_out = True
            await _run_capture(["docker", "rm", "-f", name])
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            cancelled = True
            await _run_capture(["docker", "rm", "-f", name])
            process.kill()
            stdout, stderr = await process.communicate()
            raise
        finally:
            self._running.pop(request.execution_id, None)
        return ExecutionResult(
            execution_id=request.execution_id,
            backend="docker",
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            exit_code=process.returncode,
            timed_out=timed_out,
            cancelled=cancelled,
            started_at=started,
            ended_at=utc_now(),
            profile={
                "network": request.network,
                "workspace_mount": "/workspace",
                "read_only_root": True,
                "capabilities": "dropped-all",
                "memory": "1g",
                "cpus": 2,
                "pids": 256,
            },
        )

    async def cancel(self, execution_id: str) -> None:
        name = self._running.get(execution_id)
        if name:
            await _run_capture(["docker", "rm", "-f", name])


class BubblewrapSandbox:
    def __init__(self) -> None:
        self._running: dict[str, asyncio.subprocess.Process] = {}

    async def inspect(self) -> SandboxCapabilities:
        binary = shutil.which("bwrap")
        if not binary:
            return SandboxCapabilities(backend="bubblewrap", ready=False, reason="Bubblewrap (bwrap) 未安装")
        code, _, stderr = await _run_capture(
            [
                binary,
                "--die-with-parent",
                "--unshare-all",
                "--ro-bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "/bin/true",
            ]
        )
        return SandboxCapabilities(
            backend="bubblewrap",
            ready=code == 0,
            reason="" if code == 0 else f"Bubblewrap namespace probe failed: {stderr.strip()}",
            supports_network_toggle=True,
            supports_resource_limits=True,
        )

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        started = utc_now()
        workspace = str(Path(request.workspace).resolve())
        args = [
            "bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--ro-bind",
            "/",
            "/",
            "--bind",
            workspace,
            "/workspace",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/tmp/home",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--chdir",
            "/workspace",
        ]
        if request.network:
            args.append("--share-net")
        limited_command = (
            "ulimit -v 1048576; ulimit -u 256; "
            f"exec /bin/bash -lc {shlex.quote(request.command)}"
        )
        args.extend(["/bin/bash", "-lc", limited_command])
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                "PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "LANG": "C.UTF-8",
                "HOME": "/tmp/home",
                **request.env,
            },
        )
        self._running[request.execution_id] = process
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=request.timeout_seconds)
        except TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            process.kill()
            await process.communicate()
            raise
        finally:
            self._running.pop(request.execution_id, None)
        return ExecutionResult(
            execution_id=request.execution_id,
            backend="bubblewrap",
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
            exit_code=process.returncode,
            timed_out=timed_out,
            started_at=started,
            ended_at=utc_now(),
            profile={
                "network": request.network,
                "workspace_mount": "/workspace",
                "read_only_root": True,
                "namespaces": ["mount", "pid", "ipc", "uts", "network"],
            },
        )

    async def cancel(self, execution_id: str) -> None:
        process = self._running.get(execution_id)
        if process and process.returncode is None:
            process.kill()


def create_platform_sandbox(docker_image: str) -> SandboxExecutor:
    if sys.platform == "win32":
        return DockerSandbox(docker_image)
    if sys.platform.startswith("linux"):
        return BubblewrapSandbox()
    return DockerSandbox(docker_image)
