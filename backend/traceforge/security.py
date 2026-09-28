from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .models import PolicyResult, RiskLevel
from .tool_plugins import ToolPluginRegistry


RISK_ORDER = {RiskLevel.ALLOW: 0, RiskLevel.ASK: 1, RiskLevel.DENY: 2}


class PathGuard:
    SENSITIVE_PARTS = {".ssh", ".aws", ".azure", ".gnupg", ".kube", "credentials"}
    SENSITIVE_FILES = {".env", "id_rsa", "id_ed25519", "credentials.json"}

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve(strict=True)

    def resolve(self, raw: str, *, allow_missing: bool = False) -> Path:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        if allow_missing and not candidate.exists():
            tail: list[str] = []
            parent = candidate
            while not parent.exists() and parent != parent.parent:
                tail.append(parent.name)
                parent = parent.parent
            resolved = parent.resolve(strict=True)
            for part in reversed(tail):
                resolved /= part
        else:
            resolved = candidate.resolve(strict=True)
        try:
            relative = resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise PermissionError(f"Path escapes workspace: {raw}") from exc
        if any(part.lower() in self.SENSITIVE_PARTS for part in relative.parts):
            raise PermissionError(f"Sensitive path is blocked: {raw}")
        if relative.name.lower() in self.SENSITIVE_FILES or relative.suffix.lower() in {".pem", ".p12", ".pfx"}:
            raise PermissionError(f"Sensitive file is blocked: {raw}")
        return resolved


class PolicyEngine:
    version = "2026-09-v1"
    DENY_COMMANDS = re.compile(r"(^|[;&|]\s*)(sudo|su|mount|umount|docker|podman|nsenter)\b", re.I)
    DESTRUCTIVE = re.compile(
        r"\b(rm\s+(-[^\s]*r[^\s]*f|--recursive)|git\s+(reset\s+--hard|clean\s+-|push)|chmod|chown|del\s+/[sq])\b",
        re.I,
    )
    INSTALLERS = re.compile(r"\b(pip|pip3|npm|pnpm|yarn|bun|apt|apt-get|dnf|yum|cargo)\s+(install|add|update)\b", re.I)
    NETWORK = re.compile(r"\b(curl|wget|Invoke-WebRequest|git\s+(clone|fetch|pull|push))\b", re.I)
    GIT_REVIEW = re.compile(r"\bgit\s+(commit|push|tag)\b", re.I)
    CREDENTIALS = re.compile(
        r"(~[/\\])?\.(ssh|aws|azure|gnupg|kube)|credentials|id_rsa|id_ed25519|"
        r"(^|[\s/\\])\.env($|[\s/\\])",
        re.I,
    )
    COMPOUND = re.compile(r"&&|\|\||;|`|\$\(")

    def __init__(self, plugins: ToolPluginRegistry | None = None) -> None:
        self.plugins = plugins

    def evaluate(self, tool_name: str, arguments: dict[str, Any], workspace: str) -> PolicyResult:
        capabilities: set[str] = set()
        reasons: list[str] = []
        affected: list[str] = []
        decision = RiskLevel.ALLOW
        guard = PathGuard(workspace)

        def escalate(level: RiskLevel, reason: str) -> None:
            nonlocal decision
            if RISK_ORDER[level] > RISK_ORDER[decision]:
                decision = level
            reasons.append(reason)

        # read_skill.path is relative to the selected skill directory, not the workspace.
        # SkillCatalog applies its own path guard after resolving the skill name.
        path_keys = ("cwd", "source_path", "destination_path") if tool_name == "read_skill" else (
            "path", "cwd", "source_path", "destination_path"
        )
        for key in path_keys:
            raw = arguments.get(key)
            if not isinstance(raw, str) or not raw:
                continue
            try:
                resolved = guard.resolve(
                    raw,
                    allow_missing=(tool_name in {"apply_patch", "create_file"} and key == "path")
                    or (tool_name == "move_file" and key == "destination_path"),
                )
                affected.append(str(resolved))
            except (PermissionError, FileNotFoundError) as exc:
                capabilities.add("workspace_escape")
                escalate(RiskLevel.DENY, str(exc))

        if tool_name in {"list_files", "search_code", "read_file", "git_status", "git_diff", "read_skill"}:
            capabilities.add("filesystem_read")
        elif tool_name in {"apply_patch", "create_file"}:
            capabilities.add("filesystem_write")
        elif tool_name in {"delete_file", "move_file"}:
            capabilities.update({"filesystem_write", "filesystem_delete"})
            escalate(RiskLevel.ASK, "Deleting or moving a file requires approval")
        elif tool_name == "run_command" or (self.plugins and self.plugins.get(tool_name)):
            capabilities.add("process_execute")
            plugin = self.plugins.get(tool_name) if self.plugins else None
            command = plugin.command(arguments) if plugin else str(arguments.get("command", "")).strip()
            if plugin:
                capabilities.add("registered_tool")
                escalate(RiskLevel.ASK, "Registered sandbox tools require approval")
            if self.CREDENTIALS.search(command):
                capabilities.add("credential_access")
                escalate(RiskLevel.DENY, "Command references a credential path")
            if self.DENY_COMMANDS.search(command):
                capabilities.add("privilege_or_host_control")
                escalate(RiskLevel.DENY, "Command attempts privilege, device, or container control")
            if self.DESTRUCTIVE.search(command):
                capabilities.add("destructive_operation")
                escalate(RiskLevel.ASK, "Command can delete data or publish/reset Git state")
            if self.GIT_REVIEW.search(command):
                capabilities.add("git_write")
                escalate(RiskLevel.ASK, "Git commit, tag, or push requires approval")
            if self.INSTALLERS.search(command):
                capabilities.add("dependency_install")
                escalate(RiskLevel.ASK, "Dependency installation requires approval")
            if self.NETWORK.search(command) or bool(arguments.get("network")) or bool(plugin and plugin.network):
                capabilities.add("network_access")
                escalate(RiskLevel.ASK, "Network access requires approval")
            if self.COMPOUND.search(command):
                capabilities.add("compound_shell")
                escalate(RiskLevel.ASK, "Compound shell command requires review")
        else:
            escalate(RiskLevel.DENY, f"Unknown tool: {tool_name}")

        if not reasons:
            reasons.append("Operation is within the workspace and matches the default low-risk policy")
        fingerprint = hashlib.sha256(
            json.dumps(
                {"tool": tool_name, "capabilities": sorted(capabilities),
                 "network": "network_access" in capabilities},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return PolicyResult(
            decision=decision,
            capabilities=sorted(capabilities),
            reasons=reasons,
            affected_paths=affected,
            network="network_access" in capabilities,
            fingerprint=fingerprint,
        )
