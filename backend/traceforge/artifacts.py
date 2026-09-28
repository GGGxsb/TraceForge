from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path

from .models import new_id


MAX_CONTEXT_BYTES = 64 * 1024
MAX_CONTEXT_LINES = 400
HALF_CONTEXT_LINES = MAX_CONTEXT_LINES // 2


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save_text(self, session_id: str, content: str, suffix: str = ".log") -> dict:
        artifact_id = new_id("artifact")
        directory = self.root / session_id
        directory.mkdir(parents=True, exist_ok=True)
        final_path = directory / f"{artifact_id}{suffix}"
        temp_path = final_path.with_suffix(final_path.suffix + ".part")
        data = content.encode("utf-8", errors="replace")
        with temp_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        preview, truncated = clip_output(content)
        return {
            "id": artifact_id,
            "relative_path": str(final_path.relative_to(self.root)),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "lines": content.count("\n") + (1 if content else 0),
            "mime_type": mimetypes.guess_type(final_path.name)[0] or "text/plain",
            "preview": preview,
            "truncated": truncated,
        }

    def resolve(self, artifact_id: str) -> Path:
        matches = list(self.root.glob(f"*/{artifact_id}.*"))
        if not matches:
            raise KeyError(f"Unknown artifact: {artifact_id}")
        return matches[0]


def clip_output(content: str) -> tuple[str, bool]:
    encoded = content.encode("utf-8", errors="replace")
    lines = content.splitlines()
    if len(encoded) <= MAX_CONTEXT_BYTES and len(lines) <= MAX_CONTEXT_LINES:
        return content, False
    head = lines[:HALF_CONTEXT_LINES]
    tail = lines[-HALF_CONTEXT_LINES:] if len(lines) > HALF_CONTEXT_LINES else []
    omitted = max(0, len(lines) - len(head) - len(tail))
    preview = "\n".join(
        [
            *head,
            f"\n… output truncated: {omitted} lines omitted; full output is stored as an artifact …\n",
            *tail,
        ]
    )
    data = preview.encode("utf-8", errors="replace")
    if len(data) > MAX_CONTEXT_BYTES:
        half = MAX_CONTEXT_BYTES // 2
        preview = (
            data[:half].decode("utf-8", errors="ignore")
            + "\n… byte limit reached …\n"
            + data[-half:].decode("utf-8", errors="ignore")
        )
    return preview, True

