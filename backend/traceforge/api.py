from __future__ import annotations

import asyncio
import ipaddress
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field, SecretStr

from .agent import AgentRunner, ApprovalBroker
from .checkpoints import CheckpointError, CheckpointStore
from .context import BranchService
from .folder_picker import pick_directory
from .hooks import HookContext, HookPoint, HookRegistry
from .models import PermissionMode, RunEvent, RunStatus, new_id
from .model_settings import ModelSettingsStore
from .skills import SkillCatalog
from .storage import EventHub, SessionStore, WorkspaceStore
from .workspaces import WorkspaceInspector
from .worktrees import WorktreeManager, WorktreeError


class WorkspaceCreate(BaseModel):
    path: str


class SessionCreate(BaseModel):
    workspace_id: str


class SessionArchiveRequest(BaseModel):
    archived: bool


class PermissionModeRequest(BaseModel):
    mode: PermissionMode


class RunCreate(BaseModel):
    content: str = Field(min_length=1)


class ApprovalDecisionRequest(BaseModel):
    session_id: str
    decision: str
    fingerprint: str


class ClarificationAnswerRequest(BaseModel):
    answer: str = Field(min_length=1)
    question_id: str | None = None
    session_id: str | None = None


class BranchRequest(BaseModel):
    target_entry_id: str
    include_summary: bool = True
    mode: Literal["fork", "resume"] = "fork"
    expected_current: str | None = None


class RollbackRequest(BaseModel):
    target_entry_id: str
    expected_current: str | None = None


class WorkspaceAdoptRequest(BaseModel):
    expected_current: str


class ModelSettingsUpdate(BaseModel):
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    model: str = Field(min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=2048)
    brief_model: str | None = Field(default=None, max_length=200)
    fallback_model: str | None = Field(default=None, max_length=200)


