from pathlib import Path

from traceforge.artifacts import ArtifactStore, MAX_CONTEXT_BYTES


def test_large_tool_output_is_clipped_but_full_artifact_is_retained(tmp_path: Path):
    content = "".join(f"line {index} {'x' * 200}\n" for index in range(1000))
    artifact = ArtifactStore(tmp_path).save_text("session", content)
    full_path = ArtifactStore(tmp_path).resolve(artifact["id"])
    assert full_path.read_text(encoding="utf-8") == content
    assert artifact["truncated"] is True
    assert len(artifact["preview"].encode("utf-8")) <= MAX_CONTEXT_BYTES + 100
    assert artifact["sha256"]
