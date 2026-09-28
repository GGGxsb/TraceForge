from types import SimpleNamespace

import pytest

from traceforge.model_adapter import OpenAIModelAdapter, _record_usage, _restore_legacy_deepseek_history, usage_sink


@pytest.mark.asyncio
async def test_stream_preserves_reasoning_output_item():
    reasoning = {"id": "rs_1", "type": "reasoning", "content": [{"type": "reasoning_text", "text": "think"}]}

    class Item:
        type = "reasoning"

        def model_dump(self, **_kwargs):
            return reasoning

    class Responses:
        def __init__(self):
            self.kwargs = None

        async def create(self, **kwargs):
            self.kwargs = kwargs

            async def events():
                yield SimpleNamespace(type="response.reasoning_text.delta", delta="think")
                yield SimpleNamespace(type="response.output_item.done", item=Item())
                yield SimpleNamespace(type="response.output_item.done", item=SimpleNamespace(
                    type="function_call", call_id="call-1", name="read_file", arguments='{"path":"a.py"}',
                ))

            return events()

    adapter = OpenAIModelAdapter(api_key="test", model="test")
    responses = Responses()
    adapter.client = SimpleNamespace(responses=responses)
    deltas = [delta async for delta in adapter.stream_turn("instructions", [], [])]
    assert [delta.type for delta in deltas] == ["reasoning_delta", "tool_call", "reasoning_item", "done"]
    assert deltas[0].text == "think"
    assert deltas[2].raw == reasoning
    assert responses.kwargs["include"] == ["reasoning.encrypted_content"]
    assert callable(adapter.summarize_compaction)
    assert callable(adapter.summarize_branch)


@pytest.mark.asyncio
async def test_completed_response_recovers_missing_and_partial_reasoning_items():
    partial = {"id": "rs_1", "type": "reasoning", "content": []}
    complete = {"id": "rs_1", "type": "reasoning",
                "content": [{"type": "reasoning_text", "text": "full thought"}]}

    class Item:
        def __init__(self, raw):
            self.raw = raw
            self.type = raw["type"]

        def model_dump(self, **_kwargs):
            return self.raw

    class Responses:
        async def create(self, **_kwargs):
            async def events():
                yield SimpleNamespace(type="response.output_item.done", item=Item(partial))
                yield SimpleNamespace(type="response.completed", response=SimpleNamespace(
                    output=[Item(complete), SimpleNamespace(type="function_call", call_id="call-1",
                                                        name="read_file", arguments='{"path":"a.py"}')],
                    usage=None,
                ))

            return events()

    adapter = OpenAIModelAdapter(api_key="test", model="test")
    adapter.client = SimpleNamespace(responses=Responses())
    deltas = [delta async for delta in adapter.stream_turn("instructions", [], [])]
    assert [delta.type for delta in deltas] == ["tool_call", "reasoning_item", "done"]
    assert deltas[1].raw == complete


@pytest.mark.asyncio
async def test_reasoning_text_stream_is_replayable_when_provider_omits_reasoning_item():
    class Responses:
        async def create(self, **_kwargs):
            async def events():
                yield SimpleNamespace(type="response.reasoning_text.delta", delta="first ")
                yield SimpleNamespace(type="response.reasoning_text.delta", delta="second")
                yield SimpleNamespace(type="response.output_item.done", item=SimpleNamespace(
                    type="function_call", call_id="call-1", name="read_file", arguments='{"path":"a.py"}',
                ))
                yield SimpleNamespace(type="response.completed", response=SimpleNamespace(output=[], usage=None))

            return events()

    adapter = OpenAIModelAdapter(api_key="test", model="test")
    adapter.client = SimpleNamespace(responses=Responses())
    deltas = [delta async for delta in adapter.stream_turn("instructions", [], [])]
    assert [delta.type for delta in deltas] == ["reasoning_delta", "reasoning_delta", "tool_call",
                                                  "reasoning_item", "done"]
    assert deltas[-2].raw == {"type": "reasoning", "content": [
        {"type": "reasoning_text", "text": "first second"},
    ]}


def test_old_deepseek_tool_pairs_without_reasoning_are_projected_as_evidence():
    items = [
        {"role": "user", "content": "inspect"},
        {"type": "function_call", "call_id": "old", "name": "run_command", "arguments": '{"command":"pwd"}'},
        {"type": "function_call_output", "call_id": "old", "output": "/workspace"},
        {"role": "user", "content": "continue"},
        {"id": "rs_1", "type": "reasoning", "content": [{"type": "reasoning_text", "text": "next"}]},
        {"type": "function_call", "call_id": "new", "name": "read_file", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "new", "output": "ok"},
    ]
    projected = _restore_legacy_deepseek_history(items)
    assert len(projected) == 6
    assert "run_command" in projected[1]["content"]
    assert "/workspace" in projected[1]["content"]
    assert projected[-3:] == items[-3:]


def test_deepseek_replay_groups_calls_before_outputs_from_mixed_batch():
    reasoning = {"id": "rs_1", "type": "reasoning", "content": [{"type": "reasoning_text", "text": "think"}]}
    first_call = {"type": "function_call", "call_id": "a", "name": "run_command", "arguments": "{}"}
    second_call = {"type": "function_call", "call_id": "b", "name": "search_code", "arguments": "{}"}
    first_output = {"type": "function_call_output", "call_id": "a", "output": "ok"}
    second_output = {"type": "function_call_output", "call_id": "b", "output": "found"}
    items = [
        {"role": "user", "content": "inspect"}, reasoning,
        {"role": "assistant", "content": "I'll check"},
        first_call, first_output, second_call, second_output,
    ]
    projected = _restore_legacy_deepseek_history(items)
    assert projected[-4:] == [first_call, second_call, first_output, second_output]


@pytest.mark.asyncio
async def test_model_usage_is_reported_with_actual_provider_counts():
    received = []

    async def collect(record):
        received.append(record)

    token = usage_sink.set(collect)
    try:
        usage = SimpleNamespace(input_tokens=100, output_tokens=25, total_tokens=125,
                                input_tokens_details={"cached_tokens": 40},
                                output_tokens_details={"reasoning_tokens": 10})
        await _record_usage(SimpleNamespace(model="test", usage=usage), model="fallback", purpose="agent_turn")
    finally:
        usage_sink.reset(token)
    assert received == [{"model": "test", "purpose": "agent_turn", "input_tokens": 100,
                         "output_tokens": 25, "total_tokens": 125, "cached_input_tokens": 40,
                         "reasoning_tokens": 10}]


@pytest.mark.asyncio
async def test_fallback_model_only_starts_after_primary_request_fails():
    class Responses:
        def __init__(self):
            self.models = []

        async def create(self, **kwargs):
            self.models.append(kwargs["model"])
            if kwargs["model"] == "primary":
                raise RuntimeError("primary offline")

            async def events():
                yield SimpleNamespace(type="response.output_text.delta", delta="ok")

            return events()

    adapter = OpenAIModelAdapter(api_key="test", model="primary", fallback_model="backup")
    responses = Responses()
    adapter.client = SimpleNamespace(responses=responses)
    deltas = [delta async for delta in adapter.stream_turn("instructions", [], [])]
    assert responses.models == ["primary", "backup"]
    assert [delta.type for delta in deltas] == ["model_fallback", "text_delta", "done"]
    assert deltas[0].raw["to_model"] == "backup"
