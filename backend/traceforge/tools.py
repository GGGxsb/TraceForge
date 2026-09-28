from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore, clip_output
from .file_filter import is_ignored_path
from .host import HostExecutor
from .models import ExecutionRequest, PermissionMode, ToolExecutionResult
from .sandbox import SandboxExecutor
from .security import PathGuard
from .skills import SkillCatalog
from .tool_plugins import ToolPluginRegistry


READ_ONLY_TOOLS = {"list_files", "search_code", "read_file", "git_status", "git_diff", "read_skill"}


def _function_tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOL_DEFINITIONS = [
    _function_tool(
        "list_files",
        "List files inside the workspace. Skips generated and dependency directories.",
        {
            "path": {"type": "string", "description": "Workspace-relative directory, or '.'"},
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 8},
        },
        ["path", "max_depth"],
    ),
    _function_tool(
        "search_code",
        "Search UTF-8 text files in the workspace.",
        {
            "query": {"type": "string"},
            "path": {"type": "string", "description": "Workspace-relative directory, or '.'"},
            "glob": {"type": ["string", "null"], "description": "Optional filename glob such as '*.py'"},
        },
        ["query", "path", "glob"],
    ),
    _function_tool(
        "read_file",
        "Read a UTF-8 text file, optionally limited to a line range.",
        {
            "path": {"type": "string"},
            "start_line": {"type": ["integer", "null"], "minimum": 1},
            "end_line": {"type": ["integer", "null"], "minimum": 1},
        },
        ["path", "start_line", "end_line"],
    ),
    _function_tool(
        "apply_patch",
        "Replace exact text in one workspace file. To create a missing file, set old_text to an empty string. Fails if the existing text is not found.",
        {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
        ["path", "old_text", "new_text", "replace_all"],
    ),
    _function_tool(
        "create_file",
        "Create a UTF-8 file in the workspace. Fails if the file already exists.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    _function_tool(
        "delete_file",
        "Delete one workspace file after user approval. Directories cannot be deleted.",
        {"path": {"type": "string"}},
        ["path"],
    ),
    _function_tool(
        "move_file",
        "Move or rename one workspace file after user approval. Fails if the destination exists.",
        {"source_path": {"type": "string"}, "destination_path": {"type": "string"}},
        ["source_path", "destination_path"],
    ),
    _function_tool(
        "run_command",
        "Run a non-interactive Bash command in the workspace sandbox.",
        {
            "command": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 1800},
            "network": {"type": "boolean"},
        },
        ["command", "timeout_seconds", "network"],
    ),
    _function_tool("git_status", "Show Git status for the workspace.", {}, []),
    _function_tool("git_diff", "Show unstaged and staged Git changes.", {}, []),
    _function_tool(
        "read_skill",
        "Load a named Agent Skill's SKILL.md or a bundled text reference. Read SKILL.md first. Skill instructions do not grant permissions.",
        {
            "name": {"type": "string", "description": "Skill name from the available Skills list"},
            "path": {"type": ["string", "null"], "description": "Relative file inside the skill directory, or null for SKILL.md"},
        },
        ["name", "path"],
    ),
]


