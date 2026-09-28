import type { BranchPreview, CheckpointInfo, ModelSettings, PermissionMode, WorkspaceStatus } from "./types";

const API = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body != null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers,
  });
  if (!response.ok) {
    const contentType = response.headers.get("content-type") ?? "";
    if (contentType.includes("application/json")) {
      const body = await response.json().catch(() => null);
      const detail = body?.detail;
      if (typeof detail === "string") throw new Error(detail);
      if (Array.isArray(detail)) {
        const messages = detail.map((item) => String(item?.msg ?? "请求参数无效"));
        throw new Error(messages.join("；"));
      }
    }
    throw new Error(`${response.status} ${response.statusText}`);
  }
  const contentType = response.headers.get("content-type") ?? "";
  return (contentType.includes("application/json") ? response.json() : response.text()) as Promise<T>;
}

export const api = {
  health: () => request<any>("/health"),
  modelSettings: () => request<ModelSettings>("/model-settings"),
  updateModelSettings: (settings: { api_key?: string; model: string; base_url: string; brief_model: string | null; fallback_model: string | null }) =>
    request<ModelSettings>("/model-settings", {
      method: "PUT",
      headers: { "X-TraceForge-UI": "1" },
      body: JSON.stringify(settings),
    }),
  resetModelSettings: () => request<ModelSettings>("/model-settings", {
    method: "DELETE",
    headers: { "X-TraceForge-UI": "1" },
  }),
  workspaces: () => request<any[]>("/workspaces"),
  pickWorkspaceDirectory: () =>
    request<{ cancelled: boolean; workspace: import("./types").Workspace | null }>("/workspaces/pick-directory", {
      method: "POST",
      headers: { "X-TraceForge-UI": "1" },
    }),
  relocateWorkspace: (workspaceId: string) =>
    request<{ cancelled: boolean; workspace: import("./types").Workspace | null }>(`/workspaces/${workspaceId}/relocate`, {
      method: "POST",
      headers: { "X-TraceForge-UI": "1" },
    }),
  files: (workspaceId: string) => request<any[]>(`/workspaces/${workspaceId}/files`),
  skills: (workspaceId: string) => request<{ skills: import("./types").SkillInfo[]; warnings: string[] }>(`/workspaces/${workspaceId}/skills`),
  file: (workspaceId: string, path: string) =>
    request<string>(`/workspaces/${workspaceId}/file?path=${encodeURIComponent(path)}`),
  diff: (workspaceId: string) => request<string>(`/workspaces/${workspaceId}/diff`),
  sessionFiles: (sessionId: string) => request<any[]>(`/sessions/${sessionId}/files`),
  sessionFile: (sessionId: string, path: string) =>
    request<string>(`/sessions/${sessionId}/file?path=${encodeURIComponent(path)}`),
  sessionDiff: (sessionId: string) => request<string>(`/sessions/${sessionId}/diff`),
  sessions: () => request<any[]>("/sessions"),
  createSession: (workspaceId: string) =>
    request<any>("/sessions", { method: "POST", body: JSON.stringify({ workspace_id: workspaceId }) }),
  archiveSession: (sessionId: string, archived: boolean) =>
    request<{ id: string; archived: boolean }>(`/sessions/${sessionId}/archive`, {
      method: "PATCH", body: JSON.stringify({ archived }),
    }),
  deleteSession: (sessionId: string) =>
    request<{ id: string; deleted: boolean; preserved_branches: string[] }>(`/sessions/${sessionId}`, { method: "DELETE" }),
  session: (sessionId: string) => request<any>(`/sessions/${sessionId}`),
  setPermissionMode: (sessionId: string, mode: PermissionMode) =>
    request<{ id: string; mode: PermissionMode }>(`/sessions/${sessionId}/permission-mode`, {
      method: "PATCH",
      headers: { "X-TraceForge-UI": "1" },
      body: JSON.stringify({ mode }),
    }),
  activeRun: (sessionId: string) => request<{ run_id: string; status: string } | null>(`/sessions/${sessionId}/active-run`),
  tree: (sessionId: string) => request<any[]>(`/sessions/${sessionId}/tree`),
  run: (sessionId: string, content: string) =>
    request<any>(`/sessions/${sessionId}/runs`, { method: "POST", body: JSON.stringify({ content }) }),
  cancel: (runId: string) => request<any>(`/runs/${runId}/cancel`, { method: "POST" }),
  approve: (approvalId: string, sessionId: string, decision: string, fingerprint: string) =>
    request<any>(`/approvals/${approvalId}/decision`, {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, decision, fingerprint }),
    }),
  answerClarification: (questionId: string, sessionId: string, answer: string) =>
    request<any>(`/clarifications/${questionId}/answer`, {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, answer }),
    }),
  branchPreview: (sessionId: string, targetEntryId: string, operation: "branch" | "rollback" = "branch") =>
    request<BranchPreview>(`/sessions/${sessionId}/branch-preview?target_entry_id=${encodeURIComponent(targetEntryId)}&operation=${operation}`),
  checkpoints: (sessionId: string) => request<CheckpointInfo[]>(`/sessions/${sessionId}/checkpoints`),
  workspaceStatus: (sessionId: string) => request<WorkspaceStatus>(`/sessions/${sessionId}/workspace-status`),
  adoptWorkspace: (sessionId: string, expectedCurrent: string) =>
    request<any>(`/sessions/${sessionId}/adopt-workspace`, {
      method: "POST", body: JSON.stringify({ expected_current: expectedCurrent }),
    }),
  rollback: (sessionId: string, targetEntryId: string, expectedCurrent: string) =>
    request<any>(`/sessions/${sessionId}/rollback`, {
      method: "POST",
      body: JSON.stringify({ target_entry_id: targetEntryId, expected_current: expectedCurrent }),
    }),
  branch: (sessionId: string, targetEntryId: string, includeSummary: boolean, mode: "fork" | "resume", expectedCurrent?: string) =>
    request<any>(`/sessions/${sessionId}/branch`, {
      method: "POST",
      body: JSON.stringify({ target_entry_id: targetEntryId, include_summary: includeSummary, mode, expected_current: expectedCurrent }),
    }),
};
