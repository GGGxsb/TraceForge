from dataclasses import replace
from pathlib import Path

import pytest

from traceforge.config import Settings
from traceforge.main import create_app
from traceforge.model_adapter import ModelDelta, usage_sink
from traceforge.models import ToolCall

from .fakes import FakeModelAdapter


class MeteredAdapter(FakeModelAdapter):
    async def stream_turn(self, instructions, input_items, tools):
        sink = usage_sink.get()
        assert sink is not None
        await sink({"model": "fixture-model", "purpose": "agent_turn", "input_tokens": 20,
                    "output_tokens": 5, "total_tokens": 25, "cached_input_tokens": 4,
                    "reasoning_tokens": 2})
        yield ModelDelta(type="tool_call", tool_call=ToolCall(
            call_id="metered-call", name="read_file",
            arguments={"path": "main.py", "start_line": None, "end_line": None},
        ))
        yield ModelDelta(type="done")


@pytest.mark.asyncio
@pytest.mark.parametrize(("max_tokens", "max_cost", "outcome", "gap_id"), [
    (10, 0.0, "token_budget", "agent_token_budget"),
    (0, 0.00001, "cost_budget", "agent_cost_budget"),
])
async def test_usage_metrics_and_token_budget_are_persisted(
    tmp_path: Path, max_tokens: int, max_cost: float, outcome: str, gap_id: str,
):
    settings = replace(Settings.from_env(), data_dir=tmp_path / "data", model_config_dir=tmp_path / "config",
                       max_run_tokens=max_tokens, max_run_cost_usd=max_cost,
                       input_price_per_million=1.0, output_price_per_million=2.0)
    app = create_app(settings)
    services = app.state.services
    services.model_settings.adapter.replace(MeteredAdapter())
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "main.py").write_text("answer = 42\n", encoding="utf-8")
    session = services.sessions.create(services.workspaces.register(str(workspace)))
    run = services.runner.start(session.header.id, "read main.py")
    await run.task
    usage = [entry.payload for entry in session.entries if entry.type == "model_usage"]
    assert len(usage) == 1
    assert usage[0]["total_tokens"] == 25
    assert usage[0]["estimated_cost_usd"] == 0.00003
    terminal = next(entry.payload for entry in reversed(session.entries)
                    if entry.type == "run_state" and entry.payload.get("status") == "completed")
    assert terminal["outcome"] == outcome
    assert terminal["usage"]["total_tokens"] == 25
    assert terminal["tool_calls"] == 1
    assert terminal["tool_errors"] == 0
    assert any(entry.type == "clarification_question" and entry.payload["gap_id"] == gap_id
               for entry in session.entries)
