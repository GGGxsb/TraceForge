from pathlib import Path

import pytest

from traceforge.models import PermissionMode, RiskLevel
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


def test_permission_modes_distinguish_plain_network_from_detected_risk(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    policy = PolicyEngine()
    read_network = {"command": "curl https://example.com/status"}
    assert policy.evaluate("run_command", read_network, str(workspace)).decision == RiskLevel.ASK
    automatic = policy.evaluate("run_command", read_network, str(workspace), PermissionMode.AUTO_APPROVE)
    assert automatic.decision == RiskLevel.ALLOW
    assert automatic.network is True
    upload = policy.evaluate("run_command", {"command": "curl -X POST https://example.com"},
                             str(workspace), PermissionMode.AUTO_APPROVE)
    assert upload.decision == RiskLevel.ASK


def test_external_file_edit_needs_approval_in_both_restricted_modes(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = tmp_path / "outside.txt"
    policy = PolicyEngine()
    for mode in (PermissionMode.REQUEST_APPROVAL, PermissionMode.AUTO_APPROVE):
        result = policy.evaluate("create_file", {"path": str(target)}, str(workspace), mode)
        assert result.decision == RiskLevel.ASK
        assert "external_file_write" in result.capabilities
        assert str(target) in result.affected_paths
    secret = policy.evaluate("create_file", {"path": str(tmp_path / ".env")}, str(workspace))
    assert secret.decision == RiskLevel.DENY
