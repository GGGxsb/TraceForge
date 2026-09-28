from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from traceforge.config import Settings
from traceforge.main import create_app


def _app(tmp_path: Path):
    return create_app(replace(
        Settings.from_env(),
        data_dir=tmp_path / "data",
        model_config_dir=tmp_path / "config",
    ))


def test_project_relocation_keeps_session_history_and_stable_id(tmp_path: Path, monkeypatch):
    old = tmp_path / "old-repo"
    old.mkdir()
    (old / "main.py").write_text("print('hello')\n", encoding="utf-8")
    app = _app(tmp_path)
    with TestClient(app) as client:
        project = client.post("/api/workspaces", json={"path": str(old)}).json()
        session = client.post("/api/sessions", json={"workspace_id": project["id"]}).json()
        loaded = app.state.services.sessions.get(session["id"])
        asyncio.run(loaded.append("user_message", {"content": "hello"}))
        checkpoint = app.state.services.checkpoints.capture(loaded)

        new = tmp_path / "new-repo"
        old.rename(new)
        async def choose_new():
            return str(new)
        monkeypatch.setattr("traceforge.api.pick_directory", choose_new)

        assert client.get("/api/workspaces").json()[0]["available"] is False
        assert client.post("/api/sessions", json={"workspace_id": project["id"]}).status_code == 409
        assert client.post(f"/api/workspaces/{project['id']}/relocate").status_code == 403
        response = client.post(f"/api/workspaces/{project['id']}/relocate",
                               headers={"X-TraceForge-UI": "1"})
        assert response.status_code == 200
        assert response.json()["workspace"]["id"] == project["id"]
        assert response.json()["workspace"]["path"] == str(new)
        assert client.get("/api/workspaces").json()[0]["available"] is True

        detail = client.get(f"/api/sessions/{session['id']}").json()
        assert detail["header"]["workspace"] == str(old)
        assert detail["active_workspace"] == str(new)
        assert detail["entries"][0]["payload"]["content"] == "hello"
        assert app.state.services.checkpoints.manifest(loaded, checkpoint["checkpoint_id"])["workspace"] == str(old)
        assert client.get("/api/sessions").json()[0]["workspace"] == str(new)
        assert client.get(f"/api/sessions/{session['id']}/file", params={"path": "main.py"}).text == "print('hello')\n"

    # The root is resolved from the project registry after a process restart too.
    with TestClient(_app(tmp_path)) as client:
        assert client.get(f"/api/sessions/{session['id']}").json()["active_workspace"] == str(new)


def test_project_relocation_rejects_live_root_and_existing_branch(tmp_path: Path, monkeypatch):
    old = tmp_path / "old-repo"
    new = tmp_path / "new-repo"
    old.mkdir()
    new.mkdir()
    app = _app(tmp_path)

    async def choose_new():
        return str(new)
    monkeypatch.setattr("traceforge.api.pick_directory", choose_new)
    with TestClient(app) as client:
        project = client.post("/api/workspaces", json={"path": str(old)}).json()
        session = client.post("/api/sessions", json={"workspace_id": project["id"]}).json()
        endpoint = f"/api/workspaces/{project['id']}/relocate"
        headers = {"X-TraceForge-UI": "1"}
        assert client.post(endpoint, headers=headers).status_code == 409
        old.rmdir()
        loaded = app.state.services.sessions.get(session["id"])
        asyncio.run(loaded.append("workspace_binding", {"path": str(tmp_path / "fork")}))
        assert client.post(endpoint, headers=headers).status_code == 409
        assert client.get("/api/workspaces").json()[0]["path"] == str(old)
