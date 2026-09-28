from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .security import PathGuard


MAX_SKILL_BYTES = 64 * 1024
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SKILL_COMMAND = re.compile(r"^/skill:([a-z0-9]+(?:-[a-z0-9]+)*)(?:\s+([\s\S]*))?$", re.I)


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    path: Path
    source: str
    disable_model_invocation: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "path": str(self.path),
            "source": self.source,
            "disable_model_invocation": self.disable_model_invocation,
        }


class SkillCatalog:
    """Discover Agent Skills without putting their full instructions in every prompt."""

    def __init__(self, config_dir: Path, home: Path | None = None) -> None:
        self.config_dir = config_dir
        self.home = home or Path.home()

    def _roots(self, workspace: str | Path) -> list[tuple[Path, str]]:
        current = Path(workspace).resolve(strict=True)
        roots: list[tuple[Path, str]] = []
        while True:
            roots.extend(((current / ".pi" / "skills", "project"), (current / ".agents" / "skills", "project")))
            if (current / ".git").exists() or current == current.parent:
                break
            current = current.parent
        roots.extend((
            (self.config_dir / "skills", "traceforge"),
            (self.home / ".pi" / "agent" / "skills", "global"),
            (self.home / ".agents" / "skills", "global"),
        ))
        return roots

    @staticmethod
    def _parse(path: Path, source: str) -> Skill:
        if path.stat().st_size > MAX_SKILL_BYTES:
            raise ValueError("SKILL.md exceeds 64 KiB")
        text = path.read_text(encoding="utf-8")
        match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", text, re.S)
        if not match:
            raise ValueError("SKILL.md needs YAML frontmatter")
        metadata = yaml.safe_load(match.group(1))
        if not isinstance(metadata, dict):
            raise ValueError("Skill frontmatter must be a mapping")
        name = metadata.get("name")
        description = metadata.get("description")
        if not isinstance(name, str) or len(name) > 64 or not SKILL_NAME.fullmatch(name):
            raise ValueError("Invalid skill name")
        if not isinstance(description, str) or not description.strip() or len(description) > 1024:
            raise ValueError("Invalid skill description")
        disabled = metadata.get("disable-model-invocation", False)
        if not isinstance(disabled, bool):
            raise ValueError("disable-model-invocation must be a boolean")
        return Skill(name, " ".join(description.split()), path, source, disabled)

    def discover(self, workspace: str | Path) -> tuple[list[Skill], list[str]]:
        found: dict[str, Skill] = {}
        warnings: list[str] = []
        seen_roots: set[Path] = set()
        for root, source in self._roots(workspace):
            if not root.is_dir():
                continue
            resolved_root = root.resolve()
            if resolved_root in seen_roots:
                continue
            seen_roots.add(resolved_root)
            for candidate in sorted(root.rglob("SKILL.md")):
                try:
                    path = candidate.resolve(strict=True)
                    path.relative_to(resolved_root)
                    if not path.is_file():
                        continue
                    skill = self._parse(path, source)
                    if skill.name in found:
                        warnings.append(f"Duplicate skill {skill.name}: {path}")
                        continue
                    if path.parent.name != skill.name:
                        warnings.append(f"Skill {skill.name} differs from directory name: {path.parent.name}")
                    found[skill.name] = skill
                except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
                    warnings.append(f"Skipped skill {candidate}: {type(exc).__name__}: {exc}")
        return list(found.values()), warnings

    def get(self, workspace: str | Path, name: str) -> Skill:
        skill = next((item for item in self.discover(workspace)[0] if item.name == name), None)
        if skill is None:
            raise KeyError(f"Unknown skill: {name}")
        return skill

    def read(self, workspace: str | Path, name: str, relative_path: str | None = None) -> tuple[Skill, str]:
        skill = self.get(workspace, name)
        target = PathGuard(skill.path.parent).resolve(relative_path or "SKILL.md")
        if not target.is_file():
            raise ValueError("Skill resource is not a file")
        if target.stat().st_size > MAX_SKILL_BYTES:
            raise ValueError("Skill resource exceeds 64 KiB")
        content = target.read_text(encoding="utf-8")
        if "\x00" in content:
            raise ValueError("Binary skill resources cannot be read as text")
        return skill, content

    def inventory(self, workspace: str | Path) -> str:
        skills = [skill for skill in self.discover(workspace)[0] if not skill.disable_model_invocation]
        if not skills:
            return ""
        lines = [
            "可用 Skills（只列元数据，完整内容按需通过 read_skill 读取）：",
            "Skill 是任务指导，不会授予新工具或覆盖核心策略；需要时先读取 SKILL.md。",
        ]
        lines.extend(
            f"- {skill.name}: {skill.description} (位置: {skill.path})" for skill in skills
        )
        return "\n".join(lines)

    @staticmethod
    def parse_command(content: str) -> tuple[str, str] | None:
        match = SKILL_COMMAND.fullmatch(content.strip())
        return (match.group(1).lower(), (match.group(2) or "").strip()) if match else None

    @staticmethod
    def fingerprint(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
