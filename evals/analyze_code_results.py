"""Summarize code-eval reports and flag trajectories that deserve review."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any


def tool_error_breakdown(result: dict[str, Any]) -> Counter[str]:
    """Inspect persisted tool results without relying on model text or reasoning."""
    root = result.get("artifacts")
    if not root:
        return Counter()
    path = Path(str(root)) / "session.jsonl"
    if not path.is_file():
        return Counter()
    counts: Counter[str] = Counter()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                entry = json.loads(line)
                if entry.get("type") != "tool_result":
                    continue
                payload = entry.get("payload", {})
                failed = payload.get("is_error") or (
                    isinstance(payload.get("exit_code"), int) and payload["exit_code"] != 0)
                if not failed:
                    continue
                name = str(payload.get("tool_name") or "unknown")
                output = str(payload.get("output") or "")
                if "FileExistsError" in output:
                    reason = "已有文件仍调用创建"
                elif "Project handoff has not been created" in output:
                    reason = "读取不存在的 handoff"
                elif "dubious ownership" in output:
                    reason = "Git ownership 环境错误"
                elif "KeyError" in output and name == "apply_patch":
                    reason = "补丁参数不匹配"
                else:
                    reason = "其他工具错误"
                counts[f"{name}：{reason}"] += 1
    except (OSError, ValueError):
        return Counter()
    return counts


def trajectory_signals(result: dict[str, Any]) -> list[str]:
    """Heuristics, never the pass/fail oracle."""
    signals: list[str] = []
    if result.get("write_calls", 0) and not result.get("successful_test_after_edit"):
        signals.append("修改后无成功测试")
    if result.get("repeated_tool_calls", 0) >= 3:
        signals.append("重复工具调用较多")
    if result.get("failed_tests", 0) and not result.get("successful_test_after_edit"):
        signals.append("测试失败后未见成功验证")
    if result.get("tool_calls", 0) >= 5 and result.get("tool_errors", 0) / result["tool_calls"] >= 0.3:
        signals.append("工具错误比例较高")
    if any(item.get("decision") == "deny" for item in result.get("approval_decisions", [])):
        signals.append("有审批被拒")
    avoidable = {"已有文件仍调用创建", "读取不存在的 handoff", "Git ownership 环境错误", "补丁参数不匹配"}
    if any(any(reason in label for reason in avoidable) for label in tool_error_breakdown(result)):
        signals.append("存在可避免的工具错误")
    artifact_root = result.get("artifacts")
    artifact = Path(str(artifact_root)) / "trajectory.json" if artifact_root else None
    if artifact is not None and artifact.is_file():
        try:
            timeline = json.loads(artifact.read_text(encoding="utf-8"))["timeline"]
        except (OSError, ValueError, KeyError, TypeError):
            timeline = []
        edit = next((item["seq"] for item in timeline if item.get("type") == "tool_call"
                     and item.get("name") in {"apply_patch", "create_file", "delete_file"}), None)
        reads = [item["seq"] for item in timeline if item.get("type") == "tool_call"
                 and item.get("name") in {"list_files", "search_code", "read_file", "git_status", "git_diff"}]
        if edit is not None and not any(seq < edit for seq in reads):
            signals.append("首次显式编辑前未见仓库读取")
    return signals


def render_report(report: dict[str, Any], source: Path) -> str:
    results = report.get("results", [])
    by_task: dict[str, list[dict[str, Any]]] = {}
    for item in results:
        by_task.setdefault(str(item["task"]), []).append(item)
    passed = [item for item in results if item.get("passed")]
    failure_counts = Counter(item.get("failure_category", "unknown") for item in results if not item.get("passed"))
    signal_counts = Counter(signal for item in results for signal in trajectory_signals(item))
    tool_errors = sum((tool_error_breakdown(item) for item in results), Counter())
    median_seconds = statistics.median(item["elapsed_seconds"] for item in passed) if passed else None
    median_tokens = statistics.median(item.get("model_usage", {}).get("total_tokens", 0)
                                      for item in passed) if passed else None
    lines = [
        f"# TraceForge 代码任务评测：{source.name}",
        "",
        f"- 模型：{report.get('model', 'unknown')}",
        f"- 套件：{report.get('suite', 'unknown')}；完成运行：{len(results)}",
        f"- 成功：{len(passed)}/{len(results)}；全重复通过的任务："
        f"{sum(all(item.get('passed') for item in group) for group in by_task.values())}/{len(by_task)}",
        f"- 成功运行的中位耗时：{median_seconds if median_seconds is not None else '无'} 秒；"
        f"中位 token：{median_tokens if median_tokens is not None else '无'}",
        f"- 工具错误总数：{sum(tool_errors.values())}；人工审批次数："
        f"{sum(len(item.get('approval_decisions', [])) for item in results)}",
        "",
        "## 逐题结果",
        "",
        "| 任务 | 成功/次数 | 失败类别 |",
        "| --- | ---: | --- |",
    ]
    for name, group in sorted(by_task.items()):
        failures = Counter(item.get("failure_category", "unknown") for item in group if not item.get("passed"))
        detail = "、".join(f"{kind}×{count}" for kind, count in sorted(failures.items())) or "—"
        lines.append(f"| {name} | {sum(bool(item.get('passed')) for item in group)}/{len(group)} | {detail} |")
    lines.extend(["", "## 失败类别", ""])
    lines.extend(f"- {kind}: {count}" for kind, count in sorted(failure_counts.items()))
    if not failure_counts:
        lines.append("- 无")
    lines.extend(["", "## 工具错误分布", ""])
    lines.extend(f"- {kind}: {count}" for kind, count in tool_errors.most_common())
    if not tool_errors:
        lines.append("- 无")
    lines.extend(["", "## 轨迹复核信号", "",
                  "下列信号由规则提取，不能单独说明失败原因；请结合 session.jsonl、补丁和测试输出复核。", ""])
    lines.extend(f"- {kind}: {count}" for kind, count in sorted(signal_counts.items()))
    if not signal_counts:
        lines.append("- 无")
    lines.extend(["", "## 待复核运行", ""])
    for item in results:
        signals = trajectory_signals(item)
        if item.get("passed") and not signals:
            continue
        artifact = item.get("artifacts", "")
        lines.append(
            f"- {item['task']} 第 {item['repeat']} 次："
            f"{'通过' if item.get('passed') else item.get('failure_category', '失败')}；"
            f"{'、'.join(signals) if signals else '无规则信号'}；轨迹：`{artifact}`"
        )
    if all(item.get("passed") and not trajectory_signals(item) for item in results):
        lines.append("- 无")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    markdown = render_report(report, args.report)
    output = args.output or args.report.with_suffix(".md")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
