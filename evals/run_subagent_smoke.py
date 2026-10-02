"""A real-model smoke check for project-scoped JSONL history delegation."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from traceforge.config import Settings  # noqa: E402
from traceforge.main import create_app  # noqa: E402
from traceforge.models import new_id  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evals/results/subagent-history-smoke.json")
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="traceforge-subagent-eval-") as temp:
        base = Path(temp)
        app = create_app(replace(Settings.from_env(), data_dir=base / "data"))
        services = app.state.services
        model = services.model_settings.status()
        if not model["configured"]:
            print("Model not configured", file=sys.stderr)
            return 2
        repo = base / "repo"
        repo.mkdir()
        project = services.workspaces.register(str(repo))
        old = services.sessions.create(project)
        root = await old.append("user_message", {"content": "讨论 TF_MEMORY_DESIGN 的会话存储和压缩机制"})
        abandoned = await old.append("assistant_message", {"content": "TF_MEMORY_DESIGN 早期提案：采用 SQLite 和滚动摘要。"})
        final = await old.append("assistant_message", {
            "content": "TF_MEMORY_DESIGN 用户最终确认：每个会话一个 JSONL 文件，保留原始记录；不用 SQLite。"
                       "取消滚动摘要，compact 更新项目唯一 .traceforge/handoff.md，新会话只提示文件存在，模型按需读取；"
                       "需要精确历史时搜索 JSONL 原文。",
        }, parent_id=root.id)
        parent = services.sessions.create(project)
        await parent.append("user_message", {"content": "找回 TF_MEMORY_DESIGN 的最终决定，并区分放弃的提案。"})
        run_id, call_id = new_id("run"), new_id("call")
        await parent.append("tool_call", {"call_id": call_id, "name": "delegate_task", "arguments": {
            "role": "explore", "task": "回查 TF_MEMORY_DESIGN 的 JSONL 原文，说明最终存储和 compact 机制；"
                                     "区分放弃的分支提案。必须读取原文并返回准确 session_id/entry_id 证据。",
            "context_entry_ids": [],
        }}, run_id=run_id)
        tool = parent.entries[-1]
        result = await services.runner.subagents.delegate(
            parent, run_id, call_id, tool.payload["arguments"],
            context_window=model["effective_context_window"], budget_check=lambda: True,
        )
        await parent.append("tool_result", {"call_id": call_id, "tool_name": "delegate_task", **result.model_dump(mode="json")}, run_id=run_id)
        report = json.loads(result.output)
        cited = {(ref["session_id"], ref["entry_id"]) for finding in report["findings"] for ref in finding["evidence"]}
        checks = {"completed": report["status"] == "completed",
                  "cites_final_original": (old.header.id, final.id) in cited,
                  "mentions_jsonl_and_handoff": "jsonl" in result.output.lower() and "handoff" in result.output.lower(),
                  "has_snapshot": bool(report.get("snapshot_id")),
                  "no_write_or_shell_tools": True}
        child = services.runner.subagents.get(parent.header.id, report["child_session_id"])
        calls = [entry.payload["name"] for entry in child.entries if entry.type == "tool_call"]
        checks["no_write_or_shell_tools"] = not any(name in {"run_command", "apply_patch", "create_file", "delegate_task"} for name in calls)
        artifacts = output.with_suffix("")
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "child.jsonl").write_bytes(child.path.read_bytes())
        (artifacts / "parent.jsonl").write_bytes(parent.path.read_bytes())
        (artifacts / "source.jsonl").write_bytes(old.path.read_bytes())
        evaluation = {"passed": all(checks.values()), "checks": checks, "model": model["model"],
                      "report": report, "child_tools": calls, "original_final_entry_id": final.id,
                      "abandoned_entry_id": abandoned.id, "artifacts": str(artifacts)}
        output.write_text(json.dumps(evaluation, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": evaluation["passed"], "checks": checks, "output": str(output)}, ensure_ascii=False))
        return 0 if evaluation["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
