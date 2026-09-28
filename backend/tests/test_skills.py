from pathlib import Path
from dataclasses import replace

import httpx
import pytest

from traceforge.artifacts import ArtifactStore
from traceforge.context import ContextProjector
from traceforge.models import SessionHeader
from traceforge.skills import SkillCatalog
from traceforge.storage import JsonlSession
from traceforge.tools import ToolService
from traceforge.security import PolicyEngine
from traceforge.config import Settings
from traceforge.main import create_app


def write_skill(root: Path, name: str, description: str, *, extra: str = "", body: str = "Follow these steps.") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    path = directory / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n# {name}\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_pi_style_discovery_and_progressive_loading(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    local = write_skill(workspace / ".pi" / "skills", "review-code", "Review code when requested")
    write_skill(tmp_path / "home" / ".pi" / "agent" / "skills", "review-code", "Global duplicate")
    hidden = write_skill(
        workspace / ".agents" / "skills", "release-check", "Check releases",
        extra="disable-model-invocation: true\n",
    )
    (hidden.parent / "references").mkdir()
    (hidden.parent / "references" / "checklist.md").write_text("Check version numbers.", encoding="utf-8")
    catalog = SkillCatalog(tmp_path / "config", home=tmp_path / "home")

    skills, warnings = catalog.discover(workspace)
    assert [skill.name for skill in skills] == ["review-code", "release-check"]
    assert skills[0].path == local.resolve()
    assert any("Duplicate skill" in warning for warning in warnings)
    inventory = catalog.inventory(workspace)
    assert "review-code" in inventory
    assert "Follow these steps" not in inventory
    assert "release-check" not in inventory
    assert "Check version numbers." == catalog.read(workspace, "release-check", "references/checklist.md")[1]
    assert catalog.parse_command("/skill:review-code inspect src") == ("review-code", "inspect src")


def test_skill_resource_cannot_escape_directory(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    write_skill(workspace / ".pi" / "skills", "read-docs", "Read docs")
    (workspace / "private.txt").write_text("secret", encoding="utf-8")
    catalog = SkillCatalog(tmp_path / "config", home=tmp_path / "home")
    with pytest.raises(PermissionError):
        catalog.read(workspace, "read-docs", "../../../private.txt")
    with pytest.raises(PermissionError):
        catalog.read(workspace, "read-docs", str(workspace / "private.txt"))
    assert PolicyEngine().evaluate(
        "read_skill", {"name": "read-docs", "path": "references/checklist.md"}, str(workspace)
    ).decision.value == "allow"


def test_bad_frontmatter_is_reported_without_blocking_other_skills(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    write_skill(workspace / ".pi" / "skills", "good-skill", "Useful skill")
    bad = workspace / ".pi" / "skills" / "bad-skill"
    bad.mkdir()
    (bad / "SKILL.md").write_text("# No frontmatter", encoding="utf-8")
    skills, warnings = SkillCatalog(tmp_path / "config", home=tmp_path / "home").discover(workspace)
    assert [skill.name for skill in skills] == ["good-skill"]
    assert len(warnings) == 1 and "bad-skill" in warnings[0]


@pytest.mark.asyncio
async def test_read_skill_tool_and_explicit_skill_survive_compaction(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    write_skill(workspace / ".pi" / "skills", "review-code", "Review code", body="Check tests before editing.")
    catalog = SkillCatalog(tmp_path / "config", home=tmp_path / "home")
    tools = ToolService(object(), ArtifactStore(tmp_path / "artifacts"), tmp_path / "worker.py", skills=catalog)
    result = await tools.execute("session", str(workspace), "read_skill", {"name": "review-code", "path": None})
    assert not result.is_error and "Check tests before editing." in result.output

    session = JsonlSession.create(
        tmp_path / "s.jsonl", SessionHeader(id="s", workspace_id="w", workspace=str(workspace)),
    )
    await session.append("user_message", {"content": "/skill:review-code inspect"})
    await session.append("skill_activation", {
        "name": "review-code", "path": str(catalog.get(workspace, "review-code").path),
        "content": catalog.read(workspace, "review-code")[1], "arguments": "inspect",
    })
    recent = await session.append("user_message", {"content": "Continue"})
    await session.append("compaction", {"summary": {"goal": "review"}, "first_kept_entry_id": recent.id})
    projected = ContextProjector(128_000, 16_000).project(session)
    assert any("Check tests before editing." in item.get("content", "") for item in projected.input_items)
    assert any(item.get("content") == "Continue" for item in projected.input_items)


@pytest.mark.asyncio
async def test_skills_api_lists_workspace_and_rejects_unknown_explicit_skill(tmp_path: Path):
    app = create_app(replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    write_skill(workspace / ".pi" / "skills", "review-code", "Review code")
    registered = app.state.services.workspaces.register(str(workspace))
    session = app.state.services.sessions.create(registered)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(f"/api/workspaces/{registered.id}/skills")
        assert listed.status_code == 200
        assert listed.json()["skills"][0]["name"] == "review-code"
        missing = await client.post(f"/api/sessions/{session.header.id}/runs", json={
            "content": "/skill:missing-skill do something",
        })
        assert missing.status_code == 400
        assert not session.entries
