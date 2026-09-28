import json
from pathlib import Path

import pytest

from traceforge.storage import WorkspaceStore
from traceforge.workspaces import WorkspaceInspector


def test_directory_snapshot_excludes_credentials(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "safe.txt").write_text("safe", encoding="utf-8")
    (repo / ".env").write_text("SECRET=do-not-store", encoding="utf-8")
    store = WorkspaceStore(tmp_path / "registry")
    workspace = store.register(str(repo))
    inspector = WorkspaceInspector(store)
    inspector.capture_directory_baseline(workspace.id)
    snapshot = json.loads((store.root / f"{workspace.id}.snapshot.json").read_text(encoding="utf-8"))
    assert snapshot == {"safe.txt": "safe"}
    assert all(item["path"] != ".env" for item in inspector.files(workspace.id))


def test_workspace_ignores_internal_data_and_test_cache(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_text("print('ok')\n", encoding="utf-8")
    for folder in (".traceforge-data", ".traceforge-data-smoke", ".pytest_cache"):
        internal = repo / folder
        internal.mkdir()
        (internal / "hidden.txt").write_text("internal", encoding="utf-8")
    store = WorkspaceStore(tmp_path / "registry")
    workspace = store.register(str(repo))
    inspector = WorkspaceInspector(store)
    inspector.capture_directory_baseline(workspace.id)

    snapshot = json.loads((store.root / f"{workspace.id}.snapshot.json").read_text(encoding="utf-8"))
    assert snapshot == {"source.py": "print('ok')\n"}
    assert [item["path"] for item in inspector.files(workspace.id)] == ["source.py"]


@pytest.mark.asyncio
async def test_directory_diff_tracks_created_and_modified_files(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "existing.txt").write_text("before\n", encoding="utf-8")
    store = WorkspaceStore(tmp_path / "registry")
    workspace = store.register(str(repo))
    inspector = WorkspaceInspector(store)
    inspector.capture_directory_baseline(workspace.id)

    (repo / "existing.txt").write_text("after\n", encoding="utf-8")
    (repo / "created.txt").write_text("new\n", encoding="utf-8")
    diff = await inspector.diff(workspace.id)

    assert "-before" in diff and "+after" in diff
    assert "b/created.txt" in diff and "+new" in diff
