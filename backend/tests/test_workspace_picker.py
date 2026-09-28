from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from traceforge.config import Settings
from traceforge.main import create_app


def test_folder_picker_registers_selected_directory_and_handles_cancel(tmp_path: Path, monkeypatch):
    folder = tmp_path / "chosen-repo"
    folder.mkdir()

    async def choose_folder():
        return str(folder)

    monkeypatch.setattr("traceforge.api.pick_directory", choose_folder)
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    with TestClient(app) as client:
        assert client.post("/api/workspaces/pick-directory").status_code == 403

        chosen = client.post("/api/workspaces/pick-directory", headers={"X-TraceForge-UI": "1"})
        assert chosen.status_code == 200
        assert chosen.json()["cancelled"] is False
        assert chosen.json()["workspace"]["path"] == str(folder)
        assert len(client.get("/api/workspaces").json()) == 1

        async def cancel():
            return None

        monkeypatch.setattr("traceforge.api.pick_directory", cancel)
        cancelled = client.post("/api/workspaces/pick-directory", headers={"X-TraceForge-UI": "1"})
        assert cancelled.status_code == 200
        assert cancelled.json() == {"cancelled": True, "workspace": None}
        assert len(client.get("/api/workspaces").json()) == 1
