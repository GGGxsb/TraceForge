from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _default_data_dir() -> Path:
    configured = os.getenv("TRACEFORGE_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "TraceForge"
    base = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "traceforge"


def _default_model_config_dir() -> Path:
    configured = os.getenv("TRACEFORGE_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "TraceForge" / "config"
    base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "traceforge"


@dataclass(slots=True, frozen=True)
class Settings:
    data_dir: Path
    model_config_dir: Path
    openai_api_key: str | None
    openai_model: str | None
    openai_base_url: str | None
    brief_model: str | None
    fallback_model: str | None
    context_window: int
    reserve_tokens: int
    keep_recent_tokens: int
    max_agent_rounds: int
    max_run_tokens: int
    max_run_cost_usd: float
    input_price_per_million: float
    output_price_per_million: float
    docker_image: str
    command_timeout: int
    approval_timeout: int
    hook_plugins: tuple[str, ...]
    tool_plugins: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Settings":
        model = os.getenv("OPENAI_MODEL") or None
        return cls(
            data_dir=_default_data_dir(),
            model_config_dir=_default_model_config_dir(),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_model=model,
            openai_base_url=os.getenv("OPENAI_BASE_URL") or None,
            brief_model=os.getenv("OPENAI_BRIEF_MODEL") or model,
            fallback_model=os.getenv("OPENAI_FALLBACK_MODEL") or None,
            context_window=int(os.getenv("TRACEFORGE_CONTEXT_WINDOW", "128000")),
            reserve_tokens=int(os.getenv("TRACEFORGE_RESERVE_TOKENS", "16384")),
            keep_recent_tokens=int(os.getenv("TRACEFORGE_KEEP_RECENT_TOKENS", "20000")),
            max_agent_rounds=int(os.getenv("TRACEFORGE_MAX_AGENT_ROUNDS", "50")),
            max_run_tokens=max(0, int(os.getenv("TRACEFORGE_MAX_RUN_TOKENS", "0"))),
            max_run_cost_usd=max(0.0, float(os.getenv("TRACEFORGE_MAX_RUN_COST_USD", "0"))),
            input_price_per_million=max(0.0, float(os.getenv("TRACEFORGE_INPUT_USD_PER_1M", "0"))),
            output_price_per_million=max(0.0, float(os.getenv("TRACEFORGE_OUTPUT_USD_PER_1M", "0"))),
            docker_image=os.getenv("TRACEFORGE_DOCKER_IMAGE", "traceforge-runner:local"),
            command_timeout=int(os.getenv("TRACEFORGE_COMMAND_TIMEOUT", "120")),
            approval_timeout=int(os.getenv("TRACEFORGE_APPROVAL_TIMEOUT", "600")),
            hook_plugins=tuple(
                item.strip()
                for item in os.getenv("TRACEFORGE_HOOKS", "").split(",")
                if item.strip()
            ),
            tool_plugins=tuple(
                item.strip() for item in os.getenv("TRACEFORGE_TOOL_PLUGINS", "").split(",")
                if item.strip()
            ),
        )

    def ensure_directories(self) -> None:
        for child in ("sessions", "artifacts", "workspaces", "cache"):
            (self.data_dir / child).mkdir(parents=True, exist_ok=True)
