"""Run reproducible TraceForge coding evaluations with clean-room grading."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from code_tasks import TASKS, CodeTask  # noqa: E402
from complex_tasks import COMPLEX_TASKS  # noqa: E402
from traceforge.config import Settings  # noqa: E402
from traceforge.main import create_app  # noqa: E402
from traceforge.models import ExecutionRequest  # noqa: E402

ALL_TASKS = (*TASKS, *COMPLEX_TASKS)
TEST_COMMAND = re.compile(r"\b(?:pytest|unittest|npm\s+test|pnpm\s+test|cargo\s+test|go\s+test)\b", re.I)
WRITE_TOOLS = {"apply_patch", "create_file", "delete_file"}
READ_TOOLS = {"list_files", "search_code", "read_file", "git_status", "git_diff"}
FORBIDDEN_APPROVAL_CAPABILITIES = {
    "network_access", "credential_access", "privilege_or_host_control", "git_write",
    "dependency_install", "workspace_escape", "destructive_operation",
}


def git(repo: Path, *args: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, timeout=30, check=True,
        **({} if binary else {"text": True, "errors": "replace"}),
    )
    return result.stdout


def fixture_digest(task: CodeTask) -> str:
    data = {"prompt": task.prompt, "files": task.files, "hidden_test": task.hidden_test,
            "answer_contains": task.answer_contains, "expected_paths": task.expected_paths,
            "reference_files": task.reference_files}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def make_repo(root: Path, task: CodeTask) -> Path:
    workspace = root / "repo"
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True, timeout=30)
    git(workspace, "config", "core.autocrlf", "false")
    for name, content in task.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    (workspace / ".gitignore").write_bytes(b"__pycache__/\n*.pyc\n")
    git(workspace, "add", ".")
    subprocess.run(
        ["git", "-C", str(workspace), "-c", "user.name=TraceForge Eval",
         "-c", "user.email=eval@example.invalid", "commit", "-q", "-m", "fixture"],
        check=True, timeout=30,
        env={**os.environ, "GIT_AUTHOR_DATE": "2020-01-01T00:00:00+00:00",
             "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+00:00"},
    )
    return workspace


def _changed_paths(repo: Path, base_commit: str) -> list[str]:
    tracked = git(repo, "diff", "--name-only", "-z", "--no-renames", base_commit, binary=True)
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z", binary=True)
    return sorted({path.decode("utf-8", "surrogateescape")
                   for path in (tracked + untracked).split(b"\0") if path})


def _safe_relative(name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or not path.parts or any(part in {".", "..", ".git"} for part in path.parts):
        raise ValueError(f"Unsafe changed path: {name}")
    return path


def apply_worktree_changes(source: Path, target: Path, base_commit: str) -> list[str]:
    """Copy the final code state onto a pristine fixture, including untracked files."""
    changed = _changed_paths(source, base_commit)
    for name in changed:
        relative = _safe_relative(name)
        source_file = source / relative
        target_file = target / relative
        if source_file.is_symlink() or target_file.is_symlink():
            raise ValueError(f"Symlinks are not supported in benchmark patches: {name}")
        if source_file.exists():
            if not source_file.is_file():
                raise ValueError(f"Non-file benchmark patch: {name}")
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, target_file)
        elif target_file.exists():
            target_file.unlink()
    return changed


async def hidden_check(sandbox: Any | None, workspace: Path, task: CodeTask) -> tuple[int | None, str]:
    if task.hidden_test is None:
        return None, ""
    hidden_path = workspace / "test_hidden.py"
    if hidden_path.exists():
        raise ValueError("Agent patch collides with hidden test path")
    hidden_path.write_bytes(task.hidden_test.encode("utf-8"))
    try:
        if sandbox is None:
            # Only --validate-only may run our checked-in fixture and gold code locally.
            # Agent-generated patches always require the sandbox path below.
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "-q", "test_hidden"],
                cwd=workspace, capture_output=True, text=True, timeout=90, check=False,
            )
            return result.returncode, (result.stdout + "\n" + result.stderr)[-4000:]
        result = await sandbox.execute(ExecutionRequest(
            workspace=str(workspace), command="python3 -m unittest -q test_hidden",
            network=False, timeout_seconds=90,
        ))
        return result.exit_code, (result.stdout + "\n" + result.stderr)[-4000:]
    finally:
        hidden_path.unlink(missing_ok=True)


async def public_check(sandbox: Any | None, workspace: Path, task: CodeTask) -> tuple[int | None, str]:
    modules = [Path(name).stem for name in task.files if name.startswith("test_") and name.endswith(".py")]
    if not modules:
        return None, ""
    if sandbox is None:
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "-q", *modules],
            cwd=workspace, capture_output=True, text=True, timeout=90, check=False,
        )
        return result.returncode, (result.stdout + "\n" + result.stderr)[-4000:]
    result = await sandbox.execute(ExecutionRequest(
        workspace=str(workspace), command="python3 -m unittest -q " + " ".join(modules),
        network=False, timeout_seconds=90,
    ))
    return result.exit_code, (result.stdout + "\n" + result.stderr)[-4000:]


async def validate_fixture(task: CodeTask, sandbox: Any) -> dict[str, Any]:
    """Reject cases whose hidden checks pass on empty code or fail on the gold solution."""
    with tempfile.TemporaryDirectory(prefix=f"traceforge-fixture-{task.id}-") as temp:
        base = make_repo(Path(temp) / "empty", task)
        commit = str(git(base, "rev-parse", "HEAD")).strip()
        base_public_exit, base_public_output = await public_check(sandbox, base, task)
        if base_public_exit not in {None, 0}:
            raise ValueError(f"{task.id}: existing tests fail on base fixture:\n{base_public_output}")
        if task.hidden_test is None:
            return {"task": task.id, "fixture_sha256": fixture_digest(task),
                    "base_commit": commit, "empty_exit_code": None, "reference_exit_code": None}
        empty_exit, _ = await hidden_check(sandbox, base, task)
        if empty_exit == 0:
            raise ValueError(f"{task.id}: hidden test passes on empty patch")
        gold = None
        if task.reference_files is not None:
            reference = make_repo(Path(temp) / "reference", task)
            for name, content in task.reference_files.items():
                target = reference / _safe_relative(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content.encode("utf-8"))
            gold, output = await hidden_check(sandbox, reference, task)
            if gold != 0:
                raise ValueError(f"{task.id}: reference solution fails hidden test:\n{output}")
            public_exit, public_output = await public_check(sandbox, reference, task)
            if public_exit not in {None, 0}:
                raise ValueError(f"{task.id}: reference solution breaks public tests:\n{public_output}")
        return {"task": task.id, "fixture_sha256": fixture_digest(task),
                "base_commit": commit, "empty_exit_code": empty_exit,
                "reference_exit_code": gold}


def trajectory(entries: list[Any]) -> dict[str, Any]:
    calls: dict[str, Any] = {}
    seen: set[tuple[str, str]] = set()
    repeated = read_calls = write_calls = test_calls = failed_tests = tool_errors = 0
    first_edit_seq: int | None = None
    first_test_seq: int | None = None
    successful_test_after_edit = False
    timeline: list[dict[str, Any]] = []
    for entry in entries:
        payload = entry.payload
        if entry.type == "tool_call":
            name = str(payload.get("name", ""))
            args = payload.get("arguments") or {}
            call_id = str(payload.get("call_id", ""))
            calls[call_id] = entry
            fingerprint = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
            repeated += fingerprint in seen
            seen.add(fingerprint)
            read_calls += name in READ_TOOLS
            write_calls += name in WRITE_TOOLS
            is_test = name == "run_command" and bool(TEST_COMMAND.search(str(args.get("command", ""))))
            test_calls += is_test
            if name in WRITE_TOOLS and first_edit_seq is None:
                first_edit_seq = entry.seq
            if is_test and first_test_seq is None:
                first_test_seq = entry.seq
            timeline.append({"seq": entry.seq, "type": "tool_call", "name": name,
                             "call_id": call_id, "test": is_test})
        elif entry.type == "tool_result":
            call = calls.get(str(payload.get("call_id", "")))
            failed = bool(payload.get("is_error")) or (
                isinstance(payload.get("exit_code"), int) and payload["exit_code"] != 0)
            tool_errors += failed
            is_test = bool(call and call.payload.get("name") == "run_command" and TEST_COMMAND.search(
                str((call.payload.get("arguments") or {}).get("command", ""))))
            failed_tests += bool(is_test and failed)
            successful_test_after_edit |= bool(is_test and not failed and first_edit_seq is not None
                                               and entry.seq > first_edit_seq)
            timeline.append({"seq": entry.seq, "type": "tool_result",
                             "call_id": payload.get("call_id"), "error": failed,
                             "exit_code": payload.get("exit_code")})
        elif entry.type in {"run_state", "context_checkpoint", "approval_request", "approval_decision"}:
            timeline.append({"seq": entry.seq, "type": entry.type,
                             "status": payload.get("status"), "outcome": payload.get("outcome")})
    return {"read_calls": read_calls, "write_calls": write_calls, "test_calls": test_calls,
            "failed_tests": failed_tests, "tool_errors": tool_errors, "repeated_tool_calls": repeated,
            "first_edit_seq": first_edit_seq, "first_test_seq": first_test_seq,
            "successful_test_after_edit": successful_test_after_edit, "timeline": timeline}


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in results:
        groups.setdefault(item["task"], []).append(item)
    successful = [item for item in results if item["passed"]]
    return {
        "passes": len(successful), "runs": len(results),
        "pass_rate": len(successful) / len(results) if results else 0,
        "tasks_all_repeats_passed": sum(all(item["passed"] for item in group)
                                        for group in groups.values()),
        "tasks": {name: {"passes": sum(item["passed"] for item in group), "runs": len(group),
                         "failure_counts": {
                             reason: sum(item["failure_category"] == reason for item in group)
                             for reason in sorted({item["failure_category"] for item in group if not item["passed"]})
                         }} for name, group in groups.items()},
        "successful_median_seconds": statistics.median(item["elapsed_seconds"] for item in successful)
        if successful else None,
        "successful_median_tokens": statistics.median(item["model_usage"].get("total_tokens", 0)
                                                       for item in successful) if successful else None,
    }


def failure_category(*, passed: bool, timed_out: bool, status: dict[str, Any],
                     unrelated: list[str], hidden_exit: int | None,
                     read_only_failed: bool, grading_error: str | None = None) -> str:
    if passed:
        return "passed"
    if timed_out:
        return "timeout"
    if grading_error:
        return "grading_error"
    if status.get("status") == "failed":
        return "agent_failed"
    if status.get("outcome") in {"round_limit", "token_budget", "cost_budget"}:
        return str(status["outcome"])
    if status.get("outcome") == "clarification":
        return "clarification_blocked"
    if unrelated:
        return "out_of_scope_changes"
    if hidden_exit is not None:
        return "hidden_test_failed"
    if read_only_failed:
        return "read_only_answer_failed"
    return "incomplete"


async def regrade_report(original: dict[str, Any], selected: list[CodeTask],
                         sandbox: Any, source: Path,
                         fixture_checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Re-run only the verifier on saved patches; keep original Agent trajectories."""
    by_id = {task.id: task for task in selected}
    updated = []
    for old in original["results"]:
        task = by_id[old["task"]]
        with tempfile.TemporaryDirectory(prefix=f"traceforge-regrade-{task.id}-") as temp:
            grader = make_repo(Path(temp) / "grader", task)
            base_commit = str(git(grader, "rev-parse", "HEAD")).strip()
            if base_commit != old["base_commit"]:
                raise ValueError(f"{task.id}: base commit changed; cannot regrade old patch")
            patch = Path(old["artifacts"]) / "patch.diff"
            if not patch.is_file():
                raise FileNotFoundError(f"{task.id}: missing saved patch: {patch}")
            if patch.stat().st_size:
                normalized = Path(temp) / "patch.diff"
                normalized.write_bytes(patch.read_bytes().replace(b"\r\n", b"\n"))
                git(grader, "apply", "--check", str(normalized))
                git(grader, "apply", str(normalized))
            hidden_exit, hidden_output = await hidden_check(sandbox, grader, task)
            public_exit, public_output = await public_check(sandbox, grader, task)
            passed = (old["status"] == "completed" and not old.get("unrelated_files")
                      and hidden_exit == 0 and public_exit in {None, 0})
            result = dict(old)
            result.update({
                "fixture_sha256": fixture_digest(task), "passed": passed,
                "hidden_exit_code": hidden_exit, "hidden_output": hidden_output,
                "public_exit_code": public_exit, "public_output": public_output,
                "grading_error": None,
                "failure_category": (
                    "public_regression" if public_exit not in {None, 0} else failure_category(
                        passed=passed, timed_out=old.get("failure") == "timeout",
                        status={"status": old["status"], "outcome": old.get("outcome")},
                        unrelated=old.get("unrelated_files", []), hidden_exit=hidden_exit,
                        read_only_failed=False,
                    )
                ),
            })
            updated.append(result)
    report = dict(original)
    report.update({"schema_version": 2, "regraded_at": datetime.now(timezone.utc).isoformat(),
                   "regraded_from": str(source), "results": updated,
                   "fixtures": fixture_checks, "summary": summarize_results(updated)})
    return report


