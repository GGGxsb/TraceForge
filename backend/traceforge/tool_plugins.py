from __future__ import annotations

import importlib
import re
import shlex
from dataclasses import dataclass
from typing import Any, Callable


NAME_RE = re.compile(r"[a-z][a-z0-9_]{1,63}\Z")
PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z_0-9]*)\}\Z")


@dataclass(frozen=True, slots=True)
class SandboxTool:
    """Trusted registration metadata; model arguments only fill quoted argv slots."""

    name: str
    description: str
    properties: dict[str, dict[str, Any]]
    argv: tuple[str, ...]
    network: bool = False

    def __post_init__(self) -> None:
        if not NAME_RE.fullmatch(self.name) or not self.description.strip() or not self.argv:
            raise ValueError("Invalid sandbox tool registration")
        for token in self.argv:
            match = PLACEHOLDER_RE.fullmatch(token)
            if match and match.group(1) not in self.properties:
                raise ValueError(f"Unknown tool argument placeholder: {match.group(1)}")
        if any(not isinstance(key, str) or not isinstance(value, dict) for key, value in self.properties.items()):
            raise ValueError("Tool properties must be JSON Schema objects")

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function", "name": self.name, "description": self.description,
            "strict": True,
            "parameters": {"type": "object", "properties": self.properties,
                           "required": list(self.properties), "additionalProperties": False},
        }

    def argv_for(self, arguments: dict[str, Any]) -> list[str]:
        if set(arguments) != set(self.properties):
            raise ValueError(f"{self.name} requires exactly: {', '.join(self.properties)}")
        words: list[str] = []
        for token in self.argv:
            match = PLACEHOLDER_RE.fullmatch(token)
            value = arguments[match.group(1)] if match else token
            if value is None:
                raise ValueError(f"Null argument is not supported by {self.name}")
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"Unsupported argument value for {self.name}")
            words.append(str(value))
        return words

    def command(self, arguments: dict[str, Any]) -> str:
        return " ".join(shlex.quote(word) for word in self.argv_for(arguments))


class ToolPluginRegistry:
    def __init__(self, reserved_names: set[str]) -> None:
        self._reserved = reserved_names
        self._items: dict[str, SandboxTool] = {}

    def register(self, tool: SandboxTool) -> None:
        if tool.name in self._reserved or tool.name in self._items:
            raise ValueError(f"Duplicate or reserved tool name: {tool.name}")
        self._items[tool.name] = tool

    def get(self, name: str) -> SandboxTool | None:
        return self._items.get(name)

    def definitions(self) -> list[dict[str, Any]]:
        return [item.definition() for item in self._items.values()]

    def list(self) -> list[dict[str, Any]]:
        return [{"name": item.name, "description": item.description, "network": item.network}
                for item in self._items.values()]


def load_tool_plugin(spec: str, registry: ToolPluginRegistry) -> None:
    """Load an explicitly configured, trusted Python registration function."""
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"Tool plugin must be module:function: {spec}")
    register: Callable[[ToolPluginRegistry], None] = getattr(importlib.import_module(module_name), attribute)
    if not callable(register):
        raise ValueError(f"Tool plugin is not callable: {spec}")
    register(registry)
