"""Run a small, reproducible live-model evaluation of TraceForge's TaskBrief gate.

The report contains prompts, decisions, and questions but never model credentials.
No agent tools or workspace mutations are performed by this gate-level evaluation.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from traceforge.config import Settings
from traceforge.model_settings import ModelSettingsStore
from traceforge.models import HookContext, HookPoint, RunStatus
from traceforge.task_brief import TaskBriefHook


HERE = Path(__file__).resolve().parent


def hook_context(point: HookPoint, prompt: str, state: dict, evidence: list[dict] | None = None) -> HookContext:
    return HookContext(
        session_id="eval_session",
        run_id="eval_run",
        point=point,
        status=RunStatus(state.get("brief_phase", RunStatus.BRIEFING.value)),
        user_text=prompt,
        evidence=evidence or [],
        state=state,
    )


async def evaluate_one(hook: TaskBriefHook, case: dict, workspace: Path, timeout: float, followup: bool) -> dict:
    prompt = case["prompt"]
    state = {"workspace": str(workspace)}
    started = time.monotonic()
    result = {key: case[key] for key in ("id", "category", "prompt", "ask_user")}
    if "required_fact" in case:
        result["required_fact"] = case["required_fact"]
    try:
        committed = await asyncio.wait_for(
            hook.handle(hook_context(HookPoint.USER_MESSAGE_COMMITTED, prompt, state)), timeout
        )
        state.update(committed.state_updates)
        decision = await asyncio.wait_for(
            hook.handle(hook_context(HookPoint.BEFORE_MODEL_REQUEST, prompt, state)), timeout
        )
        state.update(decision.state_updates)
        initial_phase = state.get("brief_phase")
        evidence = case.get("repo_evidence", [])
        result["discovery_evidence_injected"] = bool(initial_phase == RunStatus.DISCOVERING.value and evidence)
        if initial_phase == RunStatus.DISCOVERING.value and evidence:
            batch = await asyncio.wait_for(
                hook.handle(hook_context(HookPoint.AFTER_TOOL_BATCH, prompt, state, evidence)), timeout
            )
            state.update(batch.state_updates)
            followup = await asyncio.wait_for(
                hook.handle(hook_context(HookPoint.BEFORE_MODEL_REQUEST, prompt, state, evidence)), timeout
            )
            state.update(followup.state_updates)
        phase = state.get("brief_phase")
        brief = state.get("task_brief", {})
        selected_gap_ids = state.get("ask_gap_ids")
        selected_points = [
            point for point in brief.get("missing_points", [])
            if selected_gap_ids is None or point.get("id") in selected_gap_ids
        ]
        questions = [
            point.get("question", "").strip()
            or f"请确认：{point.get('description', '').strip().rstrip('。？?')}？"
            for point in selected_points
            if point.get("question", "").strip() or point.get("description", "").strip()
        ] if phase == RunStatus.CLARIFYING.value else []
        observed_ask = phase == RunStatus.CLARIFYING.value
        result.update(
            initial_phase=initial_phase,
            phase=phase,
            observed_ask=observed_ask,
            correct=observed_ask == case["ask_user"] and (not observed_ask or bool(questions)),
            reason=brief.get("reason", ""),
            gaps=[
                {
                    "description": point.get("description", ""),
                    "discoverable_from_repo": point.get("discoverable_from_repo", False),
                    "impact": point.get("impact", ""),
                }
                for point in brief.get("missing_points", [])
            ],
            questions=questions,
        )
        if followup and observed_ask and questions and case.get("oracle_answer"):
            answered_request = (
                f"原始需求：\n{prompt}\n\n用户已经提供的澄清：\n"
                + "\n".join(f"- {question}：{case['oracle_answer']}" for question in questions)
            )
            answer_state = {"workspace": str(workspace)}
            answer_commit = await asyncio.wait_for(
                hook.handle(hook_context(HookPoint.USER_MESSAGE_COMMITTED, answered_request, answer_state)), timeout
            )
            answer_state.update(answer_commit.state_updates)
            answer_decision = await asyncio.wait_for(
                hook.handle(hook_context(HookPoint.BEFORE_MODEL_REQUEST, answered_request, answer_state)), timeout
            )
            answer_state.update(answer_decision.state_updates)
            answer_phase = answer_state.get("brief_phase")
            expected_answer_phase = case.get("answer_expected_phase", RunStatus.EXECUTING.value)
            answer_brief = answer_state.get("task_brief", {})
            answer_gap_ids = answer_state.get("ask_gap_ids")
            result["answer_recheck"] = {
                "phase": answer_phase,
                "expected_phase": expected_answer_phase,
                "correct": answer_phase == expected_answer_phase,
                "resumed_without_reasking": answer_phase == RunStatus.EXECUTING.value,
                "reason": answer_brief.get("reason", ""),
                "questions": [
                    point.get("question", "").strip()
                    for point in answer_brief.get("missing_points", [])
                    if answer_phase == RunStatus.CLARIFYING.value
                    and (answer_gap_ids is None or point.get("id") in answer_gap_ids)
                    and point.get("question", "").strip()
                ],
            }
    except Exception as exc:  # noqa: BLE001 - do not persist provider errors that might echo request data
        result.update(error=type(exc).__name__, correct=False)
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return result


def summarize(results: list[dict]) -> dict:
    completed = [item for item in results if "error" not in item]
    confusion = Counter((item["ask_user"], item["observed_ask"]) for item in completed)
    tp, fn, fp, tn = (confusion[(True, True)], confusion[(True, False)],
                      confusion[(False, True)], confusion[(False, False)])
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    answer_rechecks = [item["answer_recheck"] for item in completed if "answer_recheck" in item]
    asked_cases = [item for item in completed if item.get("questions")]
    chinese_asked_cases = [item for item in asked_cases if re.search(r"[\u4e00-\u9fff]", item["prompt"])]
    return {
        "cases": len(results),
        "completed": len(completed),
        "errors": len(results) - len(completed),
        "correct": sum(bool(item["correct"]) for item in completed),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "ask_decision_precision": round(precision, 3),
        "ask_decision_recall": round(recall, 3),
        "ask_decision_f1": round(2 * precision * recall / (precision + recall), 3) if precision + recall else 0.0,
        "unnecessary_ask_rate": round(fp / (fp + tn), 3) if fp + tn else 0.0,
        "answer_rechecks": len(answer_rechecks),
        "answer_recheck_correct": sum(item["correct"] for item in answer_rechecks),
        "resumed_without_reasking": sum(item["resumed_without_reasking"] for item in answer_rechecks),
        "questions": sum(len(item["questions"]) for item in asked_cases),
        "chinese_prompts_with_english_only_questions": sum(
            not re.search(r"[\u4e00-\u9fff]", " ".join(item["questions"]))
            for item in chinese_asked_cases
        ),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=HERE / "clarification_cases.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ids", help="Comma-separated case IDs to run")
    parser.add_argument("--followup", action="store_true", help="Recheck cases with a scripted user answer")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    load_dotenv(HERE.parent / ".env")
    settings = Settings.from_env()
    store = ModelSettingsStore(
        settings.model_config_dir / "model-settings.json",
        env_api_key=settings.openai_api_key,
        env_model=settings.openai_model,
        env_base_url=settings.openai_base_url,
        env_brief_model=settings.brief_model,
    )
    status = store.status()
    if not status["configured"]:
        raise SystemExit("TraceForge model is not configured")
    case_bytes = args.cases.read_bytes()
    cases = json.loads(case_bytes.decode("utf-8"))
    if args.ids:
        selected = set(args.ids.split(","))
        cases = [case for case in cases if case["id"] in selected]
    if args.limit is not None:
        cases = cases[:args.limit]
    hook = TaskBriefHook(store.adapter)
    results = []
    for index, case in enumerate(cases, 1):
        result = await evaluate_one(hook, case, HERE.parent, args.timeout, args.followup)
        results.append(result)
        label = "ERROR" if "error" in result else "PASS" if result["correct"] else "FAIL"
        print(f"[{index}/{len(cases)}] {label} {case['id']} -> {result.get('phase', result.get('error', 'unknown'))}", flush=True)

    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "scope": "TaskBrief gate and generated clarification questions; no full agent tool execution",
        "evidence_note": "repo_evidence is controlled, simulated read-only tool output; it is not an actual agent tool trace",
        "case_file": str(args.cases),
        "case_sha256": hashlib.sha256(case_bytes).hexdigest(),
        "model": status["brief_model"],
        "base_host": urlsplit(status["base_url"]).hostname if status["base_url"] else "default",
        "summary": summarize(results),
        "results": results,
    }
    output = args.output or HERE / "results" / f"clarification-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"REPORT {output}", flush=True)
    print("SUMMARY " + json.dumps(report["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