async def run_one(task: CodeTask, repeat: int, *, timeout: int, artifacts_dir: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"traceforge-eval-{task.id}-") as temp:
        root = Path(temp)
        workspace = make_repo(root / "agent", task)
        base_commit = str(git(workspace, "rev-parse", "HEAD")).strip()
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
                    decision = "deny" if capabilities & FORBIDDEN_APPROVAL_CAPABILITIES else "allow_once"
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
        case_artifacts = artifacts_dir / f"{task.id}-r{repeat}"
        case_artifacts.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(session.path, case_artifacts / "session.jsonl")
        trace = trajectory(session.entries)
        (case_artifacts / "trajectory.json").write_text(
            json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
        changed = _changed_paths(workspace, base_commit)
        unrelated = sorted(set(changed) - set(task.expected_paths))
        hidden_exit: int | None = None
        hidden_output = ""
        public_exit: int | None = None
        public_output = ""
        grading_error = None
        if task.hidden_test is not None and not timed_out:
            grader = make_repo(root / "grader", task)
            try:
                apply_worktree_changes(workspace, grader, base_commit)
                git(grader, "add", "-N", "--all")
                patch = str(git(grader, "diff", "--binary", "HEAD"))
                (case_artifacts / "patch.diff").write_bytes(patch.encode("utf-8"))
                hidden_exit, hidden_output = await hidden_check(services.sandbox, grader, task)
                public_exit, public_output = await public_check(services.sandbox, grader, task)
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                grading_error = f"{type(exc).__name__}: {exc}"
        else:
            (case_artifacts / "patch.diff").write_bytes(
                str(git(workspace, "diff", "--binary", base_commit)).encode("utf-8"))
        read_only_failed = bool(task.answer_contains and (
            not any(task.answer_contains in text for text in answers) or changed))
        passed = (status.get("status") == "completed" and not timed_out and not grading_error
                  and not unrelated and (hidden_exit == 0 if task.hidden_test is not None
                                         else not read_only_failed)
                  and public_exit in {None, 0})
        return {
            "task": task.id, "suite": task.suite, "repeat": repeat, "passed": passed,
            "fixture_sha256": fixture_digest(task), "base_commit": base_commit,
            "status": status.get("status", "unknown"), "outcome": status.get("outcome"),
            "elapsed_seconds": elapsed, "changed_files": changed, "unrelated_files": unrelated,
            "hidden_exit_code": hidden_exit, "hidden_output": hidden_output,
            "public_exit_code": public_exit, "public_output": public_output,
            "grading_error": grading_error, "model_usage": status.get("usage", {}),
            "estimated_cost_usd": status.get("estimated_cost_usd"),
            "tool_calls": status.get("tool_calls", 0), "tool_errors": trace["tool_errors"],
            "read_calls": trace["read_calls"], "write_calls": trace["write_calls"],
            "test_calls": trace["test_calls"], "failed_tests": trace["failed_tests"],
            "repeated_tool_calls": trace["repeated_tool_calls"],
            "successful_test_after_edit": trace["successful_test_after_edit"],
            "approval_decisions": approval_decisions,
            "failure": status.get("error") or ("timeout" if timed_out else grading_error),
            "failure_category": failure_category(
                passed=passed, timed_out=timed_out, status=status, unrelated=unrelated,
                hidden_exit=hidden_exit, read_only_failed=read_only_failed, grading_error=grading_error,
            ) if public_exit in {None, 0} else "public_regression",
            "artifacts": str(case_artifacts),
        }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("smoke", "complex", "all"), default="complex")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--task", action="append", choices=[task.id for task in ALL_TASKS])
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--regrade", type=Path, help="Re-score saved patches without calling the model")
    parser.add_argument("--resume", type=Path, help="Continue a saved report up to --repeats runs per task")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1 or args.timeout < 1:
        parser.error("repeats and timeout must be positive")
    if args.regrade and args.resume:
        parser.error("--regrade and --resume cannot be combined")
    load_dotenv(ROOT / ".env")
    old_report = json.loads(args.regrade.read_text(encoding="utf-8")) if args.regrade else None
    resume_report = json.loads(args.resume.read_text(encoding="utf-8")) if args.resume else None
    if old_report or resume_report:
        old_ids = {item["task"] for item in (old_report or resume_report)["results"]}
        selected = [task for task in ALL_TASKS if task.id in old_ids]
        if len(selected) != len(old_ids):
            parser.error("Saved report contains unknown tasks")
    else:
        selected = [task for task in ALL_TASKS if (args.suite == "all" or task.suite == args.suite)
                    and (not args.task or task.id in args.task)]
    if not selected:
        parser.error("No task matches --suite and --task")
    preflight = create_app(Settings.from_env())
    services = preflight.state.services
    capabilities = await services.sandbox.inspect()
    if not capabilities.ready and not args.validate_only and any(task.hidden_test is not None for task in selected):
        print(f"Sandbox unavailable: {capabilities.reason}", file=sys.stderr)
        return 2
    if not args.validate_only and not args.regrade and not services.model_settings.status()["configured"]:
        print("Model not configured", file=sys.stderr)
        return 2
    fixture_checks = []
    fixture_executor = services.sandbox if capabilities.ready else None
    if fixture_executor is None:
        print("Sandbox unavailable; validating checked-in fixtures locally only.", file=sys.stderr)
    for task in selected:
        print(f"Validating fixture {task.id}...", flush=True)
        fixture_checks.append(await validate_fixture(task, fixture_executor))
    if args.validate_only:
        validation = {"validated": len(fixture_checks), "fixtures": fixture_checks,
                      "executor": "sandbox" if fixture_executor is not None else "local_checked_in_fixtures"}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Validation report: {args.output}")
        else:
            print(json.dumps(validation, ensure_ascii=False, indent=2))
        return 0
    if old_report is not None:
        regraded = await regrade_report(old_report, selected, services.sandbox, args.regrade, fixture_checks)
        output = args.output or args.regrade.with_name(args.regrade.stem + "-regraded.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(regraded, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{regraded['summary']['passes']}/{regraded['summary']['runs']} passed; report: {output}")
        return 0 if regraded["summary"]["passes"] == regraded["summary"]["runs"] else 1
    model_status = services.model_settings.status()
    if resume_report is not None:
        current_fixtures = {item["task"]: item for item in fixture_checks}
        for item in resume_report["results"]:
            fixture = current_fixtures[item["task"]]
            if (item.get("fixture_sha256") != fixture["fixture_sha256"]
                    or item.get("base_commit") != fixture["base_commit"]):
                raise ValueError(f"{item['task']}: fixture changed; regrade before resuming")
        if (resume_report.get("model") != model_status["model"]
                or resume_report.get("effective_context_window") != model_status.get("effective_context_window")
                or resume_report.get("sandbox_backend") != capabilities.backend
                or resume_report.get("permission_mode") != "request_approval"
                or resume_report.get("max_seconds_per_run") != args.timeout):
            raise ValueError("Model, context window, sandbox, permission, or timeout changed since saved report")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output = args.output or (args.resume.with_name(args.resume.stem + "-continued.json") if args.resume
                             else ROOT / "evals" / "results" / f"code-tasks-{stamp}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    artifacts_dir = output.with_suffix("")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    results = list(resume_report["results"]) if resume_report is not None else []
    report: dict[str, Any] | None = None
    for task in selected:
        existing = {item["repeat"] for item in results if item["task"] == task.id}
        for repeat in (number for number in range(1, args.repeats + 1) if number not in existing):
            print(f"Running {task.id} ({repeat}/{args.repeats})...", flush=True)
            result = await run_one(task, repeat, timeout=args.timeout, artifacts_dir=artifacts_dir)
            results.append(result)
            print(f"  {'PASS' if result['passed'] else 'FAIL'}; {result['elapsed_seconds']}s", flush=True)
            report = {"schema_version": 2,
                      "created_at": datetime.now(timezone.utc).isoformat(),
                      "model": model_status["model"],
                      "effective_context_window": model_status.get("effective_context_window"),
                      "sandbox_backend": capabilities.backend,
                      "permission_mode": "request_approval",
                      "max_seconds_per_run": args.timeout,
                      "source_commit": str(git(ROOT, "rev-parse", "HEAD")).strip(),
                      "source_dirty": bool(str(git(ROOT, "status", "--porcelain")).strip()),
                      "suite": resume_report.get("suite", args.suite) if resume_report else args.suite,
                      "repeats": args.repeats, "fixtures": fixture_checks,
                      "resumed_from": str(args.resume) if args.resume else None,
                      "summary": summarize_results(results), "results": results}
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if report is None:
        report = dict(resume_report or {})
        report.update({"repeats": args.repeats, "summary": summarize_results(results), "results": results})
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{report['summary']['passes']}/{report['summary']['runs']} passed; report: {output}")
    return 0 if report["summary"]["passes"] == report["summary"]["runs"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
