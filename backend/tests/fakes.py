from __future__ import annotations

from collections.abc import AsyncIterator

from traceforge.model_adapter import ModelDelta
from traceforge.models import BranchSummary, CompactionSummary, TaskBrief


class FakeModelAdapter:
    def __init__(self, briefs: list[TaskBrief] | None = None, turns: list[list[ModelDelta]] | None = None) -> None:
        self.briefs = list(briefs or [TaskBrief()])
        self.turns = list(turns or [[ModelDelta(type="text_delta", text="done"), ModelDelta(type="done")]])
        self.brief_calls = 0

    async def analyze_task_brief(self, user_text, evidence, previous):
        self.brief_calls += 1
        if len(self.briefs) > 1:
            return self.briefs.pop(0)
        return self.briefs[0]

    async def stream_turn(self, instructions, input_items, tools) -> AsyncIterator[ModelDelta]:
        turn = self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]
        for delta in turn:
            yield delta

    async def summarize_compaction(self, transcript, previous):
        return CompactionSummary(
            goal="test",
            completed=["history summarized"],
            read_files=["README.md"],
            next_steps=["continue"],
        )

    async def summarize_branch(self, transcript):
        return BranchSummary(
            branch_goal="test branch",
            completed=["branch work"],
            recommended_next_step="continue from target",
        )

