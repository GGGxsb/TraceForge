from __future__ import annotations

import json
import os
import ipaddress
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from .model_adapter import ModelAdapter, ModelDelta, OpenAIModelAdapter, UnavailableModelAdapter
from .models import BranchSummary, CompactionSummary, TaskBrief


class SwitchableModelAdapter:
    """One adapter reference shared by the Agent, hooks and summary services."""

    def __init__(self, delegate: ModelAdapter) -> None:
        self._delegate = delegate

    def replace(self, delegate: ModelAdapter) -> None:
        self._delegate = delegate

    async def analyze_task_brief(self, user_text: str, evidence: list[dict[str, Any]], previous: TaskBrief | None) -> TaskBrief:
        delegate = self._delegate
        return await delegate.analyze_task_brief(user_text, evidence, previous)

    async def stream_turn(
        self, instructions: str, input_items: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[ModelDelta]:
        delegate = self._delegate
        async for delta in delegate.stream_turn(instructions, input_items, tools):
            yield delta

    async def summarize_compaction(self, transcript: str, previous: CompactionSummary | None) -> CompactionSummary:
        delegate = self._delegate
        return await delegate.summarize_compaction(transcript, previous)

    async def summarize_branch(self, transcript: str) -> BranchSummary:
        delegate = self._delegate
        return await delegate.summarize_branch(transcript)


class ModelSettingsStore:
    """Local web overrides layered over environment defaults; never returns the key."""

    def __init__(
        self,
        path: Path,
        *,
        env_api_key: str | None,
        env_model: str | None,
        env_base_url: str | None,
        env_brief_model: str | None,
        env_fallback_model: str | None = None,
        default_context_window: int = 128_000,
    ) -> None:
        self.path = path
        self.env_api_key = env_api_key
        self.env_model = env_model
        self.env_base_url = env_base_url
        self.env_brief_model = env_brief_model
        self.env_fallback_model = env_fallback_model
        self.default_context_window = default_context_window
        self.load_error: str | None = None
        self._saved = self._read()
        self.adapter = SwitchableModelAdapter(self._build_adapter(self._saved))

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != 1:
                raise ValueError("Unsupported model settings format")
            result: dict[str, Any] = {}
            for field in ("api_key", "model", "base_url", "brief_model", "fallback_model"):
                value = raw.get(field)
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"Invalid {field} setting")
                result[field] = value
            for field in ("context_window", "fallback_context_window"):
                value = raw.get(field)
                if value is not None and (type(value) is not int or value < 1024):
                    raise ValueError(f"Invalid {field} setting")
                result[field] = value
            return result
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.load_error = f"本地模型配置无法读取：{type(exc).__name__}"
            return {}

    def _resolved(self, saved: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None, str | None]:
        key = saved.get("api_key") or self.env_api_key
        model = saved.get("model") or self.env_model
        base_url = saved.get("base_url") or self.env_base_url
        brief_model = saved.get("brief_model") or (self.env_brief_model if not saved else None) or model
        fallback_model = saved.get("fallback_model") or (self.env_fallback_model if not saved else None)
        return key, model, base_url, brief_model, fallback_model

    def _build_adapter(self, saved: dict[str, Any]) -> ModelAdapter:
        key, model, base_url, brief_model, fallback_model = self._resolved(saved)
        if key and model:
            return OpenAIModelAdapter(api_key=key, model=model, brief_model=brief_model,
                                      fallback_model=fallback_model, base_url=base_url)
        return UnavailableModelAdapter("OpenAI 未配置。请在 TraceForge 设置中填写 API Key 和模型。")

    def status(self) -> dict[str, Any]:
        key, model, base_url, brief_model, fallback_model = self._resolved(self._saved)
        main_window = self._saved.get("context_window") or self.default_context_window
        fallback_window = self._saved.get("fallback_context_window") or self.default_context_window
        has_fallback = bool(fallback_model and fallback_model != model)
        return {
            "provider": "openai",
            "configured": bool(key and model),
            "has_api_key": bool(key),
            "model": model or "",
            "base_url": base_url or "",
            "brief_model": brief_model or "",
            "fallback_model": fallback_model or "",
            "context_window": main_window,
            "fallback_context_window": fallback_window if has_fallback else None,
            "effective_context_window": min(main_window, fallback_window) if has_fallback else main_window,
            "source": "web" if self._saved else "environment",
            "load_error": self.load_error,
        }

    @staticmethod
    def _validate_base_url(value: str) -> str:
        url = value.strip().rstrip("/")
        if not url or any(ord(char) < 33 for char in url):
            raise ValueError("Base URL 格式无效")
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            parsed.port  # Validate a supplied port number.
        except ValueError as exc:
            raise ValueError("Base URL 格式无效") from exc
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            raise ValueError("Base URL 必须是没有用户名和密码的 HTTP(S) 地址")
        if parsed.query or parsed.fragment:
            raise ValueError("Base URL 不能包含查询参数或片段")
        if parsed.scheme == "http":
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host.lower() == "localhost"
            if not loopback:
                raise ValueError("非本机 Base URL 必须使用 HTTPS")
        return url

    def save(
        self, *, api_key: str | None, model: str, brief_model: str | None, base_url: str | None = None,
        fallback_model: str | None = None,
        context_window: int | None = None, fallback_context_window: int | None = None,
    ) -> dict[str, Any]:
        model = model.strip()
        brief_model = brief_model.strip() if brief_model else None
        fallback_model = fallback_model.strip() if fallback_model else None
        supplied_key = api_key.strip() if api_key else None
        if not model or any(ord(char) < 32 for char in model):
            raise ValueError("请输入有效的主模型名称")
        if brief_model and any(ord(char) < 32 for char in brief_model):
            raise ValueError("请输入有效的 TaskBrief 模型名称")
        if fallback_model and any(ord(char) < 32 for char in fallback_model):
            raise ValueError("请输入有效的备用模型名称")
        if supplied_key and any(ord(char) < 32 for char in supplied_key):
            raise ValueError("API Key 包含无效字符")
        for label, value in (("主模型", context_window), ("备用模型", fallback_context_window)):
            if value is not None and (type(value) is not int or value < 1024):
                raise ValueError(f"{label}上下文窗口至少为 1024 token")
        saved: dict[str, Any] = {
            "api_key": supplied_key or self._saved.get("api_key"),
            "model": model,
            "base_url": (
                self._saved.get("base_url") if base_url is None
                else self._validate_base_url(base_url) if base_url.strip() else None
            ),
            "brief_model": brief_model,
            "fallback_model": fallback_model,
            "context_window": context_window or self._saved.get("context_window") or self.default_context_window,
            "fallback_context_window": (
                fallback_context_window or self._saved.get("fallback_context_window") or self.default_context_window
            ) if fallback_model else None,
        }
        if not (saved["api_key"] or self.env_api_key):
            raise ValueError("请输入 OpenAI API Key")
        delegate = self._build_adapter(saved)
        self._write(saved)
        self._saved = saved
        self.load_error = None
        self.adapter.replace(delegate)
        return self.status()

    def reset(self) -> dict[str, Any]:
        delegate = self._build_adapter({})
        self.path.unlink(missing_ok=True)
        self._saved = {}
        self.load_error = None
        self.adapter.replace(delegate)
        return self.status()

    def _write(self, saved: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        data = json.dumps({"version": 1, **saved}, ensure_ascii=False).encode("utf-8")
        try:
            with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)
