from pathlib import Path

import pytest

from traceforge.models import RiskLevel
from traceforge.security import PathGuard, PolicyEngine


def test_path_guard_blocks_workspace_escape(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    guard = PathGuard(workspace)
    with pytest.raises(PermissionError):
        guard.resolve("../secret", allow_missing=True)


def test_policy_requires_approval_for_network_and_denies_privilege(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    policy = PolicyEngine()
    ask = policy.evaluate("run_command", {"command": "curl https://example.com", "network": True}, str(workspace))
    deny = policy.evaluate("run_command", {"command": "sudo mount /dev/sda /mnt", "network": False}, str(workspace))
    assert ask.decision == RiskLevel.ASK
    assert deny.decision == RiskLevel.DENY


def test_sensitive_files_symlink_escape_and_git_commit_are_guarded(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".env").write_text("SECRET=x", encoding="utf-8")
    with pytest.raises(PermissionError):
        PathGuard(workspace).resolve(".env")

    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this Windows account")
    with pytest.raises(PermissionError):
        PathGuard(workspace).resolve("link.txt")

    result = PolicyEngine().evaluate(
        "run_command",
        {"command": "git commit -am test", "network": False},
        str(workspace),
    )
    assert result.decision == RiskLevel.ASK
    assert "git_write" in result.capabilities
