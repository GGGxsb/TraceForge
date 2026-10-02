from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from traceforge.agent import ActiveRun
from traceforge.config import Settings
from traceforge.main import create_app
from traceforge.models import RunStatus

from .fakes import FakeModelAdapter


def isolated_settings(tmp_path: Path, **overrides):
    return replace(
        Settings.from_env(),
        data_dir=tmp_path / "data",
        model_config_dir=tmp_path / "config",
        openai_api_key=None,
        openai_model=None,
        openai_base_url=None,
        brief_model=None,
        **overrides,
    )


@pytest.mark.asyncio
async def test_web_model_settings_apply_without_restart_and_survive_restart(tmp_path: Path):
    settings = isolated_settings(tmp_path)
    app = create_app(settings)
    services = app.state.services
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    secret = "sk-local-test-key"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        initial = await client.get("/api/model-settings")
        assert initial.json()["configured"] is False
        assert initial.json()["has_api_key"] is False

        saved = await client.put(
            "/api/model-settings",
            headers={"X-TraceForge-UI": "1"},
            json={
                "api_key": secret,
                "model": "test-main",
                "base_url": "https://gateway.example.test/v1",
                "brief_model": "test-brief",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["configured"] is True
        assert saved.json()["model"] == "test-main"
        assert saved.json()["brief_model"] == "test-brief"
        assert saved.json()["base_url"] == "https://gateway.example.test/v1"
        assert secret not in saved.text
        assert secret not in (await client.get("/api/model-settings")).text
        assert (await client.get("/api/health")).json()["model_configured"] is True

        shared = services.model_settings.adapter
        assert services.runner.adapter is shared
        assert services.runner.compactor.adapter is shared
        assert services.branches.adapter is shared
        assert next(hook for hook in services.hooks._hooks if hook.name == "task_brief").adapter is shared
        assert shared._delegate.model == "test-main"
        assert shared._delegate.base_url == "https://gateway.example.test/v1"
        assert str(shared._delegate.client.base_url).startswith("https://gateway.example.test/v1")

        changed = await client.put(
            "/api/model-settings",
            headers={"X-TraceForge-UI": "1"},
            json={"model": "test-second", "brief_model": ""},
        )
        assert changed.status_code == 200
        assert changed.json()["has_api_key"] is True
        assert changed.json()["base_url"] == "https://gateway.example.test/v1"
        assert shared._delegate.model == "test-second"
        assert shared._delegate.brief_model == "test-second"

        cleared = await client.put(
            "/api/model-settings",
            headers={"X-TraceForge-UI": "1"},
            json={"model": "test-second", "base_url": "", "brief_model": ""},
        )
        assert cleared.status_code == 200
        assert cleared.json()["base_url"] == ""
        assert shared._delegate.base_url is None

        repo = tmp_path / "repo"
        repo.mkdir()
        workspace = services.workspaces.register(str(repo))
        session = services.sessions.create(workspace)
        shared.replace(FakeModelAdapter())
        run = services.runner.start(session.header.id, "answer using the configured adapter")
        await run.task
        assert any(entry.type == "assistant_message" and entry.payload["content"] == "done" for entry in session.entries)

    config_path = settings.model_config_dir / "model-settings.json"
    assert secret in config_path.read_text(encoding="utf-8")
    restarted = create_app(settings)
    assert restarted.state.services.model_settings.status()["model"] == "test-second"
    assert restarted.state.services.model_settings.status()["base_url"] == ""
    assert restarted.state.services.model_settings.status()["has_api_key"] is True

    reset_transport = httpx.ASGITransport(app=restarted, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=reset_transport, base_url="http://test") as client:
        reset = await client.delete("/api/model-settings", headers={"X-TraceForge-UI": "1"})
        assert reset.status_code == 200
        assert reset.json()["configured"] is False
    assert not config_path.exists()


@pytest.mark.asyncio
async def test_model_specific_context_windows_use_smaller_fallback_limit(tmp_path: Path):
    app = create_app(isolated_settings(tmp_path))
    services = app.state.services
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        saved = await client.put(
            "/api/model-settings",
            headers={"X-TraceForge-UI": "1"},
            json={
                "api_key": "sk-test", "model": "large-model", "context_window": 128000,
                "fallback_model": "small-model", "fallback_context_window": 8192,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["effective_context_window"] == 8192
        assert services.runner.projector.context_window == 8192
        assert services.runner.compactor.context_window == 8192
        assert services.runner.compactor.reserve_tokens == 2048
        assert services.runner.compactor.keep_recent_tokens <= 3072

        changed = await client.put(
            "/api/model-settings",
            headers={"X-TraceForge-UI": "1"},
            json={"model": "large-model", "context_window": 128000},
        )
        assert changed.status_code == 200
        assert changed.json()["effective_context_window"] == 128000
        assert services.runner.projector.context_window == 128000


@pytest.mark.asyncio
async def test_model_settings_require_local_ui_and_do_not_change_during_active_run(tmp_path: Path):
    app = create_app(isolated_settings(tmp_path))
    services = app.state.services
    local = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    remote = httpx.ASGITransport(app=app, client=("192.0.2.1", 1234))
    payload = {"api_key": "sk-test", "model": "test-main"}
    async with httpx.AsyncClient(transport=local, base_url="http://test") as client:
        assert (await client.put("/api/model-settings", json=payload)).status_code == 403
    async with httpx.AsyncClient(transport=remote, base_url="http://test") as client:
        denied = await client.put("/api/model-settings", headers={"X-TraceForge-UI": "1"}, json=payload)
        assert denied.status_code == 403

    wait_forever = asyncio.Event()
    task = asyncio.create_task(wait_forever.wait())
    services.runner.runs["active"] = ActiveRun(id="active", session_id="session", status=RunStatus.EXECUTING, task=task)
    try:
        async with httpx.AsyncClient(transport=local, base_url="http://test") as client:
            blocked = await client.put("/api/model-settings", headers={"X-TraceForge-UI": "1"}, json=payload)
            assert blocked.status_code == 409
            assert (await client.get("/api/model-settings")).json()["configured"] is False
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_malformed_json_request_does_not_echo_api_key(tmp_path: Path):
    app = create_app(isolated_settings(tmp_path))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/model-settings",
            headers={"X-TraceForge-UI": "1", "Content-Type": "text/plain"},
            content='{"api_key":"sk-test-secret","model":"test-model"}',
        )
    assert response.status_code == 422
    assert "sk-test-secret" not in response.text
    assert response.json()["detail"][0]["msg"]


@pytest.mark.asyncio
async def test_base_url_validation_and_local_http_proxy(tmp_path: Path):
    app = create_app(isolated_settings(tmp_path))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for base_url in (
            "http://gateway.example.test/v1",
            "https://user:password@gateway.example.test/v1",
            "https://gateway.example.test/v1?token=secret",
        ):
            response = await client.put(
                "/api/model-settings",
                headers={"X-TraceForge-UI": "1"},
                json={"api_key": "sk-test", "model": "test-main", "base_url": base_url},
            )
            assert response.status_code == 400
        accepted = await client.put(
            "/api/model-settings",
            headers={"X-TraceForge-UI": "1"},
            json={"api_key": "sk-test", "model": "test-main", "base_url": "http://127.0.0.1:9000/v1"},
        )
        assert accepted.status_code == 200
        assert accepted.json()["base_url"] == "http://127.0.0.1:9000/v1"


@pytest.mark.asyncio
async def test_web_reset_restores_environment_defaults(tmp_path: Path):
    settings = replace(
        Settings.from_env(),
        data_dir=tmp_path / "data",
        model_config_dir=tmp_path / "config",
        openai_api_key="sk-environment",
        openai_model="env-model",
        openai_base_url="https://env.example.test/v1",
        brief_model="env-brief",
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        initial = (await client.get("/api/model-settings")).json()
        assert initial["source"] == "environment"
        assert initial["model"] == "env-model"
        assert initial["base_url"] == "https://env.example.test/v1"
        assert "sk-environment" not in str(initial)
        await client.put(
            "/api/model-settings",
            headers={"X-TraceForge-UI": "1"},
            json={"model": "web-model", "brief_model": None},
        )
        restored = await client.delete("/api/model-settings", headers={"X-TraceForge-UI": "1"})
        assert restored.json()["source"] == "environment"
        assert restored.json()["model"] == "env-model"
        assert restored.json()["base_url"] == "https://env.example.test/v1"
        assert restored.json()["brief_model"] == "env-brief"


def test_agent_cannot_run_in_workspace_containing_model_config(tmp_path: Path):
    app = create_app(isolated_settings(tmp_path))
    services = app.state.services
    workspace = services.workspaces.register(str(tmp_path))
    session = services.sessions.create(workspace)
    with pytest.raises(RuntimeError, match="模型配置"):
        services.runner.start(session.header.id, "inspect everything")
