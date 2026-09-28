---
name: traceforge-review
description: Review TraceForge code changes for correctness, security boundaries, and regression risks when the user asks for a code review.
---

# TraceForge code review

1. Confirm the requested review scope and inspect the affected files and nearby callers.
2. Trace Agent state transitions, JSONL persistence, tool policy, approval, and sandbox paths when relevant.
3. Run focused tests for code that changed when the environment permits.
4. Report concrete findings with file locations and severity. State what was tested and any remaining uncertainty.

Do not modify files unless the user asks for a fix. This skill does not change tool permissions or approval requirements.
