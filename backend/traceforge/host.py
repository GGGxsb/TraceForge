from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
from pathlib import Path

from .models import ExecutionRequest, ExecutionResult, SandboxCapabilities, utc_now


class HostExecutor:
    """Explicit full-access execution as the current OS user."""

    async def inspect(self) -> SandboxCapabilities:
        return SandboxCapabilities(backend="host", ready=True)

    async def execute(
        self, request: ExecutionRequest, *, argv: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> ExecutionResult:
        if argv is None:
            if os.name == "nt":
                shell = shutil.which("pwsh") or shutil.which("powershell.exe")
                if shell is None:
                    raise RuntimeError("PowerShell is unavailable on this host")
                argv = [shell, "-NoProfile", "-NonInteractive", "-Command", request.command]
            else:
                shell = shutil.which("bash")
                if shell is None:
                    raise RuntimeError("Bash is unavailable on this host")
                argv = [shell, "-lc", request.command]
        env = {key: value for key, value in os.environ.items()
               if key.upper() not in {"OPENAI_API_KEY", "TRACEFORGE_API_KEY"}}
        env.update(request.env)
        if extra_env:
            env.update(extra_env)
        started = utc_now()
        options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                   else {"start_new_session": True})
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(Path(request.workspace).resolve()), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=request.timeout_seconds)
        except TimeoutError:
            timed_out = True
            await self._stop_tree(process)
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            await self._stop_tree(process)
            await process.communicate()
            raise
        return ExecutionResult(
            execution_id=request.execution_id, backend="host",
            stdout=stdout.decode(errors="replace"), stderr=stderr.decode(errors="replace"),
            exit_code=process.returncode, timed_out=timed_out,
            started_at=started, ended_at=utc_now(),
            profile={"mode": "full_access", "cwd": request.workspace, "network": "host"},
        )

    @staticmethod
    async def _stop_tree(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(killer.wait(), timeout=8)
            except TimeoutError:
                killer.kill()
                await killer.wait()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
