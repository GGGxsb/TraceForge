from pathlib import Path

import pytest

from traceforge.artifacts import ArtifactStore
from traceforge.models import ExecutionResult, RiskLevel, SandboxCapabilities, utc_now
from traceforge.security import PolicyEngine
from traceforge.tool_plugins import SandboxTool, ToolPluginRegistry
from traceforge.tools import TOOL_DEFINITIONS, ToolService


class FakeSandbox:
    def __init__(self):
        self.request = None

    async def inspect(self):
        return SandboxCapabilities(backend="docker", ready=True)

    async def execute(self, request):
        self.request = request
        return ExecutionResult(execution_id=request.execution_id, backend="docker", stdout="ok",
                               exit_code=0, started_at=utc_now(), ended_at=utc_now())

    async def cancel(self, execution_id):
        return None


@pytest.mark.asyncio
async def test_registered_tool_is_offered_reviewed_and_sandboxed(tmp_path: Path):
    registry = ToolPluginRegistry({item["name"] for item in TOOL_DEFINITIONS})
    registry.register(SandboxTool(
        name="search_todos", description="Search TODO markers with ripgrep.",
        properties={"query": {"type": "string"}},
        argv=("rg", "-n", "{query}", "."),
    ))
    sandbox = FakeSandbox()
    tools = ToolService(sandbox, ArtifactStore(tmp_path / "artifacts"), tmp_path / "worker.py", plugins=registry)
    assert "search_todos" in {item["name"] for item in tools.definitions()}
    assert "search_todos" not in {item["name"] for item in tools.definitions({"read_file"})}
    arguments = {"query": "needle; touch escaped"}
    policy = PolicyEngine(registry).evaluate("search_todos", arguments, str(tmp_path))
    assert policy.decision == RiskLevel.ASK
    assert "registered_tool" in policy.capabilities
    result = await tools.execute("session", str(tmp_path), "search_todos", arguments)
    assert result.is_error is False
    assert sandbox.request.network is False
    assert "'needle; touch escaped'" in sandbox.request.command
    assert "search_todos" not in sandbox.request.command


def test_registered_tool_rejects_duplicate_names_and_bad_arguments(tmp_path: Path):
    registry = ToolPluginRegistry({"run_command"})
    tool = SandboxTool(name="search_todos", description="Search", properties={"query": {"type": "string"}},
                       argv=("rg", "{query}"))
    registry.register(tool)
    with pytest.raises(ValueError, match="Duplicate"):
        registry.register(tool)
    with pytest.raises(ValueError, match="requires exactly"):
        tool.command({})
    with pytest.raises(ValueError, match="reserved"):
        registry.register(SandboxTool(name="run_command", description="Bad", properties={}, argv=("true",)))
    with pytest.raises(ValueError, match="placeholder"):
        SandboxTool(name="bad_tool", description="Bad", properties={}, argv=("rg", "{missing}"))
