export type Health = {
  ok: boolean;
  model_configured: boolean;
  sandbox: {
    backend: string;
    ready: boolean;
    reason: string;
    supports_network_toggle: boolean;
    supports_resource_limits: boolean;
  };
};

export type ModelSettings = {
  provider: "openai";
  configured: boolean;
  has_api_key: boolean;
  model: string;
  base_url: string;
  brief_model: string;
  fallback_model: string;
  source: "web" | "environment";
  load_error: string | null;
};

export type Workspace = {
  id: string;
  path: string;
  name: string;
  kind: "git" | "directory";
  created_at: string;
  available?: boolean;
};

export type SkillInfo = {
  name: string;
  description: string;
  path: string;
  source: "project" | "traceforge" | "global";
  disable_model_invocation: boolean;
};

export type SessionSummary = {
  id: string;
  workspace_id: string;
  workspace: string;
  title: string;
  created_at: string;
  entry_count: number;
  turn_count: number;
  archived: boolean;
  active_leaf_id: string | null;
  permission_mode: PermissionMode;
};

export type PermissionMode = "request_approval" | "auto_approve" | "full_access";

export type SessionEntry = {
  type: string;
  id: string;
  parent_id: string | null;
  seq: number;
  run_id: string | null;
  timestamp: string;
  payload: Record<string, any>;
};

export type SessionDetail = {
  header: {
    id: string;
    workspace_id: string;
    workspace: string;
    created_at: string;
  };
  entries: SessionEntry[];
  active_branch: SessionEntry[];
  active_leaf_id: string | null;
  active_workspace: string;
  recovery_issues: string[];
  archived: boolean;
  permission_mode: PermissionMode;
};

export type TreeNode = {
  entry: SessionEntry;
  target_entry_id: string;
  assistant_preview: string;
  question: string;
  event_count: number;
  tool_count: number;
  status: string;
  checkpoint_id: string | null;
  branch_tips: { entry_id: string; seq: number; active: boolean }[];
  children: TreeNode[];
  orphaned: boolean;
};

export type BranchPreview = {
  available: boolean;
  reason?: string;
  checkpoint_id?: string;
  current_fingerprint?: string;
  git_head_change?: { from: string; to: string } | null;
  changes: { path: string; action: "create" | "modify" | "delete" }[];
};

export type CheckpointInfo = {
  entry_id: string;
  checkpoint_id: string;
  seq: number;
  timestamp: string;
  reason: string;
  file_count: number;
  total_bytes: number;
};

export type WorkspaceStatus = {
  state: "unbound" | "aligned" | "diverged";
  current_fingerprint: string;
  checkpoint_id: string | null;
  reason: string;
  restorable?: boolean;
  changes: { path: string; action: "create" | "modify" | "delete" }[];
};

export type FileNode = {
  path: string;
  name: string;
  type: "file" | "directory";
  size: number | null;
};

export type RunEvent = {
  session_id?: string;
  run_id?: string | null;
  seq?: number;
  type: string;
  timestamp?: string;
  payload?: Record<string, any>;
};
