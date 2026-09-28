from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from .agent import AgentRunner, ApprovalBroker
from .api import ApiServices, build_router
from .artifacts import ArtifactStore
from .config import Settings
from .context import BranchService, CompactionService, ContextProjector
from .checkpoints import CheckpointStore
from .hooks import HookRegistry, load_configured_hook
from .skills import SkillCatalog
from .model_settings import ModelSettingsStore
from .sandbox import create_platform_sandbox
from .security import PolicyEngine
from .storage import EventHub, SessionStore, WorkspaceStore
from .task_brief import TaskBriefHook
from .tools import ToolService
from .tools import TOOL_DEFINITIONS
from .tool_plugins import ToolPluginRegistry, load_tool_plugin
from .workspaces import WorkspaceInspector
from .worktrees import WorktreeManager


def create_app(settings: Settings | None = None) -> FastAPI:
    load_dotenv()
    settings = settings or Settings.from_env()
    settings.ensure_directories()
    model_settings = ModelSettingsStore(
        settings.model_config_dir / "model-settings.json",
        env_api_key=settings.openai_api_key,
        env_model=settings.openai_model,
        env_base_url=settings.openai_base_url,
        env_brief_model=settings.brief_model,
        env_fallback_model=settings.fallback_model,
    )
    adapter = model_settings.adapter

    workspaces = WorkspaceStore(settings.data_dir / "workspaces")
    inspector = WorkspaceInspector(workspaces)
    sessions = SessionStore(settings.data_dir / "sessions", workspaces)
    events = EventHub()
    artifacts = ArtifactStore(settings.data_dir / "artifacts")
    checkpoints = CheckpointStore(settings.data_dir / "checkpoints")
    worktrees = WorktreeManager(settings.data_dir / "worktrees")
    skills = SkillCatalog(settings.model_config_dir)
    tool_plugins = ToolPluginRegistry({item["name"] for item in TOOL_DEFINITIONS})
    for plugin_spec in settings.tool_plugins:
        load_tool_plugin(plugin_spec, tool_plugins)
    sandbox = create_platform_sandbox(settings.docker_image)
    tools = ToolService(
        sandbox=sandbox,
        artifacts=artifacts,
        tool_worker_path=Path(__file__).resolve().parents[2] / "infra" / "runner" / "tool_worker.py",
        max_command_timeout=settings.command_timeout,
        skills=skills,
        plugins=tool_plugins,
    )
    hooks = HookRegistry()
    hooks.register(TaskBriefHook(adapter))
    for hook_spec in settings.hook_plugins:
        hooks.register(load_configured_hook(hook_spec))
    approvals = ApprovalBroker()
    projector = ContextProjector(settings.context_window, settings.reserve_tokens)
    compactor = CompactionService(
        adapter,
        settings.context_window,
        settings.reserve_tokens,
        settings.keep_recent_tokens,
    )
    runner = AgentRunner(
        sessions=sessions,
        events=events,
        hooks=hooks,
        adapter=adapter,
        projector=projector,
        compactor=compactor,
        tools=tools,
        policy=PolicyEngine(tool_plugins),
        approvals=approvals,
        max_rounds=settings.max_agent_rounds,
        max_run_tokens=settings.max_run_tokens,
        max_run_cost_usd=settings.max_run_cost_usd,
        input_price_per_million=settings.input_price_per_million,
        output_price_per_million=settings.output_price_per_million,
        approval_timeout=settings.approval_timeout,
        protected_paths=(model_settings.path,),
        skills=skills,
        checkpoints=checkpoints,
    )
    services = ApiServices(
        workspaces=workspaces,
        inspector=inspector,
        sessions=sessions,
        events=events,
        runner=runner,
        approvals=approvals,
        branches=BranchService(adapter),
        hooks=hooks,
        artifacts_root=settings.data_dir / "artifacts",
        sandbox=sandbox,
        model_settings=model_settings,
        skills=skills,
        checkpoints=checkpoints,
        worktrees=worktrees,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await sessions.recover_all(checkpoints, worktrees)
        for session_info in sessions.list():
            approvals.restore_session_grants(sessions.get(session_info["id"]))
        yield

    app = FastAPI(title="TraceForge", version="0.1.0", lifespan=lifespan)
    app.state.services = services

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        # FastAPI normally includes the submitted `input` in 422 responses.
        # Model settings contain API keys, so never echo request values.
        detail = [
            {"type": issue.get("type"), "loc": issue.get("loc"), "msg": issue.get("msg")}
            for issue in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(build_router(services))

    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.exists():
        assets = frontend_dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def frontend(path: str):
            if path == "api" or path.startswith("api/"):
                raise HTTPException(status_code=404, detail="API route not found")
            candidate = (frontend_dist / path).resolve()
            if path and candidate.is_file() and frontend_dist.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(frontend_dist / "index.html")

    return app


app = create_app()
