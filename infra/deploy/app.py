"""Container entry point; keep host-mode API and configuration unchanged."""

import os
from pathlib import Path

from traceforge.main import app


initial_path = os.getenv("TRACEFORGE_INITIAL_WORKSPACE", "/projects/default")
if initial_path:
    project = Path(initial_path).expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    services = app.state.services
    workspace = services.workspaces.register(str(project))
    services.inspector.capture_directory_baseline(workspace.id)