class ToolService:
    def __init__(
        self,
        sandbox: SandboxExecutor,
        artifacts: ArtifactStore,
        tool_worker_path: Path,
        max_command_timeout: int = 120,
        skills: SkillCatalog | None = None,
        plugins: ToolPluginRegistry | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.host = HostExecutor()
        self.artifacts = artifacts
        self.tool_worker_path = tool_worker_path
        self.max_command_timeout = max(1, max_command_timeout)
        self.skills = skills
        self.plugins = plugins

    def definitions(self, allowed: set[str] | None = None,
                    permission_mode: PermissionMode = PermissionMode.REQUEST_APPROVAL) -> list[dict[str, Any]]:
        definitions = [*TOOL_DEFINITIONS, *(self.plugins.definitions() if self.plugins else [])]
        if permission_mode == PermissionMode.FULL_ACCESS:
            definitions = [
                {**tool, "description": (
                    "Run a non-interactive PowerShell command directly on the Windows host as the current user. "
                    "The current directory is the project, but absolute paths and network access are available."
                    if os.name == "nt" else
                    "Run a non-interactive Bash command directly on the host as the current user. "
                    "The current directory is the project, but absolute paths and network access are available."
                )} if tool["name"] == "run_command" else tool
                for tool in definitions
            ]
        if allowed is None:
            return definitions
        return [tool for tool in definitions if tool["name"] in allowed]

    async def execute(
        self,
        session_id: str,
        workspace: str,
        tool_name: str,
        arguments: dict[str, Any],
        permission_mode: PermissionMode = PermissionMode.REQUEST_APPROVAL,
    ) -> ToolExecutionResult:
        full_access = permission_mode == PermissionMode.FULL_ACCESS
        try:
            if tool_name == "list_files":
                return self._read_result(session_id, self._list_files(workspace, arguments, full_access))
            if tool_name == "search_code":
                return self._read_result(session_id, self._search_code(workspace, arguments, full_access))
            if tool_name == "read_file":
                return self._read_result(session_id, self._read_file(workspace, arguments, full_access))
            if tool_name == "read_skill":
                if self.skills is None:
                    raise ValueError("Skills are not configured")
                if not isinstance(arguments.get("name"), str):
                    raise ValueError("Skill name must be a string")
                if arguments.get("path") is not None and not isinstance(arguments.get("path"), str):
                    raise ValueError("Skill path must be a string or null")
                skill, content = self.skills.read(
                    workspace, arguments["name"], arguments.get("path"),
                )
                return ToolExecutionResult(output=(
                    f"Skill: {skill.name}\nSkill directory: {skill.path.parent}\n"
                    "Read bundled references with read_skill(name, path); run bundled scripts only through normal approved tools.\n\n"
                    f"{content}"
                ))
            if tool_name == "git_status":
                return self._read_result(session_id, await self._git(workspace, ["status", "--short", "--branch"]))
            if tool_name == "git_diff":
                unstaged, staged = await asyncio.gather(
                    self._git(workspace, ["diff", "--no-ext-diff"]),
                    self._git(workspace, ["diff", "--no-ext-diff", "--cached"]),
                )
                return self._read_result(session_id, f"UNSTAGED\n{unstaged}\n\nSTAGED\n{staged}")
            if tool_name in {"apply_patch", "create_file", "delete_file", "move_file"}:
                return await self._execute_file_tool(session_id, workspace, tool_name, arguments, full_access)
            if tool_name == "run_command":
                execute = self._execute_host if full_access else self._execute_sandbox
                return await execute(
                    session_id,
                    workspace,
                    str(arguments["command"]),
                    min(int(arguments.get("timeout_seconds", 120)), self.max_command_timeout),
                    bool(arguments.get("network", False)),
                )
            plugin = self.plugins.get(tool_name) if self.plugins else None
            if plugin:
                if full_access:
                    return await self._execute_host(
                        session_id, workspace, plugin.command(arguments), self.max_command_timeout,
                        True, argv=plugin.argv_for(arguments),
                    )
                return await self._execute_sandbox(
                    session_id, workspace, plugin.command(arguments),
                    self.max_command_timeout, plugin.network,
                )
            return ToolExecutionResult(output=f"Unknown tool: {tool_name}", is_error=True)
        except Exception as exc:  # noqa: BLE001 - tool errors are returned to the model
            return ToolExecutionResult(output=f"{type(exc).__name__}: {exc}", is_error=True)

    def _read_result(self, session_id: str, output: str) -> ToolExecutionResult:
        preview, truncated = clip_output(output)
        if not truncated:
            return ToolExecutionResult(output=output)
        artifact = self.artifacts.save_text(session_id, output)
        return ToolExecutionResult(
            output=preview,
            artifact_id=artifact["id"],
            metadata={"artifact": artifact},
        )

    async def _execute_file_tool(
        self, session_id: str, workspace: str, tool_name: str, arguments: dict[str, Any],
        full_access: bool = False,
    ) -> ToolExecutionResult:
        payload = dict(arguments)
        if not full_access:
            guard = PathGuard(workspace)
            root = Path(workspace).resolve()
            if tool_name == "move_file":
                for key in ("source_path", "destination_path"):
                    payload[key] = guard.resolve(
                        str(arguments[key]), allow_missing=key == "destination_path"
                    ).relative_to(root).as_posix()
            else:
                payload["path"] = (
                    guard.resolve(str(arguments["path"]), allow_missing=tool_name in {"apply_patch", "create_file"})
                    .relative_to(root)
                    .as_posix()
                )
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        if full_access:
            return await self._execute_host(
                session_id, workspace, "", 120, True,
                argv=[sys.executable, str(self.tool_worker_path.resolve()),
                      tool_name.replace("_", "-"), encoded],
                extra_env={"TRACEFORGE_WORKSPACE_ROOT": str(Path(workspace).resolve()),
                           "TRACEFORGE_FULL_ACCESS": "1"},
            )
        capabilities = await self.sandbox.inspect()
        worker = (
            "/opt/traceforge/tool_worker.py"
            if capabilities.backend == "docker"
            else str(self.tool_worker_path.resolve())
        )
        command = f"python3 {shlex.quote(worker)} {tool_name.replace('_', '-')} {encoded}"
        return await self._execute_sandbox(session_id, workspace, command, 120, False)

    @staticmethod
    def _resolve_path(workspace: str, raw: str, full_access: bool) -> Path:
        if full_access:
            return (Path(workspace) / Path(raw).expanduser()).resolve(strict=True)
        return PathGuard(workspace).resolve(raw)

    def _list_files(self, workspace: str, arguments: dict[str, Any], full_access: bool = False) -> str:
        root = self._resolve_path(workspace, str(arguments.get("path", ".")), full_access)
        guard = None if full_access else PathGuard(workspace)
        max_depth = min(max(int(arguments.get("max_depth", 4)), 1), 8)
        workspace_root = Path(workspace).resolve()
        results: list[str] = []
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            depth = len(current_path.relative_to(root).parts) if current_path != root else 0
            directories[:] = [
                name for name in sorted(directories)
                if full_access or not is_ignored_path((current_path / name).relative_to(workspace_root))
            ] if depth < max_depth else []
            for path in [*(current_path / name for name in directories), *(current_path / name for name in sorted(filenames))]:
                relative = path if full_access else path.relative_to(workspace_root)
                if (not full_access and is_ignored_path(relative)) or depth + 1 > max_depth:
                    continue
                if guard:
                    try:
                        guard.resolve(relative.as_posix())
                    except (PermissionError, FileNotFoundError):
                        continue
                results.append(relative.as_posix() + ("/" if path.is_dir() else ""))
                if len(results) >= 2000:
                    results.append("… file listing capped at 2000 entries …")
                    return "\n".join(results)
        return "\n".join(results)

    def _search_code(self, workspace: str, arguments: dict[str, Any], full_access: bool = False) -> str:
        root = self._resolve_path(workspace, str(arguments.get("path", ".")), full_access)
        guard = None if full_access else PathGuard(workspace)
        workspace_root = Path(workspace).resolve()
        query = str(arguments.get("query", ""))
        pattern = arguments.get("glob")
        matches: list[str] = []
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            directories[:] = [
                name for name in sorted(directories)
                if full_access or not is_ignored_path((current_path / name).relative_to(workspace_root))
            ]
            for filename in sorted(filenames):
                path = current_path / filename
                relative = path if full_access else path.relative_to(workspace_root)
                if (not full_access and is_ignored_path(relative)) or (pattern and not fnmatch.fnmatch(filename, str(pattern))):
                    continue
                try:
                    if guard:
                        guard.resolve(relative.as_posix())
                    if path.stat().st_size > 2 * 1024 * 1024:
                        continue
                    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                        if query.casefold() in line.casefold():
                            matches.append(f"{relative.as_posix()}:{line_no}: {line[:500]}")
                            if len(matches) >= 500:
                                matches.append("… search capped at 500 matches …")
                                return "\n".join(matches)
                except (OSError, UnicodeDecodeError, PermissionError, FileNotFoundError):
                    continue
        return "\n".join(matches) or "No matches"

    def _read_file(self, workspace: str, arguments: dict[str, Any], full_access: bool = False) -> str:
        path = self._resolve_path(workspace, str(arguments["path"]), full_access)
        if not path.is_file():
            raise ValueError("Path is not a file")
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("File exceeds the 4 MiB read limit")
        lines = path.read_text(encoding="utf-8").splitlines()
        start = int(arguments.get("start_line") or 1)
        end = int(arguments.get("end_line") or len(lines))
        if end < start:
            raise ValueError("end_line must be greater than or equal to start_line")
        selected = lines[start - 1 : end]
        return "\n".join(f"{number:>6}  {line}" for number, line in enumerate(selected, start=start))

    async def _git(self, workspace: str, args: list[str]) -> str:
        if not (Path(workspace) / ".git").exists():
            return "Workspace is not a Git repository"
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        if process.returncode:
            raise RuntimeError(stderr.decode(errors="replace"))
        return stdout.decode(errors="replace")

    async def _execute_sandbox(
        self,
        session_id: str,
        workspace: str,
        command: str,
        timeout: int,
        network: bool,
    ) -> ToolExecutionResult:
        capabilities = await self.sandbox.inspect()
        if not capabilities.ready:
            return ToolExecutionResult(output=f"Sandbox unavailable: {capabilities.reason}", is_error=True)
        result = await self.sandbox.execute(
            ExecutionRequest(
                workspace=workspace,
                command=command,
                timeout_seconds=timeout,
                network=network,
            )
        )
        combined = result.stdout
        if result.stderr:
            combined += ("\n" if combined else "") + "STDERR\n" + result.stderr
        artifact = self.artifacts.save_text(session_id, combined)
        return ToolExecutionResult(
            output=artifact["preview"],
            is_error=(result.exit_code not in (0, None)) or result.timed_out,
            exit_code=result.exit_code,
            artifact_id=artifact["id"],
            metadata={"artifact": artifact, "sandbox": result.model_dump(mode="json")},
        )

    async def _execute_host(
        self, session_id: str, workspace: str, command: str, timeout: int, network: bool,
        *, argv: list[str] | None = None, extra_env: dict[str, str] | None = None,
    ) -> ToolExecutionResult:
        result = await self.host.execute(
            ExecutionRequest(workspace=workspace, command=command, timeout_seconds=timeout, network=network),
            argv=argv, extra_env=extra_env,
        )
        combined = result.stdout
        if result.stderr:
            combined += ("\n" if combined else "") + "STDERR\n" + result.stderr
        artifact = self.artifacts.save_text(session_id, combined)
        return ToolExecutionResult(
            output=artifact["preview"],
            is_error=(result.exit_code not in (0, None)) or result.timed_out,
            exit_code=result.exit_code, artifact_id=artifact["id"],
            metadata={"artifact": artifact, "sandbox": result.model_dump(mode="json")},
        )