class ApiServices:
    def __init__(
        self,
        *,
        workspaces: WorkspaceStore,
        inspector: WorkspaceInspector,
        sessions: SessionStore,
        events: EventHub,
        runner: AgentRunner,
        approvals: ApprovalBroker,
        branches: BranchService,
        hooks: HookRegistry,
        artifacts_root: Path,
        sandbox,
        model_settings: ModelSettingsStore,
        skills: SkillCatalog | None = None,
        checkpoints: CheckpointStore | None = None,
        worktrees: WorktreeManager | None = None,
    ) -> None:
        self.workspaces = workspaces
        self.inspector = inspector
        self.sessions = sessions
        self.events = events
        self.runner = runner
        self.approvals = approvals
        self.branches = branches
        self.hooks = hooks
        self.artifacts_root = artifacts_root
        self.sandbox = sandbox
        self.model_settings = model_settings
        self.skills = skills
        self.checkpoints = checkpoints
        self.worktrees = worktrees
        self.model_configuration_lock = asyncio.Lock()
        self.folder_picker_lock = asyncio.Lock()
        self.session_transition_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.workspace_transition_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def build_router(services: ApiServices) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    async def health() -> dict[str, Any]:
        sandbox = await services.sandbox.inspect()
        model_configured = services.model_settings.status()["configured"]
        return {
            "ok": model_configured and sandbox.ready,
            "model_configured": model_configured,
            "sandbox": sandbox.model_dump(mode="json"),
        }

    @router.get("/tools")
    async def available_tools():
        plugins = getattr(services.runner.tools, "plugins", None)
        registered = {item["name"] for item in plugins.definitions()} if plugins else set()
        return [{"name": item["name"], "description": item["description"],
                 "source": "plugin" if item["name"] in registered else "core",
                 "requires_approval": item["name"] in registered}
                for item in services.runner.tools.definitions()]

    @router.get("/model-settings")
    async def get_model_settings():
        return services.model_settings.status()

    def require_local_ui(request: Request, ui_request: str | None) -> None:
        if ui_request != "1":
            raise HTTPException(status_code=403, detail="Model settings are only available from the local UI")
        try:
            is_loopback = ipaddress.ip_address(request.client.host if request.client else "").is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise HTTPException(status_code=403, detail="Model settings require a local connection")

    @router.put("/model-settings")
    async def update_model_settings(
        update: ModelSettingsUpdate,
        request: Request,
        ui_request: str | None = Header(default=None, alias="X-TraceForge-UI"),
    ):
        require_local_ui(request, ui_request)
        async with services.model_configuration_lock:
            if any(run.task and not run.task.done() for run in services.runner.runs.values()):
                raise HTTPException(status_code=409, detail="Agent 正在运行，请结束后再修改模型配置")
            try:
                return services.model_settings.save(
                    api_key=update.api_key.get_secret_value() if update.api_key else None,
                    model=update.model,
                    base_url=update.base_url,
                    brief_model=update.brief_model,
                    fallback_model=update.fallback_model,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"保存模型配置失败：{type(exc).__name__}") from exc

    @router.delete("/model-settings")
    async def reset_model_settings(
        request: Request,
        ui_request: str | None = Header(default=None, alias="X-TraceForge-UI"),
    ):
        require_local_ui(request, ui_request)
        async with services.model_configuration_lock:
            if any(run.task and not run.task.done() for run in services.runner.runs.values()):
                raise HTTPException(status_code=409, detail="Agent 正在运行，请结束后再修改模型配置")
            try:
                return services.model_settings.reset()
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"重置模型配置失败：{type(exc).__name__}") from exc

    @router.get("/workspaces")
    async def list_workspaces():
        return [
            {**item.model_dump(mode="json"), "available": Path(item.path).is_dir()}
            for item in services.workspaces.list()
        ]

    @router.post("/workspaces")
    async def create_workspace(request: WorkspaceCreate):
        try:
            workspace = services.workspaces.register(request.path)
            services.inspector.capture_directory_baseline(workspace.id)
            return workspace.model_dump(mode="json")
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/workspaces/pick-directory")
    async def pick_workspace_directory(ui_request: str | None = Header(default=None, alias="X-TraceForge-UI")):
        if ui_request != "1":
            raise HTTPException(status_code=403, detail="Folder picker is only available from the local UI")
        if services.folder_picker_lock.locked():
            raise HTTPException(status_code=409, detail="A folder picker is already open")
        async with services.folder_picker_lock:
            try:
                path = await pick_directory()
                if not path:
                    return {"cancelled": True, "workspace": None}
                workspace = services.workspaces.register(path)
                services.inspector.capture_directory_baseline(workspace.id)
                return {"cancelled": False, "workspace": workspace.model_dump(mode="json")}
            except TimeoutError as exc:
                raise HTTPException(status_code=408, detail="Folder picker timed out") from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/workspaces/{workspace_id}/relocate")
    async def relocate_workspace(workspace_id: str, ui_request: str | None = Header(default=None, alias="X-TraceForge-UI")):
        if ui_request != "1":
            raise HTTPException(status_code=403, detail="Folder picker is only available from the local UI")
        try:
            services.workspaces.get(workspace_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if services.folder_picker_lock.locked():
            raise HTTPException(status_code=409, detail="A folder picker is already open")
        async with services.folder_picker_lock:
            try:
                path = await pick_directory()
            except TimeoutError as exc:
                raise HTTPException(status_code=408, detail="Folder picker timed out") from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not path:
            return {"cancelled": True, "workspace": None}
        async with services.workspace_transition_locks[workspace_id]:
            record = services.workspaces.get(workspace_id)
            target = Path(path).expanduser().resolve()
            if target != Path(record.path):
                project_sessions = [services.sessions.get(item["id"]) for item in services.sessions.list()
                                    if item["workspace_id"] == workspace_id]
                if any(run.task and not run.task.done() and
                       services.sessions.get(run.session_id).header.workspace_id == workspace_id
                       for run in services.runner.runs.values()):
                    raise HTTPException(status_code=409, detail="请先停止此项目正在运行的任务")
                if any(entry.type == "workspace_binding" for session in project_sessions for entry in session.entries):
                    raise HTTPException(status_code=409, detail="此项目已有独立 Git worktree 分支，暂不能重定位")
                if project_sessions and Path(record.path).exists():
                    raise HTTPException(status_code=409, detail="请先将旧项目目录移走，再重新定位已有会话的项目")
            try:
                updated = services.workspaces.relocate(workspace_id, path)
                if updated.kind == "directory":
                    services.inspector.capture_directory_baseline(updated.id)
                return {"cancelled": False, "workspace": updated.model_dump(mode="json")}
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/workspaces/{workspace_id}/files")
    async def workspace_files(workspace_id: str, path: str = "."):
        try:
            return services.inspector.files(workspace_id, path)
        except (KeyError, ValueError, PermissionError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/workspaces/{workspace_id}/skills")
    async def workspace_skills(workspace_id: str):
        try:
            workspace = services.workspaces.get(workspace_id)
            if services.skills is None:
                return {"skills": [], "warnings": []}
            skills, warnings = services.skills.discover(workspace.path)
            return {"skills": [skill.public() for skill in skills], "warnings": warnings}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/workspaces/{workspace_id}/file")
    async def workspace_file(workspace_id: str, path: str = Query(...)):
        try:
            return PlainTextResponse(services.inspector.read_file(workspace_id, path))
        except (KeyError, ValueError, PermissionError, OSError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/workspaces/{workspace_id}/diff")
    async def workspace_diff(workspace_id: str):
        try:
            return PlainTextResponse(await services.inspector.diff(workspace_id))
        except (KeyError, ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/sessions")
    async def list_sessions():
        return services.sessions.list()

    @router.post("/sessions")
    async def create_session(request: SessionCreate):
        try:
            workspace = services.workspaces.get(request.workspace_id)
            if not Path(workspace.path).is_dir():
                raise HTTPException(status_code=409, detail="项目目录不可用，请先重新定位")
            session = services.sessions.create(workspace)
            return {
                "id": session.header.id,
                "workspace_id": workspace.id,
                "workspace": workspace.path,
                "created_at": session.header.created_at,
                "active_leaf_id": None,
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str):
        try:
            session = services.sessions.get(session_id)
            return {
                "header": session.header.model_dump(mode="json"),
                "entries": [entry.model_dump(mode="json") for entry in session.entries],
                "active_branch": [entry.model_dump(mode="json") for entry in session.get_branch()],
                "active_leaf_id": session.active_leaf_id,
                "active_workspace": session.workspace,
                "archived": session.archived,
                "permission_mode": session.permission_mode.value,
                "recovery_issues": session.recovery_issues,
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/files")
    async def session_files(session_id: str, path: str = "."):
        try:
            session = services.sessions.get(session_id)
            return services.inspector.files(session.header.workspace_id, path, path_override=session.workspace)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, PermissionError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/file")
    async def session_file(session_id: str, path: str = Query(...)):
        try:
            session = services.sessions.get(session_id)
            return PlainTextResponse(services.inspector.read_file(
                session.header.workspace_id, path, path_override=session.workspace,
            ))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, PermissionError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/diff")
    async def session_diff(session_id: str):
        try:
            session = services.sessions.get(session_id)
            return PlainTextResponse(await services.inspector.diff(
                session.header.workspace_id, path_override=session.workspace,
            ))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.patch("/sessions/{session_id}/archive")
    async def archive_session(session_id: str, request: SessionArchiveRequest):
        try:
            async with services.session_transition_locks[session_id]:
                session = services.sessions.get(session_id)
                if services.runner.active_for_session(session_id):
                    raise HTTPException(status_code=409, detail="运行中的会话不能归档或恢复")
                if session.archived != request.archived:
                    entry = await session.append("session_archive", {"archived": request.archived})
                    await services.events.publish(RunEvent(
                        session_id=session_id,
                        seq=entry.seq,
                        type=entry.type,
                        timestamp=entry.timestamp,
                        payload={"entry_id": entry.id, **entry.payload},
                    ))
                return {"id": session_id, "archived": session.archived}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.patch("/sessions/{session_id}/permission-mode")
    async def set_permission_mode(
        session_id: str, request: PermissionModeRequest,
        ui_request: str | None = Header(default=None, alias="X-TraceForge-UI"),
    ):
        if ui_request != "1":
            raise HTTPException(status_code=403, detail="Permission mode can only be changed from the local UI")
        try:
            async with services.session_transition_locks[session_id]:
                session = services.sessions.get(session_id)
                if services.runner.active_for_session(session_id):
                    raise HTTPException(status_code=409, detail="运行中不能切换权限模式，请先停止当前任务")
                if session.archived:
                    raise HTTPException(status_code=409, detail="请先恢复已归档会话")
                if session.permission_mode != request.mode:
                    entry = await session.append("permission_mode", {
                        "mode": request.mode.value,
                        "previous_mode": session.permission_mode.value,
                    })
                    await services.events.publish(RunEvent(
                        session_id=session_id, seq=entry.seq, type=entry.type,
                        timestamp=entry.timestamp, payload={"entry_id": entry.id, **entry.payload},
                    ))
                return {"id": session_id, "mode": session.permission_mode.value}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/sessions/{session_id}")
    async def delete_session(session_id: str):
        try:
            async with services.session_transition_locks[session_id]:
                services.sessions.get(session_id)
                if services.runner.active_for_session(session_id):
                    raise HTTPException(status_code=409, detail="运行中的会话不能删除")
                preserved_branches = services.worktrees.delete_session(
                    services.sessions.get(session_id), services.checkpoints,
                ) if services.worktrees else []
                services.sessions.delete(session_id, services.artifacts_root)
                if services.checkpoints:
                    services.checkpoints.delete_session(session_id)
                return {"id": session_id, "deleted": True,
                        "preserved_branches": preserved_branches}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/tree")
    async def session_tree(session_id: str, granularity: Literal["turn", "entry"] = "turn"):
        try:
            session = services.sessions.get(session_id)
            return session.get_turn_tree() if granularity == "turn" else session.get_tree()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/active-run")
    async def active_session_run(session_id: str):
        try:
            services.sessions.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        run = services.runner.active_for_session(session_id)
        return {"run_id": run.id, "status": run.status.value} if run else None

    @router.post("/sessions/{session_id}/runs")
    async def start_run(session_id: str, request: RunCreate):
        try:
            async with services.model_configuration_lock:
                session = services.sessions.get(session_id)
                async with services.workspace_transition_locks[session.header.workspace_id]:
                    async with services.session_transition_locks[session_id]:
                        if session.archived:
                            raise HTTPException(status_code=409, detail="会话已归档，请先恢复")
                        if _workspace_has_active_run(session):
                            raise HTTPException(status_code=409, detail="此工作区已有运行中的 Agent，请等待其结束")
                        _assert_workspace_aligned(session)
                        run = services.runner.start(session_id, request.content)
                        return {"run_id": run.id, "status": run.status.value}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CheckpointError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str):
        try:
            await services.runner.cancel(run_id)
            return {"run_id": run_id, "status": "cancelled"}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/approvals/{approval_id}/decision")
    async def approval_decision(approval_id: str, request: ApprovalDecisionRequest):
        if request.decision not in {"allow_once", "allow_session", "deny"}:
            raise HTTPException(status_code=400, detail="Invalid approval decision")
        try:
            services.approvals.decide(
                request.session_id,
                approval_id,
                request.decision,
                request.fingerprint,
            )
            return {"approval_id": approval_id, "decision": request.decision}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @router.post("/sessions/{session_id}/clarifications")
    async def clarification_answer(session_id: str, request: ClarificationAnswerRequest):
        return await _answer_clarification(session_id, request.question_id, request.answer)

    @router.post("/clarifications/{question_id}/answer")
    async def clarification_answer_by_id(question_id: str, request: ClarificationAnswerRequest):
        if not request.session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        return await _answer_clarification(request.session_id, question_id, request.answer)

    async def _answer_clarification(session_id: str, question_id: str | None, answer: str):
        async with services.model_configuration_lock:
            try:
                session = services.sessions.get(session_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            async with services.workspace_transition_locks[session.header.workspace_id]:
                return await _answer_clarification_locked(session_id, question_id, answer)

    async def _answer_clarification_locked(session_id: str, question_id: str | None, answer: str):
        try:
            async with services.session_transition_locks[session_id]:
                session = services.sessions.get(session_id)
                if session.archived:
                    raise RuntimeError("会话已归档，请先恢复")
                if services.runner.active_for_session(session_id):
                    raise RuntimeError("This session already has an active run")
                if _workspace_has_active_run(session):
                    raise RuntimeError("此工作区已有运行中的 Agent，请等待其结束")
                _assert_workspace_aligned(session)
                branch = session.get_branch()
                answered_ids = {
                    item.payload.get("question_id") for item in branch
                    if item.type == "clarification_answer"
                }
                pending = [
                    item for item in branch
                    if item.type == "clarification_question"
                    and item.payload.get("question_id") not in answered_ids
                ]
                question = next(
                    (item for item in pending if item.payload.get("question_id") == question_id),
                    pending[0] if question_id is None and pending else None,
                )
                if question is None:
                    raise ValueError("Clarification question does not belong to the active branch")
                question_id = question.payload.get("question_id")
                answer = answer.strip()
                if not answer:
                    raise ValueError("Clarification answer cannot be empty")
                entry = await session.append(
                    "clarification_answer",
                    {
                        "question_id": question_id,
                        "question": question.payload.get("question", ""),
                        "answer": answer,
                        "status": "answered",
                    },
                )
                await services.events.publish(
                    RunEvent(
                        session_id=session_id,
                        seq=entry.seq,
                        type=entry.type,
                        timestamp=entry.timestamp,
                        payload={"entry_id": entry.id, **entry.payload},
                    )
                )
                remaining = len(pending) - 1
                if remaining:
                    return {"entry_id": entry.id, "run_id": None, "remaining": remaining}
                source_id = question.payload.get("source_message_id")
                source = session.by_id.get(source_id) if source_id else None
                if source is None or source.type != "user_message":
                    source = next((item for item in reversed(branch) if item.type == "user_message"), None)
                base_request = str(source.payload.get("content", "")) if source else ""
                source_seq = source.seq if source else 0
                answers = [
                    item for item in [*branch, entry]
                    if item.type == "clarification_answer" and item.seq > source_seq
                ]
                brief_text = (
                    f"原始需求：\n{base_request}\n\n用户已经提供的澄清：\n"
                    + "\n".join(
                        f"- {item.payload.get('question', '澄清问题')}：{item.payload.get('answer', '')}"
                        for item in answers
                    )
                )
                run = services.runner.start(
                    session_id,
                    answer,
                    brief_text=brief_text,
                    resume_from_entry_id=entry.id,
                    request_source_id=source.id if source else None,
                )
                return {"entry_id": entry.id, "run_id": run.id, "remaining": 0}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/sessions/{session_id}/branch")
    async def switch_branch(session_id: str, request: BranchRequest):
        async with services.model_configuration_lock:
            try:
                session = services.sessions.get(session_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            async with services.workspace_transition_locks[session.header.workspace_id]:
                return await _switch_branch_locked(session_id, request)

    def _workspace_has_active_run(session) -> bool:
        return any(
            services.sessions.get(run.session_id).header.workspace_id == session.header.workspace_id
            and Path(services.sessions.get(run.session_id).workspace).resolve() == Path(session.workspace).resolve()
            for run in services.runner.runs.values()
            if run.task and not run.task.done()
        )

    def _assert_workspace_aligned(session) -> None:
        if services.checkpoints and services.checkpoints.status(session)["state"] == "diverged":
            raise CheckpointError("当前文件与此会话的代码检查点不同。请先恢复此会话代码，或将当前文件记为新检查点。")

    @router.get("/sessions/{session_id}/workspace-status")
    async def workspace_status(session_id: str):
        try:
            session = services.sessions.get(session_id)
            if not services.checkpoints:
                raise CheckpointError("代码检查点不可用")
            return services.checkpoints.status(session)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (CheckpointError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/sessions/{session_id}/adopt-workspace")
    async def adopt_workspace(session_id: str, request: WorkspaceAdoptRequest):
        try:
            session = services.sessions.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        async with services.workspace_transition_locks[session.header.workspace_id]:
            async with services.session_transition_locks[session_id]:
                try:
                    if session.archived or _workspace_has_active_run(session):
                        raise CheckpointError("会话已归档或工作区正在运行，暂不能更新代码检查点")
                    if not services.checkpoints:
                        raise CheckpointError("代码检查点不可用")
                    if services.checkpoints.live_fingerprint(session) != request.expected_current:
                        raise CheckpointError("工作区已发生变化，请刷新后重试")
                    snapshot = services.checkpoints.capture(session)
                    entry = await session.append("workspace_checkpoint", {
                        **snapshot, "reason": "adopt_workspace",
                    })
                    await services.events.publish(RunEvent(
                        session_id=session_id, seq=entry.seq, type=entry.type,
                        timestamp=entry.timestamp, payload={"entry_id": entry.id, **entry.payload},
                    ))
                    return entry.model_dump(mode="json")
                except (CheckpointError, OSError) as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/checkpoints")
    async def list_checkpoints(session_id: str):
        try:
            session = services.sessions.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [
            {"entry_id": entry.id, "seq": entry.seq, "timestamp": entry.timestamp, **entry.payload}
            for entry in session.entries if entry.type == "workspace_checkpoint"
        ]

    @router.get("/sessions/{session_id}/branch-preview")
    async def branch_preview(session_id: str, target_entry_id: str,
                             operation: Literal["branch", "rollback"] = "branch"):
        try:
            session = services.sessions.get(session_id)
            if not services.checkpoints:
                raise CheckpointError("代码检查点不可用")
            return services.checkpoints.preview(
                session, target_entry_id,
                allow_cross_head=(operation == "branch" and bool(
                    services.worktrees and services.worktrees.enabled(session)
                )),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (CheckpointError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def _switch_branch_locked(session_id: str, request: BranchRequest):
        run_id = new_id("branch")
        try:
            async with services.session_transition_locks[session_id]:
                session = services.sessions.get(session_id)
                if session.archived:
                    raise HTTPException(status_code=409, detail="会话已归档，请先恢复")
                if _workspace_has_active_run(session):
                    raise HTTPException(status_code=409, detail="此工作区有运行中的 Agent，暂不能切换分支")
                if not services.checkpoints:
                    raise CheckpointError("代码检查点不可用")
                _assert_workspace_aligned(session)
                git_isolated = bool(services.worktrees and services.worktrees.enabled(session))
                preview = services.checkpoints.preview(
                    session, request.target_entry_id, allow_cross_head=git_isolated,
                )
                if not preview["available"]:
                    raise CheckpointError(str(preview["reason"]))
                if request.expected_current and request.expected_current != preview["current_fingerprint"]:
                    raise CheckpointError("工作区在预览后发生变化，请重新查看差异。")
                before = await services.hooks.dispatch(
                    HookContext(
                        session_id=session_id,
                        run_id=run_id,
                        point=HookPoint.BEFORE_BRANCH_SWITCH,
                        status=RunStatus.IDLE,
                        active_leaf_id=session.active_leaf_id,
                    )
                )
                if before.action == "abort":
                    raise RuntimeError("A hook aborted branch switching")
                source_workspace = session.workspace
                use_new_worktree = git_isolated and request.mode == "fork"
                target_workspace = (
                    services.worktrees.allocate(session) if use_new_worktree and services.worktrees
                    else session.workspace_for(request.target_entry_id) if git_isolated
                    else source_workspace
                )
                if git_isolated and not use_new_worktree:
                    target_preview = services.checkpoints.preview(
                        session, request.target_entry_id, workspace_override=target_workspace,
                    )
                    if not target_preview["available"] or target_preview["changes"]:
                        raise CheckpointError("目标分支的独立工作区已变化，请先处理该分支代码差异。")
                leaving = services.checkpoints.capture(session)
                leaving_entry = await session.append("workspace_checkpoint", {
                    **leaving, "reason": "before_branch_switch",
                }, run_id=run_id)
                transaction_id = new_id("restore")
                await session.append("workspace_restore_begin", {
                    "transaction_id": transaction_id, "from_checkpoint_id": leaving["checkpoint_id"],
                    "to_checkpoint_id": preview["checkpoint_id"], "target_entry_id": request.target_entry_id,
                    "reason": "branch_switch", "from_workspace": source_workspace,
                    "to_workspace": target_workspace, "isolated": git_isolated,
                    "created_worktree": use_new_worktree,
                }, run_id=run_id)
                try:
                    if use_new_worktree and services.worktrees:
                        target_manifest = services.checkpoints.manifest(session, str(preview["checkpoint_id"]))
                        services.worktrees.create(session, str(target_manifest["git"]["head"]), path=target_workspace)
                        changes = services.checkpoints.restore(
                            session, str(preview["checkpoint_id"]), workspace_override=target_workspace,
                        )
                    elif git_isolated and target_workspace != source_workspace:
                        changes = preview["changes"]
                    else:
                        changes = services.checkpoints.restore(
                            session, str(preview["checkpoint_id"]),
                            expected_current=str(preview["current_fingerprint"]),
                        )
                    branch_entry = await services.branches.switch(
                        session, request.target_entry_id, run_id,
                        include_summary=request.include_summary, mode=request.mode,
                    )
                    if use_new_worktree:
                        await session.append("workspace_binding", {
                            "path": target_workspace, "source_workspace": source_workspace,
                            "checkpoint_id": preview["checkpoint_id"],
                        }, run_id=run_id)
                    restore_entry = await session.append("workspace_restore", {
                        "checkpoint_id": preview["checkpoint_id"], "target_entry_id": request.target_entry_id,
                        "changed_files": changes, "reason": "branch_switch", "transaction_id": transaction_id,
                        "workspace": target_workspace,
                    }, run_id=run_id)
                except Exception:
                    services.checkpoints.restore(
                        session, str(leaving["checkpoint_id"]), workspace_override=source_workspace,
                    )
                    if leaving_entry.id not in {item.id for item in session.get_branch()}:
                        await services.branches.switch(session, leaving_entry.id, run_id,
                                                       include_summary=False, mode="resume")
                    if use_new_worktree and services.worktrees:
                        services.worktrees.remove(session, target_workspace, force=True)
                    await session.append("workspace_restore", {
                        "checkpoint_id": leaving["checkpoint_id"], "target_entry_id": request.target_entry_id,
                        "changed_files": [], "reason": "branch_switch_failed", "transaction_id": transaction_id,
                        "workspace": source_workspace,
                    }, run_id=run_id)
                    raise
                branch_events = [
                    item
                    for item in session.entries
                    if item.run_id == run_id and item.type in {"branch_switch", "branch_summary", "branch_resume", "workspace_checkpoint", "workspace_binding", "workspace_restore"}
                ]
                for branch_event in branch_events:
                    await services.events.publish(
                        RunEvent(
                            session_id=session_id,
                            run_id=run_id,
                            seq=branch_event.seq,
                            type=branch_event.type,
                            timestamp=branch_event.timestamp,
                            payload={"entry_id": branch_event.id, **branch_event.payload},
                        )
                    )
                await services.hooks.dispatch(
                    HookContext(
                        session_id=session_id,
                        run_id=run_id,
                        point=HookPoint.AFTER_BRANCH_SWITCH,
                        status=RunStatus.IDLE,
                        active_leaf_id=restore_entry.id,
                    )
                )
                return branch_entry.model_dump(mode="json")
        except (KeyError, RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/sessions/{session_id}/rollback")
    async def rollback_code(session_id: str, request: RollbackRequest):
        try:
            session = services.sessions.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        async with services.workspace_transition_locks[session.header.workspace_id]:
            async with services.session_transition_locks[session_id]:
                try:
                    if session.archived or _workspace_has_active_run(session):
                        raise CheckpointError("会话已归档或工作区正在运行，暂不能回滚")
                    if not services.checkpoints:
                        raise CheckpointError("代码检查点不可用")
                    preview = services.checkpoints.preview(session, request.target_entry_id)
                    if not preview["available"]:
                        raise CheckpointError(str(preview["reason"]))
                    if request.expected_current and request.expected_current != preview["current_fingerprint"]:
                        raise CheckpointError("工作区在预览后发生变化，请重新查看差异。")
                    saved = services.checkpoints.capture(session)
                    await session.append("workspace_checkpoint", {**saved, "reason": "before_rollback"})
                    transaction_id = new_id("restore")
                    await session.append("workspace_restore_begin", {
                        "transaction_id": transaction_id, "from_checkpoint_id": saved["checkpoint_id"],
                        "to_checkpoint_id": preview["checkpoint_id"], "target_entry_id": request.target_entry_id,
                        "reason": "manual_rollback",
                    })
                    try:
                        changes = services.checkpoints.restore(
                            session, str(preview["checkpoint_id"]),
                            expected_current=str(preview["current_fingerprint"]),
                        )
                        entry = await session.append("workspace_restore", {
                            "checkpoint_id": preview["checkpoint_id"],
                            "target_entry_id": request.target_entry_id,
                            "changed_files": changes, "reason": "manual_rollback",
                            "transaction_id": transaction_id,
                        })
                    except Exception:
                        services.checkpoints.restore(session, str(saved["checkpoint_id"]))
                        await session.append("workspace_restore", {
                            "checkpoint_id": saved["checkpoint_id"], "target_entry_id": request.target_entry_id,
                            "changed_files": [], "reason": "manual_rollback_failed", "transaction_id": transaction_id,
                        })
                        raise
                    await services.events.publish(RunEvent(
                        session_id=session_id, seq=entry.seq, type=entry.type,
                        timestamp=entry.timestamp, payload={"entry_id": entry.id, **entry.payload},
                    ))
                    return entry.model_dump(mode="json")
                except (CheckpointError, OSError, ValueError) as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/artifacts/{artifact_id}")
    async def artifact(artifact_id: str):
        if not re.fullmatch(r"artifact_[0-9a-f]{32}", artifact_id):
            raise HTTPException(status_code=400, detail="Invalid artifact id")
        matches = [
            path
            for path in services.artifacts_root.glob(f"*/{artifact_id}.*")
            if path.is_file() and not path.name.endswith(".part")
        ]
        if not matches:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return FileResponse(matches[0])

    @router.websocket("/sessions/{session_id}/events")
    async def session_events(websocket: WebSocket, session_id: str, after_seq: int = 0):
        try:
            session = services.sessions.get(session_id)
        except KeyError:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        queue = services.events.subscribe(session_id)
        replay = session.replay_events(after_seq)
        last_persisted_seq = after_seq
        for event in replay:
            await websocket.send_json(event.model_dump(mode="json"))
            last_persisted_seq = max(last_persisted_seq, event.seq)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25)
                    if event.type not in {"assistant_delta", "assistant_stream_reset"}:
                        if event.seq <= last_persisted_seq:
                            continue
                        last_persisted_seq = event.seq
                    await websocket.send_json(event.model_dump(mode="json"))
                except TimeoutError:
                    await websocket.send_json(
                        RunEvent(
                            session_id=session_id,
                            seq=last_persisted_seq,
                            type="ping",
                            payload={},
                        ).model_dump(mode="json")
                    )
        except WebSocketDisconnect:
            pass
        finally:
            services.events.unsubscribe(session_id, queue)

    return router
