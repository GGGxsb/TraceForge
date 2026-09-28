"""Run end-to-end code-agent tasks in fresh Git repositories with hidden checks."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from code_tasks import TASKS, CodeTask  # noqa: E402
from traceforge.config import Settings  # noqa: E402
from traceforge.main import create_app  # noqa: E402
from traceforge.models import ExecutionRequest  # noqa: E402


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            timeout=30, check=True)
    return result.stdout


def make_repo(root: Path, task: CodeTask) -> Path:
    workspace = root / "repo"
    workspace.mkdir()
    git(workspace, "init")
    for name, content in task.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (workspace / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    git(workspace, "add", ".")
    git(workspace, "-c", "user.name=TraceForge Eval", "-c", "user.email=eval@example.invalid",
        "commit", "-m", "fixture")
    return workspace


async def run_one(task: CodeTask, repeat: int, *, timeout: int) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"traceforge-eval-{task.id}-") as temp:
        root = Path(temp)
        workspace = make_repo(root, task)
        settings = replace(Settings.from_env(), data_dir=root / "data")
        app = create_app(settings)
        services = app.state.services
        record = services.workspaces.register(str(workspace))
        session = services.sessions.create(record)
        started = time.monotonic()
        run = services.runner.start(session.header.id, task.prompt)
        approval_decisions: list[dict[str, str]] = []

        async def resolve_fixture_approvals() -> None:
            seen: set[str] = set()
            while not run.task.done():
                for entry in session.entries:
                    if entry.type != "approval_request":
                        continue
                    approval_id = str(entry.payload.get("approval_id", ""))
                    if not approval_id or approval_id in seen:
                        continue
                    seen.add(approval_id)
                    policy = entry.payload.get("policy", {})
                    capabilities = set(policy.get("capabilities", []))
                    forbidden = {"network_access", "credential_access", "privilege_or_host_control",
                                 "git_write", "dependency_install", "workspace_escape",
                                 "destructive_operation"}
                    decision = "allow_once" if task.hidden_test and not capabilities & forbidden else "deny"
                    services.approvals.decide(session.header.id, approval_id, decision,
                                              str(policy.get("fingerprint", "")))
                    approval_decisions.append({"tool": str(entry.payload.get("tool_name", "")),
                                               "decision": decision})
                await asyncio.sleep(0.05)

        approval_monitor = asyncio.create_task(resolve_fixture_approvals())
        timed_out = False
        try:
            await asyncio.wait_for(run.task, timeout=timeout)
        except TimeoutError:
            timed_out = True
            await services.runner.cancel(run.id)
        finally:
            approval_monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await approval_monitor
        elapsed = round(time.monotonic() - started, 3)
        status = next((entry.payload for entry in reversed(session.entries)
                       if entry.type == "run_state" and entry.payload.get("status") in
                       {"completed", "failed", "cancelled"}), {})
        answers = [str(entry.payload.get("content", "")) for entry in session.get_branch()
                   if entry.type == "assistant_message"]
        changed = [line[3:] for line in git(workspace, "status", "--porcelain").splitlines() if len(line) > 3]
        unrelated = sorted(set(changed) - set(task.expected_paths))
        hidden_exit: int | None = None
        hidden_output = ""
        if task.hidden_test is not None and not timed_out:
            hidden_path = Path(session.workspace) / "test_hidden.py"
            hidden_path.write_text(task.hidden_test, encoding="utf-8")
            try:
                check = await services.sandbox.execute(ExecutionRequest(
                    workspace=session.workspace, command="python3 -m unittest -q test_hidden",
                    network=False, timeout_seconds=90,
                ))
                hidden_exit = check.exit_code
                hidden_output = (check.stdout + "\n" + check.stderr)[-4000:]
            finally:
                hidden_path.unlink(missing_ok=True)
        passed = (
            status.get("status") == "completed"
            and not timed_out
            and not unrelated
            and (hidden_exit == 0 if task.hidden_test is not None else
                 bool(task.answer_contains and any(task.answer_contains in text for text in answers)) and not changed)
        )
        return {
            "task": task.id, "repeat": repeat, "passed": passed,
            "status": status.get("status", "unknown"), "outcome": status.get("outcome"),
            "elapsed_seconds": elapsed, "changed_files": changed,
            "unrelated_files": unrelated,
            "hidden_exit_code": hidden_exit, "hidden_output": hidden_output,
            "model_usage": status.get("usage", {}),
            "estimated_cost_usd": status.get("estimated_cost_usd"),
            "tool_calls": status.get("tool_calls", 0), "tool_errors": status.get("tool_errors", 0),
            "tool_error_details": [
                {"tool": entry.payload.get("tool_name"), "output": str(entry.payload.get("output", ""))[:500]}
                for entry in session.entries if entry.type == "tool_result" and entry.payload.get("is_error")
            ],
            "approval_decisions": approval_decisions,
            "recent_events": [{"type": entry.type, "tool": entry.payload.get("name") or
                               entry.payload.get("tool_name"), "status": entry.payload.get("status")}
                              for entry in session.entries[-20:]],
            "reasoning_items": [
                {"seq": entry.seq, "keys": sorted(entry.payload.get("item", {}).keys()),
                 "content_types": [part.get("type") for part in entry.payload.get("item", {}).get("content", [])]}
                for entry in session.entries if entry.type == "model_reasoning"
            ],
            "failure": status.get("error") or ("timeout" if timed_out else None),
        }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--task", action="append", choices=[task.id for task in TASKS])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1 or args.timeout < 1:
        parser.error("repeats and timeout must be positive")
    load_dotenv(ROOT / ".env")
    selected = [task for task in TASKS if not args.task or task.id in args.task]
    preflight = create_app(Settings.from_env())
    services = preflight.state.services
    capabilities = await services.sandbox.inspect()
    if not capabilities.ready and any(task.hidden_test is not None for task in selected):
        print(f"Sandbox unavailable: {capabilities.reason}", file=sys.stderr)
        return 2
    if not services.model_settings.status()["configured"]:
        print("Model not configured", file=sys.stderr)
        return 2
    results = []
    for task in selected:
        for repeat in range(1, args.repeats + 1):
            print(f"Running {task.id} ({repeat}/{args.repeats})...", flush=True)
            result = await run_one(task, repeat, timeout=args.timeout)
            results.append(result)
            print(f"  {'PASS' if result['passed'] else 'FAIL'}; {result['elapsed_seconds']}s", flush=True)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": services.model_settings.status()["model"],
        "repeats": args.repeats, "passed": sum(item["passed"] for item in results),
        "total": len(results), "results": results,
    }
    output = args.output or ROOT / "evals" / "results" / f"code-tasks-{datetime.now():%Y%m%d-%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{report['passed']}/{report['total']} passed; report: {output}")
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
