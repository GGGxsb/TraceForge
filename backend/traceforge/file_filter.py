from __future__ import annotations

from pathlib import Path


SKIP_PARTS = {
    ".git",
    ".traceforge",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".vite",
}


def is_ignored_path(relative: Path) -> bool:
    return any(part in SKIP_PARTS or part.startswith(".traceforge-data") for part in relative.parts)
