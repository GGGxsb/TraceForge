import {
  AlertTriangle,
  Archive,
  ArchiveRestore,
  BarChart3,
  BookOpen,
  Bot,
  BrainCircuit,
  Braces,
  Check,
  ChevronRight,
  Code2,
  File,
  FileDiff,
  Files,
  Folder,
  FolderOpen,
  GitBranch,
  HelpCircle,
  Loader2,
  LockKeyhole,
  Maximize2,
  MessageSquareMore,
  Plus,
  RefreshCw,
  RotateCcw,
  Send,
  Settings2,
  ShieldCheck,
  Square,
  TerminalSquare,
  Trash2,
  User,
  X,
} from "lucide-react";
import { FormEvent, Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { api } from "./api";
import { buildActivityGroups, reasoningSections, reasoningText } from "./activity";
import type { ActivityGroup } from "./activity";
import { DiffViewer } from "./DiffViewer";
import { parseDiff } from "./diff";
import { countTurnTree, SessionTree } from "./SessionTree";
import type {
  BranchPreview,
  CheckpointInfo,
  FileNode,
  Health,
  ModelSettings,
  PermissionMode,
  RunEvent,
  SessionDetail,
  SessionEntry,
  SessionSummary,
  SkillInfo,
  TreeNode,
  Workspace,
  WorkspaceStatus,
} from "./types";

type RightTab = "tools" | "files" | "diff" | "tree" | "questions" | "skills" | "stats";
type BranchMove = { targetEntryId: string; kind: "fork" | "resume"; title: string };

function CheckpointPreview({ preview, loading }: { preview: BranchPreview | null; loading: boolean }) {
  if (loading) return <div className="checkpoint-preview">正在检查代码差异…</div>;
  if (!preview) return <div className="checkpoint-preview">无法读取检查点。</div>;
  if (!preview.available) return <div className="checkpoint-preview unavailable">{preview.reason}</div>;
  return <div className="checkpoint-preview">
    <strong>代码状态 · {preview.changes.length ? `将变动 ${preview.changes.length} 个文件` : "文件无需变动"}</strong>
    {preview.git_head_change && <small>目标工作树提交：{preview.git_head_change.to.slice(0, 12)}（当前 {preview.git_head_change.from.slice(0, 12)}）</small>}
    {preview.changes.length > 0 && <div className="checkpoint-file-list">{preview.changes.slice(0, 40).map((change) =>
      <div key={change.path}><em className={change.action}>{change.action === "create" ? "新增" : change.action === "delete" ? "删除" : "修改"}</em><code>{change.path}</code></div>
    )}{preview.changes.length > 40 && <small>另有 {preview.changes.length - 40} 个文件</small>}</div>}
    <small>当前代码会先保存为检查点；生成文件和被忽略文件不参与恢复。</small>
  </div>;
}

type ExplorerNode = FileNode & { children: ExplorerNode[] };

function buildExplorer(files: FileNode[]): ExplorerNode[] {
  const nodes = new Map(files.map((file) => [file.path, { ...file, children: [] as ExplorerNode[] }]));
  const roots: ExplorerNode[] = [];
  for (const node of nodes.values()) {
    const parentPath = node.path.slice(0, node.path.lastIndexOf("/"));
    const parent = nodes.get(parentPath);
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  const sort = (items: ExplorerNode[]) => {
    items.sort((a, b) => (a.type === b.type ? a.name.localeCompare(b.name, "zh-CN") : a.type === "directory" ? -1 : 1));
    items.forEach((item) => sort(item.children));
  };
  sort(roots);
  return roots;
}

function FileTreeItem({ node, depth, selectedFile, onOpen }: { node: ExplorerNode; depth: number; selectedFile: string; onOpen: (path: string) => void }) {
  const [open, setOpen] = useState(depth === 0);
  const isDirectory = node.type === "directory";
  return (
    <div className="explorer-node">
      <button
        className={`explorer-row ${selectedFile === node.path ? "active" : ""}`}
        style={{ paddingLeft: 8 + depth * 15 }}
        onClick={() => isDirectory ? setOpen(!open) : onOpen(node.path)}
        title={node.path}
        aria-expanded={isDirectory ? open : undefined}
      >
        {isDirectory ? <ChevronRight size={12} className={`explorer-chevron ${open ? "rotated" : ""}`} /> : <span className="explorer-spacer" />}
        {isDirectory ? (open ? <FolderOpen size={14} /> : <Folder size={14} />) : <File size={14} />}
        <span>{node.name}</span>
      </button>
      {isDirectory && open && node.children.map((child) => (
        <FileTreeItem key={child.path} node={child} depth={depth + 1} selectedFile={selectedFile} onOpen={onOpen} />
      ))}
    </div>
  );
}

const visibleEntryTypes = new Set([
  "user_message",
  "assistant_message",
  "clarification_answer",
  "approval_request",
  "compaction",
  "branch_summary",
  "run_state",
]);

const workspaceChangingTools = new Set(["apply_patch", "create_file", "delete_file", "move_file", "run_command"]);

function shortTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function statusLabel(status?: string) {
  const labels: Record<string, string> = {
    idle: "空闲",
    briefing: "需求检查",
    discovering: "只读调查",
    clarifying: "等待澄清",
    executing: "执行中",
    waiting_approval: "等待审核",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };
  return labels[status ?? ""] ?? status ?? "空闲";
}

function MarkdownMessage({ content }: { content: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
          table: ({ children }) => <div className="markdown-table-scroll"><table>{children}</table></div>,
        }}
      >{content}</ReactMarkdown>
    </div>
  );
}

const summaryFields: { key: string; label: string }[] = [
  { key: "branch_goal", label: "分支目标" },
  { key: "goal", label: "当前目标" },
  { key: "constraints", label: "用户约束" },
  { key: "completed", label: "已完成" },
  { key: "in_progress", label: "进行中" },
  { key: "pending", label: "待处理" },
  { key: "decisions", label: "关键决策" },
  { key: "read_files", label: "已读文件" },
  { key: "modified_files", label: "已改文件" },
  { key: "commands_and_tests", label: "命令与测试" },
  { key: "errors_and_blockers", label: "错误与阻塞" },
  { key: "next_steps", label: "下一步" },
  { key: "recommended_next_step", label: "建议继续" },
];

function StructuredSummary({ value }: { value: unknown }) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return <pre>{JSON.stringify(value, null, 2)}</pre>;
  const summary = value as Record<string, unknown>;
  const fields = summaryFields.map((field) => ({ ...field, value: summary[field.key] })).filter(({ value }) =>
    typeof value === "string" ? value.trim() : Array.isArray(value) && value.length > 0,
  );
  if (!fields.length) return <pre>{JSON.stringify(value, null, 2)}</pre>;
  return <div className="summary-fields">{fields.map(({ key, label, value }) => (
    <section key={key} className="summary-field"><h4>{label}</h4>{Array.isArray(value)
      ? <ul>{value.map((item, index) => <li key={`${key}:${index}`}>{String(item)}</li>)}</ul>
      : <p>{String(value)}</p>}
    </section>
  ))}</div>;
}

function EntryCard({ entry, activity, hasInlineReasoning, onOpenTools }: {
  entry: SessionEntry;
  activity?: ActivityGroup;
  hasInlineReasoning?: boolean;
  onOpenTools?: () => void;
}) {
  const payload = entry.payload;
  if (entry.type === "user_message") {
    return (
      <article className={`message user-message ${hasInlineReasoning ? "with-reasoning" : ""}`}>
        <div className="avatar user"><User size={15} /></div>
        <div><header>你 <time>{shortTime(entry.timestamp)}</time><span className="message-activity-actions">{activity && activity.toolCount > 0 && <button className="message-activity-link" onClick={onOpenTools} title="查看本轮工具调用"><TerminalSquare size={13} />工具 {activity.toolCount}</button>}</span></header><p>{payload.content}</p></div>
      </article>
    );
  }
  if (entry.type === "assistant_message") {
    return (
      <article className="message assistant-message">
        <div className="avatar agent"><Bot size={16} /></div>
        <div><header>TraceForge <time>{shortTime(entry.timestamp)}</time></header><MarkdownMessage content={String(payload.content ?? "")} /></div>
      </article>
    );
  }
  if (entry.type === "clarification_answer") {
    return (
      <article className="trace-card clarification-card">
        <div className="trace-title"><Check size={15} /> 已回答澄清问题<span className="message-activity-actions">{activity && activity.toolCount > 0 && <button className="message-activity-link" onClick={onOpenTools} title="查看本轮工具调用"><TerminalSquare size={13} />工具 {activity.toolCount}</button>}</span></div>
        {payload.question && <p>{payload.question}</p>}
        <p><b>{payload.answer}</b></p>
      </article>
    );
  }
  if (entry.type === "approval_request") {
    return (
      <article className="trace-card approval-card">
        <div className="trace-title"><LockKeyhole size={15} /> 等待人工审核</div>
        <b>{payload.tool_name}</b>
        <p>{(payload.policy?.reasons ?? []).join("；")}</p>
      </article>
    );
  }
  if (entry.type === "compaction" || entry.type === "branch_summary") {
    return (
      <article className="trace-card summary-card">
        <div className="trace-title"><Braces size={15} /> {entry.type === "compaction" ? "滚动上下文摘要" : "分支上下文回填"}</div>
        <details><summary>查看{entry.type === "compaction" ? "保留的上下文" : "离开分支的关键上下文"}</summary><StructuredSummary value={payload.summary} /></details>
      </article>
    );
  }
  if (entry.type === "run_state" && (["failed", "cancelled"].includes(payload.status) || payload.checkpoint_error)) {
    return <article className="trace-card error"><div className="trace-title"><AlertTriangle size={15} /> {payload.checkpoint_error ? "代码检查点失败" : statusLabel(payload.status)}</div><p>{payload.checkpoint_error ?? payload.error}</p></article>;
  }
  return null;
}

function InlineReasoning({ activity, liveReasoning, streaming }: {
  activity?: ActivityGroup;
  liveReasoning: string;
  streaming: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const contentRef = useRef<HTMLPreElement>(null);
  const followTailRef = useRef(true);
  const sections = reasoningSections(activity, liveReasoning);
  const content = sections.join("\n\n");
  const preview = sections.at(-1)?.replace(/\s+/g, " ").trim() ?? "";
  const shortPreview = preview.length > 112 ? `…${preview.slice(-112)}` : preview;
  useEffect(() => {
    if (expanded && streaming && followTailRef.current && contentRef.current) contentRef.current.scrollTop = contentRef.current.scrollHeight;
  }, [content, expanded, streaming]);
  if (!content) return null;
  return (
    <div className={`inline-reasoning ${streaming ? "live" : ""}`}>
      <button type="button" className="inline-reasoning-toggle" aria-expanded={expanded} aria-label={expanded ? "收起思考过程" : "展开思考过程"} onClick={() => { followTailRef.current = streaming; setExpanded((value) => !value); }}>
        <BrainCircuit size={14} />
        <span className="inline-reasoning-label">{streaming ? "正在思考" : "思考过程"}</span>
        {streaming && <span className="inline-reasoning-pulse" aria-label="实时更新" />}
        <span className="inline-reasoning-preview">{shortPreview}</span>
        <ChevronRight size={13} className={`inline-reasoning-chevron ${expanded ? "open" : ""}`} />
      </button>
      {expanded && <pre ref={contentRef} className="inline-reasoning-content" onScroll={(event) => {
        const element = event.currentTarget;
        followTailRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 40;
      }}>{content}</pre>}
    </div>
  );
}

function ToolPanel({ groups, selectedId, onSelect }: {
  groups: ActivityGroup[];
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  const group = groups.find((item) => item.id === selectedId) ?? groups.at(-1);
  const results = new Map((group?.entries ?? []).filter((entry) => entry.type === "tool_result").map((entry) => [String(entry.payload.call_id), entry]));
  const toolCalls = (group?.entries ?? []).filter((entry) => entry.type === "tool_call").reverse();
  return (
    <>
      <div className="side-title"><span>工具记录</span><small>{group?.toolCount ?? 0} 次调用</small></div>
      {group && <label className="activity-turn-picker">对话轮次
        <select value={group.id} onChange={(event) => onSelect(event.target.value)}>
          {groups.map((item, index) => {
            const content = String(item.turn?.payload.content ?? item.turn?.payload.answer ?? "恢复记录").replace(/\s+/g, " ");
            return <option key={item.id} value={item.id}>{index + 1}. {content.slice(0, 32)}{content.length > 32 ? "…" : ""}</option>;
          })}
        </select>
      </label>}
      <div className="activity-step-list">
        {toolCalls.length === 0 && <p className="side-empty">暂无工具调用。</p>}
        {toolCalls.map((entry) => {
          const result = results.get(String(entry.payload.call_id));
          const failed = !!result?.payload.is_error;
          return (
            <details key={entry.id} className={`activity-step tool-step ${failed ? "failed" : ""}`}>
              <summary><TerminalSquare size={15} /><span>{String(entry.payload.name ?? "工具调用")}</span><small>{result ? failed ? "失败" : "完成" : "运行中"}</small><ChevronRight size={13} className="disclosure" /></summary>
              <div className="activity-step-body">
                <b>参数</b><pre>{JSON.stringify(entry.payload.arguments ?? {}, null, 2)}</pre>
                {result && <><b>结果{typeof result.payload.exit_code === "number" ? ` · 退出码 ${result.payload.exit_code}` : ""}</b><pre>{String(result.payload.output ?? "")}</pre></>}
                {result?.payload.artifact_id && <a href={`/api/artifacts/${result.payload.artifact_id}`} target="_blank" rel="noreferrer">查看完整输出</a>}
              </div>
            </details>
          );
        })}
      </div>
    </>
  );
}

function ClarificationForm({ question, index, total, submitting, onAnswer }: {
  question: SessionEntry;
  index: number;
  total: number;
  submitting: boolean;
  onAnswer: (questionId: string, answer: string) => Promise<void>;
}) {
  const [answer, setAnswer] = useState("");
  const options = Array.isArray(question.payload.options)
    ? question.payload.options.filter((value: unknown): value is string => typeof value === "string" && !!value.trim())
    : [];
  return (
    <form className="clarification-form" onSubmit={(event) => {
      event.preventDefault();
      if (answer.trim() && !submitting) void onAnswer(String(question.payload.question_id), answer.trim());
    }}>
      <header><HelpCircle size={16} /><span>需要你确认 · {index + 1}/{total}</span></header>
      <p className="clarification-question">{question.payload.question}</p>
      {question.payload.impact && <p className="clarification-impact">影响：{question.payload.impact}</p>}
      {options.length > 0 && <div className="clarification-options" role="group" aria-label="可选回答">
        {options.map((option) => <button key={option} type="button" className={answer === option ? "selected" : ""} onClick={() => setAnswer(option)} disabled={submitting}>{option}</button>)}
      </div>}
      <div className="clarification-reply">
        <input value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder="也可以输入自己的回答" aria-label={`回答：${question.payload.question}`} disabled={submitting} />
        <button type="submit" disabled={!answer.trim() || submitting}>{submitting ? <Loader2 size={15} className="spin" /> : <Send size={15} />}提交回答</button>
      </div>
    </form>
  );
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [workspaceId, setWorkspaceId] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [switchingBranchId, setSwitchingBranchId] = useState("");
  const [selectedTreeNodeId, setSelectedTreeNodeId] = useState("");
  const [treeExpanded, setTreeExpanded] = useState(false);
  const [branchNotice, setBranchNotice] = useState("");
  const [pendingBranchMove, setPendingBranchMove] = useState<BranchMove | null>(null);
  const [pendingRollback, setPendingRollback] = useState<{ targetEntryId: string; title: string } | null>(null);
  const [branchPreview, setBranchPreview] = useState<BranchPreview | null>(null);
  const [checkpoints, setCheckpoints] = useState<CheckpointInfo[]>([]);
  const [workspaceStatus, setWorkspaceStatus] = useState<WorkspaceStatus | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [files, setFiles] = useState<FileNode[]>([]);
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [skillWarnings, setSkillWarnings] = useState<string[]>([]);
  const [selectedFile, setSelectedFile] = useState("");
  const [fileContent, setFileContent] = useState("");
  const [diff, setDiff] = useState("");
  const [rightTab, setRightTab] = useState<RightTab>("files");
  const [selectedToolTurnId, setSelectedToolTurnId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [streaming, setStreaming] = useState("");
  const [streamingReasoning, setStreamingReasoning] = useState("");
  const [reasoningStreaming, setReasoningStreaming] = useState(false);
  const [diffExpanded, setDiffExpanded] = useState(false);
  const [selectedDiffId, setSelectedDiffId] = useState("");
  const openDiffExpanded = useCallback(() => setDiffExpanded(true), []);
  const closeDiffExpanded = useCallback(() => setDiffExpanded(false), []);
  const [activeRun, setActiveRun] = useState("");
  const [pickingFolder, setPickingFolder] = useState(false);
  const [busy, setBusy] = useState(false);
  const [answeringQuestionId, setAnsweringQuestionId] = useState("");
  const [error, setError] = useState("");
  const [modelSettingsOpen, setModelSettingsOpen] = useState(false);
  const [fullAccessPending, setFullAccessPending] = useState(false);
  const [permissionBusy, setPermissionBusy] = useState(false);
  const [modelSettings, setModelSettings] = useState<ModelSettings | null>(null);
  const [modelName, setModelName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [briefModelName, setBriefModelName] = useState("");
  const [fallbackModelName, setFallbackModelName] = useState("");
  const [modelKey, setModelKey] = useState("");
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const refreshTimer = useRef<number | undefined>(undefined);
  const workspaceRefreshPending = useRef(false);
  const workspaceIdRef = useRef("");
  const sessionIdRef = useRef("");
  const selectedFileRef = useRef("");
  const initializedSelection = useRef(false);
  const lastSeqRef = useRef(0);
  const timelineRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const stickToBottomRef = useRef(true);

  const loadBase = useCallback(async () => {
    try {
      const [nextHealth, nextWorkspaces, nextSessions] = await Promise.all([
        api.health(), api.workspaces(), api.sessions(),
      ]);
      setHealth(nextHealth);
      setWorkspaces(nextWorkspaces);
      setSessions(nextSessions);
      if (!initializedSelection.current) {
        initializedSelection.current = true;
        const remembered = window.localStorage.getItem("traceforge:last-project");
        const initialWorkspaceId = nextWorkspaces.find((item) => item.id === remembered)?.id ?? nextWorkspaces[0]?.id ?? "";
        setWorkspaceId(initialWorkspaceId);
        setSessionId(nextSessions.find((item) => item.workspace_id === initialWorkspaceId && !item.archived)?.id ?? "");
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  const loadSession = useCallback(async (id: string) => {
    if (!id) {
      setSession(null);
      setTree([]);
      setCheckpoints([]);
      return;
    }
    const [detail, nextTree, running, nextCheckpoints] = await Promise.all([api.session(id), api.tree(id), api.activeRun(id), api.checkpoints(id)]);
    if (sessionIdRef.current !== id) return;
    lastSeqRef.current = Math.max(lastSeqRef.current, detail.entries.at(-1)?.seq ?? 0);
    setSession(detail);
    setTree(nextTree);
    setCheckpoints(nextCheckpoints);
    if (running) {
      setActiveRun(running.run_id);
      setBusy(true);
    }
  }, []);

  const loadWorkspaceStatus = useCallback(async (id: string) => {
    if (!id) { setWorkspaceStatus(null); return; }
    const status = await api.workspaceStatus(id);
    if (sessionIdRef.current === id) setWorkspaceStatus(status);
  }, []);

  const loadWorkspace = useCallback(async (id: string) => {
    if (!id) {
      setFiles([]);
      setDiff("");
      setSkills([]);
      setSkillWarnings([]);
      return;
    }
    const activeId = sessionIdRef.current;
    const [nextFiles, nextDiff, nextSkills] = await Promise.all([
      activeId ? api.sessionFiles(activeId) : api.files(id),
      activeId ? api.sessionDiff(activeId) : api.diff(id),
      api.skills(id),
    ]);
    setFiles(nextFiles);
    setDiff(nextDiff);
    setSkills(nextSkills.skills);
    setSkillWarnings(nextSkills.warnings);
    if (selectedFileRef.current) {
      try {
        setFileContent(await (activeId ? api.sessionFile(activeId, selectedFileRef.current) : api.file(id, selectedFileRef.current)));
      } catch {
        selectedFileRef.current = "";
        setSelectedFile("");
        setFileContent("");
      }
    }
  }, []);

  useEffect(() => {
    const target = pendingBranchMove?.targetEntryId ?? pendingRollback?.targetEntryId;
    if (!target || !sessionId) { setBranchPreview(null); return; }
    let cancelled = false;
    setBranchPreview(null);
    setPreviewBusy(true);
    void api.branchPreview(sessionId, target, pendingRollback ? "rollback" : "branch").then((preview) => {
      if (!cancelled) setBranchPreview(preview);
    }).catch((caught) => {
      if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
    }).finally(() => { if (!cancelled) setPreviewBusy(false); });
    return () => { cancelled = true; };
  }, [pendingBranchMove, pendingRollback, sessionId]);

  useEffect(() => { void loadBase(); }, [loadBase]);
  const selectedProjectAvailable = workspaces.find((item) => item.id === workspaceId)?.available !== false;
  useEffect(() => {
    sessionIdRef.current = sessionId;
    lastSeqRef.current = 0;
    stickToBottomRef.current = true;
    setSelectedToolTurnId("");
    setSwitchingBranchId("");
    setSelectedTreeNodeId("");
    setTreeExpanded(false);
    setBranchNotice("");
    setPendingBranchMove(null);
    setPendingRollback(null);
    setWorkspaceStatus(null);
    setStreamingReasoning("");
    setReasoningStreaming(false);
    void loadSession(sessionId).catch((caught) => setError(String(caught)));
    if (selectedProjectAvailable) {
      void loadWorkspaceStatus(sessionId).catch((caught) => setError(String(caught)));
    }
  }, [loadSession, loadWorkspaceStatus, sessionId, selectedProjectAvailable]);
  useEffect(() => {
    if (!selectedProjectAvailable) {
      setFiles([]); setDiff(""); setSkills([]); setSkillWarnings([]);
      return;
    }
    void loadWorkspace(workspaceId).catch((caught) => setError(String(caught)));
  }, [loadWorkspace, workspaceId, sessionId, selectedProjectAvailable]);
  useEffect(() => { workspaceIdRef.current = workspaceId; }, [workspaceId]);
  useEffect(() => { selectedFileRef.current = selectedFile; }, [selectedFile]);

  useEffect(() => {
    if (!sessionId) return;
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let reasoningFrame: number | undefined;
    let pendingReasoning = "";
    const flushReasoning = () => {
      if (reasoningFrame !== undefined) window.cancelAnimationFrame(reasoningFrame);
      reasoningFrame = undefined;
      if (!pendingReasoning) return;
      const chunk = pendingReasoning;
      pendingReasoning = "";
      setStreamingReasoning((value) => value + chunk);
    };
    const connect = () => {
      socket = new WebSocket(`${protocol}://${location.host}/api/sessions/${sessionId}/events?after_seq=${lastSeqRef.current}`);
      socket.onmessage = (message) => {
        const event: RunEvent = JSON.parse(message.data);
        if (event.type === "ping") return;
        if (event.type === "assistant_delta") {
          setStreaming((value) => value + String(event.payload?.delta ?? ""));
          return;
        }
        if (event.type === "reasoning_delta") {
          pendingReasoning += String(event.payload?.delta ?? "");
          if (reasoningFrame === undefined) reasoningFrame = window.requestAnimationFrame(flushReasoning);
          setReasoningStreaming(true);
          return;
        }
        if (event.type === "reasoning_stream_reset") {
          if (reasoningFrame !== undefined) window.cancelAnimationFrame(reasoningFrame);
          reasoningFrame = undefined;
          pendingReasoning = "";
          setStreamingReasoning("");
          setReasoningStreaming(false);
          return;
        }
        if (event.type === "assistant_stream_reset") {
          setStreaming("");
          return;
        }
        if (event.seq) lastSeqRef.current = Math.max(lastSeqRef.current, event.seq);
        if (event.type === "assistant_message") setStreaming("");
        if (event.type === "model_reasoning") {
          flushReasoning();
          setReasoningStreaming(false);
        }
        if (event.type === "run_state") {
          const status = event.payload?.status;
          if (["completed", "failed", "cancelled"].includes(status)) {
            flushReasoning();
            setBusy(false);
            setActiveRun("");
            setReasoningStreaming(false);
            workspaceRefreshPending.current = true;
            void loadWorkspaceStatus(sessionId).catch((caught) => setError(String(caught)));
          }
        }
        if (event.type === "tool_result" && workspaceChangingTools.has(String(event.payload?.tool_name ?? ""))) {
          workspaceRefreshPending.current = true;
        }
        window.clearTimeout(refreshTimer.current);
        refreshTimer.current = window.setTimeout(() => {
          const refresh = [loadSession(sessionId), loadBase()];
          if (workspaceRefreshPending.current && workspaceIdRef.current) {
            workspaceRefreshPending.current = false;
            refresh.push(loadWorkspace(workspaceIdRef.current));
          }
          void Promise.all(refresh).catch((caught) => setError(String(caught)));
        }, 80);
      };
      socket.onclose = () => {
        if (!disposed) reconnectTimer = window.setTimeout(connect, 1000);
      };
    };
    connect();
    return () => {
      disposed = true;
      if (reasoningFrame !== undefined) window.cancelAnimationFrame(reasoningFrame);
      window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [loadBase, loadSession, loadWorkspace, loadWorkspaceStatus, sessionId]);

  const activeWorkspace = workspaces.find((item) => item.id === workspaceId);
  const explorer = useMemo(() => buildExplorer(files), [files]);
  const diffFiles = useMemo(() => parseDiff(diff), [diff]);
  const branchEntries = session?.active_branch ?? [];
  const runStats = useMemo(() => {
    const usage = (session?.active_branch ?? []).filter((entry) => entry.type === "model_usage");
    const terminal = (session?.active_branch ?? []).filter((entry) => entry.type === "run_state" && ["completed", "failed", "cancelled"].includes(String(entry.payload.status)));
    const tools = (session?.active_branch ?? []).filter((entry) => entry.type === "tool_result");
    const sum = (key: string) => usage.reduce((value, entry) => value + Number(entry.payload[key] || 0), 0);
    const priced = usage.some((entry) => entry.payload.estimated_cost_usd != null);
    return {
      input: sum("input_tokens"), output: sum("output_tokens"), cached: sum("cached_input_tokens"),
      reasoning: sum("reasoning_tokens"), total: sum("total_tokens"), requests: usage.length,
      cost: priced ? sum("estimated_cost_usd") : null,
      elapsed: terminal.reduce((value, entry) => value + Number(entry.payload.elapsed_seconds || 0), 0),
      tools: tools.length, toolErrors: tools.filter((entry) => entry.payload.is_error).length,
    };
  }, [session]);
  const treeMetrics = useMemo(() => countTurnTree(tree), [tree]);
  const activePathIds = useMemo(
    () => new Set((session?.active_branch ?? []).map((entry) => entry.id)),
    [session?.active_branch],
  );
  const activeTurnId = useMemo(() => {
    const lastTurn = [...branchEntries].reverse().find((entry) => entry.type === "user_message" || entry.type === "clarification_answer");
    return lastTurn?.id ?? branchEntries[0]?.id ?? null;
  }, [branchEntries]);
  const latestStatus = useMemo(() => {
    const state = [...branchEntries].reverse().find((entry) => entry.type === "run_state");
    return state?.payload.status as string | undefined;
  }, [branchEntries]);
  const pendingApproval = useMemo(() => {
    const entries = branchEntries;
    const decided = new Set(entries.filter((entry) => entry.type === "approval_decision").map((entry) => entry.payload.approval_id));
    return [...entries].reverse().find((entry) => entry.type === "approval_request" && !decided.has(entry.payload.approval_id));
  }, [branchEntries]);
  const clarificationQuestions = useMemo(
    () => branchEntries.filter((entry) => entry.type === "clarification_question"),
    [branchEntries],
  );
  const answeredQuestionIds = useMemo(
    () => new Set(
      branchEntries
        .filter((entry) => entry.type === "clarification_answer")
        .map((entry) => entry.payload.question_id),
    ),
    [branchEntries],
  );
  const pendingQuestions = useMemo(
    () => clarificationQuestions.filter((entry) => !answeredQuestionIds.has(entry.payload.question_id)),
    [answeredQuestionIds, clarificationQuestions],
  );
  const activityGroups = useMemo(() => buildActivityGroups(branchEntries), [branchEntries]);
  const activityByTurnId = useMemo(() => new Map(activityGroups.map((group) => [group.id, group])), [activityGroups]);
  const toolGroups = useMemo(() => activityGroups.filter((group) => group.toolCount > 0), [activityGroups]);
  const selectedToolGroupId = toolGroups.find((group) => group.id === selectedToolTurnId)?.id ?? toolGroups.at(-1)?.id ?? "";
  const timelineEntries = useMemo(() => branchEntries.filter((entry) => visibleEntryTypes.has(entry.type) && (
    entry.type !== "run_state" || ["failed", "cancelled"].includes(String(entry.payload.status)) || Boolean(entry.payload.checkpoint_error)
  ) && !(
    entry.type === "assistant_message" && clarificationQuestions.some((question) =>
      question.run_id === entry.run_id && question.seq > entry.seq
      && question.payload.question === entry.payload.content,
    )
  )), [branchEntries, clarificationQuestions]);
  const latestTimelineEntryId = timelineEntries.at(-1)?.id;
  useEffect(() => {
    if (stickToBottomRef.current) {
      timelineRef.current?.scrollTo({ top: timelineRef.current.scrollHeight, behavior: "auto" });
    }
  }, [latestTimelineEntryId, streaming]);
  useEffect(() => {
    if (reasoningStreaming && stickToBottomRef.current) timelineRef.current?.scrollTo({ top: timelineRef.current.scrollHeight, behavior: "auto" });
  }, [reasoningStreaming]);
  useEffect(() => {
    if (!diffExpanded && !treeExpanded && !pendingBranchMove && !pendingRollback) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setDiffExpanded(false);
        setTreeExpanded(false);
        setPendingBranchMove(null);
        setPendingRollback(null);
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [diffExpanded, treeExpanded, pendingBranchMove, pendingRollback]);
  const activeSessionArchived = sessions.find((item) => item.id === sessionId)?.archived ?? false;
  const permissionMode = session?.header.id === sessionId
    ? session.permission_mode
    : sessions.find((item) => item.id === sessionId)?.permission_mode ?? "request_approval";
  const workspaceSessions = sessions.filter((item) => !workspaceId || item.workspace_id === workspaceId);
  const displayedSessions = workspaceSessions.filter((item) => item.archived === showArchived);

  function selectWorkspace(id: string) {
    if (id) window.localStorage.setItem("traceforge:last-project", id);
    workspaceIdRef.current = id;
    selectedFileRef.current = "";
    setWorkspaceId(id);
    const nextSessionId = sessions.find((item) => item.workspace_id === id && !item.archived)?.id ?? "";
    sessionIdRef.current = nextSessionId;
    setSessionId(nextSessionId);
    setSelectedFile("");
    setFileContent("");
  }

  async function browseWorkspace() {
    setPickingFolder(true);
    setError("");
    try {
      const result = await api.pickWorkspaceDirectory();
      if (result.cancelled || !result.workspace) return;
      await loadBase();
      selectWorkspace(result.workspace.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setPickingFolder(false);
    }
  }

  async function relocateWorkspace() {
    if (!workspaceId) return;
    setPickingFolder(true);
    setError("");
    try {
      const result = await api.relocateWorkspace(workspaceId);
      if (result.cancelled || !result.workspace) return;
      await loadBase();
      await Promise.all([loadSession(sessionId), loadWorkspace(workspaceId)]);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setPickingFolder(false);
    }
  }

  async function newSession() {
    if (!workspaceId) return;
    try {
      const created = await api.createSession(workspaceId);
      sessionIdRef.current = created.id;
      setSessionId(created.id);
      await loadBase();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  async function setSessionArchived(item: SessionSummary, archived: boolean) {
    if (busy || activeRun) return;
    try {
      await api.archiveSession(item.id, archived);
      if (item.id === sessionId && archived) {
        const nextId = sessions.find((candidate) => candidate.id !== item.id && candidate.workspace_id === workspaceId && !candidate.archived)?.id ?? "";
        sessionIdRef.current = nextId;
        setSessionId(nextId);
      }
      await loadBase();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  async function deleteSession(item: SessionSummary) {
    if (busy || activeRun || !window.confirm(`永久删除会话「${item.title || "新会话"}」及其工具日志？有未提交修改的分支会阻止删除，已提交的独立分支会保留 Git 引用。`)) return;
    try {
      const result = await api.deleteSession(item.id);
      if (item.id === sessionId) {
        const nextId = sessions.find((candidate) => candidate.id !== item.id && candidate.workspace_id === workspaceId && !candidate.archived)?.id ?? "";
        sessionIdRef.current = nextId;
        setSessionId(nextId);
      }
      await loadBase();
      if (result.preserved_branches.length) {
        window.alert(`会话已删除，分支提交已保存在 Git 引用中：\n${result.preserved_branches.join("\n")}`);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  async function sendPrompt(event: FormEvent) {
    event.preventDefault();
    const content = prompt.trim();
    if (!content || !sessionId || busy || pendingQuestions.length || activeSessionArchived || !selectedProjectAvailable) return;
    stickToBottomRef.current = true;
    setBusy(true);
    setStreaming("");
    setStreamingReasoning("");
    setReasoningStreaming(false);
    setPrompt("");
    setError("");
    setBranchNotice("");
    try {
      const run = await api.run(sessionId, content);
      setActiveRun(run.run_id);
    } catch (caught) {
      setBusy(false);
      setPrompt(content);
      setError(caught instanceof Error ? caught.message : String(caught));
      void loadWorkspaceStatus(sessionId).catch(() => undefined);
    }
  }

  async function answerQuestion(questionId: string, answer: string) {
    if (!sessionId || busy || answeringQuestionId) return;
    setAnsweringQuestionId(questionId);
    setError("");
    try {
      const result = await api.answerClarification(questionId, sessionId, answer);
      if (result.run_id) {
        setBusy(true);
        setActiveRun(result.run_id);
        setStreaming("");
        setStreamingReasoning("");
        setReasoningStreaming(false);
      }
      await loadSession(sessionId);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      void loadWorkspaceStatus(sessionId).catch(() => undefined);
    } finally {
      setAnsweringQuestionId("");
    }
  }

  async function decideApproval(decision: string) {
    if (!pendingApproval || !sessionId) return;
    try {
      await api.approve(
        pendingApproval.payload.approval_id,
        sessionId,
        decision,
        pendingApproval.payload.policy.fingerprint,
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  async function openFile(path: string) {
    if (!workspaceId) return;
    try {
      const content = await (sessionId ? api.sessionFile(sessionId, path) : api.file(workspaceId, path));
      selectedFileRef.current = path;
      setSelectedFile(path);
      setFileContent(content);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  function requestBranchMove(targetEntryId: string, kind: "fork" | "resume", title: string) {
    if (!sessionId || busy || activeSessionArchived) return;
    const move = { targetEntryId, kind, title };
    setPendingBranchMove(move);
  }

  async function applyBranchMove(move: BranchMove, includeSummary: boolean) {
    if (!sessionId || busy || activeSessionArchived || !branchPreview?.available || !branchPreview.current_fingerprint) return;
    const expectedCurrent = branchPreview.current_fingerprint;
    setPendingBranchMove(null);
    setBusy(true);
    setSwitchingBranchId(move.targetEntryId);
    stickToBottomRef.current = true;
    try {
      await api.branch(sessionId, move.targetEntryId, includeSummary, move.kind, expectedCurrent);
      setStreamingReasoning("");
      setReasoningStreaming(false);
      await loadSession(sessionId);
      await loadWorkspace(workspaceId);
      await loadWorkspaceStatus(sessionId);
      setTreeExpanded(false);
      setBranchNotice(move.kind === "resume"
        ? `已恢复所选分支的代码状态。${includeSummary ? "当前分支的关键上下文已摘要回填。" : "本次未生成回填摘要。"}`
        : `已从所选节点创建分支并恢复对应代码。${includeSummary ? "已回填离开分支的关键上下文。" : "下一条消息会接在该节点下。"}`);
      window.requestAnimationFrame(() => composerRef.current?.focus());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      void loadWorkspaceStatus(sessionId).catch(() => undefined);
    } finally {
      setBusy(false);
      setSwitchingBranchId("");
    }
  }

  async function applyRollback() {
    if (!sessionId || !pendingRollback || busy || !branchPreview?.available || !branchPreview.current_fingerprint) return;
    const target = pendingRollback;
    const expectedCurrent = branchPreview.current_fingerprint;
    setPendingRollback(null);
    setBusy(true);
    try {
      await api.rollback(sessionId, target.targetEntryId, expectedCurrent);
      await Promise.all([loadSession(sessionId), loadWorkspace(workspaceId)]);
      await loadWorkspaceStatus(sessionId);
      setBranchNotice(`已将代码恢复到「${target.title}」的检查点；对话仍留在当前分支。`);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      void loadWorkspaceStatus(sessionId).catch(() => undefined);
    } finally {
      setBusy(false);
    }
  }

  async function adoptCurrentWorkspace() {
    if (!sessionId || !workspaceStatus?.current_fingerprint || busy || activeSessionArchived) return;
    setBusy(true);
    setError("");
    try {
      await api.adoptWorkspace(sessionId, workspaceStatus.current_fingerprint);
      await Promise.all([loadSession(sessionId), loadWorkspaceStatus(sessionId)]);
      setBranchNotice("已将当前文件状态保存到此会话的新检查点。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      void loadWorkspaceStatus(sessionId).catch(() => undefined);
    } finally {
      setBusy(false);
    }
  }

  async function openModelSettings() {
    setModelSettingsOpen(true);
    setSettingsError("");
    setModelKey("");
    try {
      const current = await api.modelSettings();
      setModelSettings(current);
      setModelName(current.model);
      setBaseUrl(current.base_url);
      setBriefModelName(current.brief_model === current.model ? "" : current.brief_model);
      setFallbackModelName(current.fallback_model);
    } catch (caught) {
      setSettingsError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  async function saveModelSettings(event: FormEvent) {
    event.preventDefault();
    setSettingsBusy(true);
    setSettingsError("");
    try {
      const current = await api.updateModelSettings({
        api_key: modelKey.trim() || undefined,
        model: modelName.trim(),
        base_url: baseUrl.trim(),
        brief_model: briefModelName.trim() || null,
        fallback_model: fallbackModelName.trim() || null,
      });
      setModelSettings(current);
      setHealth(await api.health());
      setModelKey("");
      setModelSettingsOpen(false);
    } catch (caught) {
      setSettingsError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSettingsBusy(false);
    }
  }

  async function resetModelSettings() {
    setSettingsBusy(true);
    setSettingsError("");
    try {
      const current = await api.resetModelSettings();
      setModelSettings(current);
      setModelName(current.model);
      setBaseUrl(current.base_url);
      setBriefModelName(current.brief_model === current.model ? "" : current.brief_model);
      setFallbackModelName(current.fallback_model);
      setModelKey("");
      setHealth(await api.health());
    } catch (caught) {
      setSettingsError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSettingsBusy(false);
    }
  }

  async function updatePermissionMode(mode: PermissionMode) {
    if (!sessionId || busy || activeRun || permissionBusy || activeSessionArchived) return;
    setPermissionBusy(true);
    setError("");
    try {
      await api.setPermissionMode(sessionId, mode);
      await Promise.all([loadSession(sessionId), loadBase()]);
      setFullAccessPending(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setPermissionBusy(false);
    }
  }

  function requestPermissionMode(mode: PermissionMode) {
    if (mode === "full_access" && permissionMode !== "full_access") {
      setFullAccessPending(true);
      return;
    }
    void updatePermissionMode(mode);
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><div className="brand-mark"><Code2 size={19} /></div><div><b>TraceForge</b><span>本地代码助手</span></div></div>
        <div className="topbar-center">
          <span className={`status-pill ${health?.model_configured ? "ready" : "warn"}`}><span className="status-dot" />{health?.model_configured ? "模型已配置" : "待配置模型"}</span>
          <span className={`status-pill ${permissionMode === "full_access" && sessionId ? "warn" : health?.sandbox.ready ? "ready" : "warn"}`} title={permissionMode === "full_access" && sessionId ? "此会话的执行工具使用宿主机权限" : health?.sandbox.reason || ""}><ShieldCheck size={14} />{permissionMode === "full_access" && sessionId ? "宿主机完全访问" : health?.sandbox.ready ? "沙箱已就绪" : "沙箱不可用"}</span>
        </div>
        <div className="topbar-actions">
          <button className="icon-button" onClick={() => void openModelSettings()} title="模型设置" aria-label="模型设置"><Settings2 size={16} /></button>
          <button className="icon-button" onClick={() => void Promise.all([loadBase(), loadSession(sessionId), loadWorkspace(workspaceId)])} title="刷新"><RefreshCw size={16} /></button>
        </div>
      </header>

      {error && <div className="error-banner"><AlertTriangle size={15} /><span>{error}</span><button onClick={() => setError("")}><X size={14} /></button></div>}

      {modelSettingsOpen && (
        <div className="settings-backdrop" role="presentation">
          <form className="settings-dialog" role="dialog" aria-modal="true" aria-label="模型设置" onSubmit={(event) => void saveModelSettings(event)}>
            <header><div><b>模型设置</b><span>OpenAI Responses API</span></div><button type="button" className="icon-button" onClick={() => { setModelSettingsOpen(false); setModelKey(""); }} aria-label="关闭设置"><X size={17} /></button></header>
            <div className="settings-body">
              <label>OpenAI API Key<input type="password" autoComplete="off" value={modelKey} onChange={(event) => setModelKey(event.target.value)} placeholder={modelSettings?.has_api_key ? "已保存，留空保持不变" : "输入 API Key"} /></label>
              <label>主模型<input value={modelName} onChange={(event) => setModelName(event.target.value)} placeholder="输入 OpenAI 模型 ID" required maxLength={200} /></label>
              <label>Base URL <small>可选</small><input type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://api.openai.com/v1" maxLength={2048} /></label>
              <label>TaskBrief 模型 <small>可选</small><input value={briefModelName} onChange={(event) => setBriefModelName(event.target.value)} placeholder="留空使用主模型" maxLength={200} /></label>
              <label>备用模型 <small>可选</small><input value={fallbackModelName} onChange={(event) => setFallbackModelName(event.target.value)} placeholder="主模型请求在流开始前失败时使用" maxLength={200} /></label>
              <p className="settings-hint">Base URL 留空时使用环境变量或 SDK 默认地址。自定义地址需支持 Responses API，API Key 会发送到该地址。配置保存在本机用户目录，密钥不会在页面回显；模型可用性会在实际请求时验证。</p>
              {modelSettings?.load_error && <p className="settings-error">{modelSettings.load_error}</p>}
              {settingsError && <p className="settings-error">{settingsError}</p>}
            </div>
            <footer>
              {modelSettings?.source === "web" && <button type="button" className="settings-reset" onClick={() => void resetModelSettings()} disabled={settingsBusy || busy}>移除网页配置</button>}
              <span />
              <button type="button" onClick={() => { setModelSettingsOpen(false); setModelKey(""); }}>取消</button>
              <button type="submit" className="settings-save" disabled={settingsBusy || busy || !modelName.trim() || (!modelSettings?.has_api_key && !modelKey.trim())}>{settingsBusy ? "保存中…" : "保存配置"}</button>
            </footer>
          </form>
        </div>
      )}

      {fullAccessPending && <div className="settings-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !permissionBusy) setFullAccessPending(false); }}>
        <div className="permission-dialog" role="dialog" aria-modal="true" aria-label="启用完全访问">
          <div className="permission-dialog-icon"><AlertTriangle size={22} /></div>
          <h2>启用宿主机完全访问？</h2>
          <p>此会话后续的执行工具将以当前用户身份在宿主机运行，可以访问项目外文件和网络。工具调用不再经过沙箱，也不会逐次请求审批。</p>
          <p>模式会记录在会话历史中；运行期间无法切换。只在信任当前任务和模型时使用。</p>
          <div className="permission-dialog-actions"><button type="button" onClick={() => setFullAccessPending(false)} disabled={permissionBusy}>取消</button><button type="button" className="danger" onClick={() => void updatePermissionMode("full_access")} disabled={permissionBusy}>{permissionBusy ? "切换中…" : "启用完全访问"}</button></div>
        </div>
      </div>}

      {diffExpanded && <div className="diff-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setDiffExpanded(false); }}>
        <div className="diff-dialog" role="dialog" aria-modal="true" aria-label="宽屏查看工作区变更">
          <DiffViewer files={diffFiles} selectedId={selectedDiffId} onSelect={setSelectedDiffId} expanded onClose={closeDiffExpanded} />
        </div>
      </div>}

      {treeExpanded && <div className="tree-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setTreeExpanded(false); }}>
        <div className="tree-dialog" role="dialog" aria-modal="true" aria-label="会话树图">
          <header className="tree-dialog-heading"><div><GitBranch size={19} /><span><b>会话树</b><small>{treeMetrics.turns} 轮对话 · {treeMetrics.forks} 处分叉</small></span></div><button type="button" onClick={() => setTreeExpanded(false)} aria-label="关闭会话树"><X size={18} /></button></header>
          <SessionTree nodes={tree} activeTurnId={activeTurnId} activePath={activePathIds} selectedId={selectedTreeNodeId} onSelect={setSelectedTreeNodeId} onFork={(id, title) => requestBranchMove(id, "fork", title)} onResume={(id, title) => requestBranchMove(id, "resume", title)} onRollback={(id, title) => setPendingRollback({ targetEntryId: id, title })} busy={busy || activeSessionArchived || workspaceStatus?.state === "diverged"} forkingId={switchingBranchId} expanded />
        </div>
      </div>}

      {pendingBranchMove && <div className="branch-choice-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setPendingBranchMove(null); }}>
        <div className="branch-choice-dialog" role="dialog" aria-modal="true" aria-label="选择分支上下文回填方式">
          <div className="branch-choice-heading"><Bot size={19} /><span>TraceForge 想确认</span><button type="button" onClick={() => setPendingBranchMove(null)} aria-label="取消分支操作"><X size={17} /></button></div>
          <h2>{pendingBranchMove.kind === "resume" ? "切换回已有分支" : "从此创建新分支"}</h2>
          <p>目标节点：<strong>{pendingBranchMove.title}</strong></p>
          <CheckpointPreview preview={branchPreview} loading={previewBusy} />
          <p>要把当前分支的关键上下文压缩成结构化摘要，回填到目标分支吗？</p>
          <small>选择“仅切换”不会生成新的摘要；目标分支已有的对话和摘要会保留。</small>
          <div className="branch-choice-actions"><button type="button" onClick={() => setPendingBranchMove(null)}>取消</button><button type="button" disabled={!branchPreview?.available || previewBusy} onClick={() => void applyBranchMove(pendingBranchMove, false)}>仅切换</button><button type="button" className="primary" disabled={!branchPreview?.available || previewBusy} onClick={() => void applyBranchMove(pendingBranchMove, true)}>压缩摘要并回填</button></div>
        </div>
      </div>}

      {pendingRollback && <div className="branch-choice-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setPendingRollback(null); }}>
        <div className="branch-choice-dialog" role="dialog" aria-modal="true" aria-label="恢复代码检查点">
          <div className="branch-choice-heading"><RotateCcw size={19} /><span>代码检查点</span><button type="button" onClick={() => setPendingRollback(null)} aria-label="取消代码回滚"><X size={17} /></button></div>
          <h2>恢复「{pendingRollback.title}」的代码</h2>
          <p>只恢复工作区文件；当前对话分支保持不变。</p>
          <CheckpointPreview preview={branchPreview} loading={previewBusy} />
          <div className="branch-choice-actions"><button type="button" onClick={() => setPendingRollback(null)}>取消</button><button type="button" className="primary" disabled={!branchPreview?.available || previewBusy} onClick={() => void applyRollback()}>保存当前状态并恢复</button></div>
        </div>
      </div>}

      <main className="workspace-grid">
        <aside className="left-panel panel">
          <section className="panel-section workspace-section">
            <div className="section-heading"><span>项目</span><span className="section-count">{workspaces.length}</span></div>
            <div className="project-list">
              {workspaces.map((workspace) => (
                <button type="button" key={workspace.id} className={`project-item ${workspace.id === workspaceId ? "active" : ""}`} onClick={() => selectWorkspace(workspace.id)} disabled={busy || pickingFolder} title={workspace.path}>
                  <span className="project-icon">{workspace.kind === "git" ? <GitBranch size={15} /> : <Folder size={15} />}</span>
                  <span className="project-info"><b>{workspace.name}</b><small>{workspace.available === false ? "目录不可用 · 请重新定位" : `${sessions.filter((item) => item.workspace_id === workspace.id && !item.archived).length} 个会话 · ${workspace.kind === "git" ? "Git 仓库" : "普通目录"}`}</small></span>
                </button>
              ))}
              {workspaces.length === 0 && <p className="project-empty">打开一个本地项目开始使用</p>}
            </div>
            <button type="button" className="open-project-button" onClick={() => void browseWorkspace()} disabled={busy || pickingFolder}>
              {pickingFolder ? <Loader2 size={15} className="spin" /> : <Plus size={15} />}打开项目
            </button>
            {activeWorkspace && <div className="project-detail"><span title={activeWorkspace.path}>{activeWorkspace.path}</span><button type="button" onClick={() => void relocateWorkspace()} disabled={busy || pickingFolder} title="项目移动后，重新选择其目录" aria-label="重新定位项目"><FolderOpen size={14} />重新定位</button></div>}
          </section>

          <section className="panel-section sessions-section">
            <div className="section-heading"><span>会话</span><span className="section-count">{workspaceSessions.filter((item) => !item.archived).length}</span></div>
            <button className="new-session-button" onClick={() => void newSession()} disabled={!workspaceId || activeWorkspace?.available === false || busy || pickingFolder}><Plus size={16} /> 新建会话</button>
            <div className="session-filters">
              <button className={!showArchived ? "active" : ""} onClick={() => setShowArchived(false)}>进行中</button>
              <button className={showArchived ? "active" : ""} onClick={() => setShowArchived(true)}>已归档 {workspaceSessions.filter((item) => item.archived).length}</button>
            </div>
            <div className="session-list">
              {displayedSessions.map((item) => (
                <div key={item.id} className={`session-row ${item.id === sessionId ? "active" : ""}`}>
                  <button className="session-item" onClick={() => { sessionIdRef.current = item.id; setSessionId(item.id); }} disabled={busy} title={item.title || "新会话"}>
                    <MessageSquareMore size={14} />
                    <span><b>{item.title || "新会话"}</b><small>{item.turn_count} 轮对话</small></span>
                  </button>
                  <button className="session-action" onClick={() => void setSessionArchived(item, !item.archived)} disabled={busy || !!activeRun} title={item.archived ? "恢复会话" : "归档会话"} aria-label={item.archived ? "恢复会话" : "归档会话"}>{item.archived ? <ArchiveRestore size={14} /> : <Archive size={14} />}</button>
                  <button className="session-action danger" onClick={() => void deleteSession(item)} disabled={busy || !!activeRun} title="删除会话" aria-label="删除会话"><Trash2 size={14} /></button>
                </div>
              ))}
              {displayedSessions.length === 0 && <div className="session-list-empty">{showArchived ? "暂无已归档会话" : "暂无会话"}</div>}
            </div>
          </section>
          <section className="panel-section mini-status">
            <span className={`status-dot ${["executing", "discovering", "briefing"].includes(latestStatus ?? "") ? "ready" : ""}`} />
            <span>Agent {statusLabel(pendingQuestions.length ? "clarifying" : latestStatus)}</span>
          </section>
        </aside>

        <section className="center-panel panel">
          <div className="conversation-header">
            <div><b>{sessions.find((item) => item.id === sessionId)?.title || (sessionId ? "新会话" : activeWorkspace?.name ?? "选择项目")}{activeSessionArchived ? " · 已归档" : ""}</b><span>{session?.active_workspace ?? activeWorkspace?.path ?? "尚未打开项目"}</span></div>
            <div className="conversation-header-actions">
              {activeRun && <button className="cancel-button" onClick={() => void api.cancel(activeRun)}><Square size={12} />停止</button>}
            </div>
          </div>
          <div className="timeline" ref={timelineRef} onScroll={(event) => {
            const element = event.currentTarget;
            stickToBottomRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 120;
          }}>
            {branchEntries.length === 0 && <div className="empty-state"><div className="empty-symbol"><Code2 size={28} /></div><h2>{sessionId ? "开始一个任务" : activeWorkspace ? activeWorkspace.name : "开始使用 TraceForge"}</h2><p>{sessionId ? "描述你想完成的代码工作，Agent 会在这里展示进度。" : activeWorkspace?.available === false ? "项目目录已移动，请在左侧重新定位。" : activeWorkspace ? "点击左侧“新建会话”开始在此项目中工作。" : "点击左侧“打开项目”，选择本地代码目录。"}</p></div>}
            {timelineEntries.map((entry) => {
              const activity = activityByTurnId.get(entry.id);
              const isTurn = entry.type === "user_message" || entry.type === "clarification_answer";
              const liveReasoning = entry.id === activeTurnId ? streamingReasoning : "";
              const hasInlineReasoning = isTurn && (!!activity?.reasoningCount || !!liveReasoning.trim());
              return <Fragment key={entry.id}>
                <EntryCard entry={entry} activity={activity} hasInlineReasoning={hasInlineReasoning} onOpenTools={() => { setSelectedToolTurnId(entry.id); setRightTab("tools"); }} />
                {hasInlineReasoning && <InlineReasoning activity={activity} liveReasoning={liveReasoning} streaming={entry.id === activeTurnId && reasoningStreaming} />}
              </Fragment>;
            })}
            {streaming && (
              <article className="message assistant-message streaming"><div className="avatar agent"><Bot size={16} /></div><div><header>TraceForge <Loader2 size={13} className="spin" /></header><MarkdownMessage content={streaming} /></div></article>
            )}
          </div>

          {pendingApproval && (
            <div className="approval-dock">
              <div><LockKeyhole size={17} /><span><b>需要人工审核 · {pendingApproval.payload.tool_name}</b><small>{pendingApproval.payload.policy.reasons.join("；")}</small>
                <details className="approval-details"><summary>查看完整影响</summary><pre>{JSON.stringify({
                  arguments: pendingApproval.payload.arguments,
                  capabilities: pendingApproval.payload.policy.capabilities,
                  affected_paths: pendingApproval.payload.policy.affected_paths,
                  network: pendingApproval.payload.policy.network,
                }, null, 2)}</pre></details>
              </span></div>
              <div className="approval-actions">
                <button onClick={() => void decideApproval("deny")} className="danger">拒绝</button>
                <button onClick={() => void decideApproval("allow_once")}>允许一次</button>
                <button onClick={() => void decideApproval("allow_session")} className="primary">本会话允许</button>
              </div>
            </div>
          )}

          {pendingQuestions.length > 0 && (
            <div className="clarification-dock" aria-label="待回答的澄清问题">
              {pendingQuestions.map((question, index) => (
                <ClarificationForm
                  key={question.payload.question_id ?? question.id}
                  question={question}
                  index={index}
                  total={pendingQuestions.length}
                  submitting={!!answeringQuestionId || busy || activeSessionArchived || workspaceStatus?.state === "diverged"}
                  onAnswer={answerQuestion}
                />
              ))}
            </div>
          )}

          {workspaceStatus?.state === "diverged" && !busy && <div className="workspace-mismatch" role="alert">
            <div><AlertTriangle size={16} /><span><b>当前文件与此会话的检查点不同</b><small>{workspaceStatus.reason}{workspaceStatus.changes.length ? ` · ${workspaceStatus.changes.length} 个文件有差异` : ""}</small></span></div>
            <div className="workspace-mismatch-actions">
              {workspaceStatus.restorable && session?.active_leaf_id && <button type="button" onClick={() => setPendingRollback({ targetEntryId: session.active_leaf_id!, title: "当前会话" })}>恢复此会话代码</button>}
              <button type="button" onClick={() => void adoptCurrentWorkspace()}>使用当前文件并建立检查点</button>
            </div>
          </div>}
          {branchNotice && <div className="branch-notice"><GitBranch size={14} /><span>{branchNotice}</span><button type="button" onClick={() => setBranchNotice("")} aria-label="关闭分支提示"><X size={13} /></button></div>}
          <form className="composer" onSubmit={sendPrompt}>
            <textarea ref={composerRef} value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder={activeSessionArchived ? "请先恢复会话" : !selectedProjectAvailable ? "先重新定位项目目录" : workspaceStatus?.state === "diverged" ? "先处理工作区代码差异" : sessionId ? (pendingQuestions.length ? "请先回答上方的澄清问题" : "描述你希望 Agent 完成的工作…") : "先创建一个会话"} disabled={!sessionId || busy || pendingQuestions.length > 0 || activeSessionArchived || !selectedProjectAvailable || workspaceStatus?.state === "diverged"} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} />
            <div className="composer-footer"><div className="composer-options"><label className={`permission-picker ${permissionMode === "full_access" ? "full-access" : ""}`} title={permissionMode === "request_approval" ? "有风险的操作先向你请求审批" : permissionMode === "auto_approve" ? "自动批准可审批操作，仍在沙箱中执行；核心禁止项保持拒绝" : "执行工具直接使用宿主机权限，可访问项目外文件和网络"}><ShieldCheck size={14} /><select aria-label="权限模式" value={permissionMode} onChange={(event) => requestPermissionMode(event.target.value as PermissionMode)} disabled={!sessionId || session?.header.id !== sessionId || busy || !!activeRun || permissionBusy || activeSessionArchived}><option value="request_approval">请求审批</option><option value="auto_approve">帮我批准</option><option value="full_access">完全访问</option></select></label><span>Enter 发送 · Shift+Enter 换行</span></div><button type="submit" disabled={!prompt.trim() || busy || !sessionId || pendingQuestions.length > 0 || activeSessionArchived || !selectedProjectAvailable || workspaceStatus?.state === "diverged"}>{busy ? <Loader2 size={16} className="spin" /> : <Send size={16} />}</button></div>
          </form>
        </section>

        <aside className="right-panel panel">
          <nav className="right-tabs" aria-label="工作面板">
            <button className={rightTab === "tools" ? "active" : ""} onClick={() => setRightTab("tools")}><TerminalSquare size={15} />工具</button>
            <button className={rightTab === "files" ? "active" : ""} onClick={() => setRightTab("files")}><Files size={15} />文件</button>
            <button className={rightTab === "diff" ? "active" : ""} onClick={() => setRightTab("diff")}><FileDiff size={15} />变更</button>
            <button className={rightTab === "tree" ? "active" : ""} onClick={() => setRightTab("tree")}><GitBranch size={15} />分支</button>
            <button className={rightTab === "questions" ? "active" : ""} onClick={() => setRightTab("questions")}><HelpCircle size={15} />问题</button>
            <button className={rightTab === "skills" ? "active" : ""} onClick={() => setRightTab("skills")}><BookOpen size={15} />技能</button>
            <button className={rightTab === "stats" ? "active" : ""} onClick={() => setRightTab("stats")}><BarChart3 size={15} />统计</button>
          </nav>
          <div className="right-content" key={rightTab === "tools" ? `tools:${selectedToolGroupId}` : rightTab}>
            {rightTab === "tools" && <ToolPanel groups={toolGroups} selectedId={selectedToolGroupId} onSelect={setSelectedToolTurnId} />}
            {rightTab === "files" && (
              <>
                <div className="side-title"><span>文件</span><small>{files.filter((item) => item.type === "file").length}</small></div>
                <div className="file-list">
                  {files.length === 0 && <p className="side-empty">打开项目后查看文件</p>}
                  {explorer.map((node) => (
                    <FileTreeItem key={node.path} node={node} depth={0} selectedFile={selectedFile} onOpen={(path) => void openFile(path)} />
                  ))}
                </div>
                {selectedFile && <div className="code-view"><header>{selectedFile}</header><pre>{fileContent}</pre></div>}
              </>
            )}
            {rightTab === "diff" && <DiffViewer files={diffFiles} selectedId={selectedDiffId} onSelect={setSelectedDiffId} onExpand={openDiffExpanded} />}
            {rightTab === "tree" && <>
              <div className="side-title"><span>会话树</span><small>{treeMetrics.turns} 轮 · {treeMetrics.forks} 处分叉</small></div>
              <p className="tree-help">“分叉”和“切回”会恢复目标轮次的代码；选中节点可单独恢复代码。跨分支时可选择是否摘要回填。</p>
              <button type="button" className="tree-expand-button" onClick={() => setTreeExpanded(true)} disabled={!tree.length}><Maximize2 size={14} />展开树图</button>
              {tree.length === 0 ? <p className="side-empty">暂无会话记录</p> : <SessionTree nodes={tree} activeTurnId={activeTurnId} activePath={activePathIds} selectedId={selectedTreeNodeId} onSelect={setSelectedTreeNodeId} onFork={(id, title) => requestBranchMove(id, "fork", title)} onResume={(id, title) => requestBranchMove(id, "resume", title)} onRollback={(id, title) => setPendingRollback({ targetEntryId: id, title })} busy={busy || activeSessionArchived || workspaceStatus?.state === "diverged"} forkingId={switchingBranchId} />}
              {checkpoints.length > 0 && <details className="checkpoint-history"><summary>代码检查点 · {checkpoints.length}</summary><div>{[...checkpoints].reverse().slice(0, 30).map((checkpoint) => <button type="button" key={checkpoint.entry_id} disabled={busy || activeSessionArchived} onClick={() => setPendingRollback({ targetEntryId: checkpoint.entry_id, title: `检查点 #${checkpoint.seq}` })}><RotateCcw size={12} /><span>#{checkpoint.seq} · {checkpoint.reason === "before_rollback" ? "回滚前" : checkpoint.reason === "before_branch_switch" ? "切换前" : checkpoint.reason === "before_run" ? "运行前" : checkpoint.reason === "run_finished" ? "运行结束" : "恢复记录"}</span><small>{checkpoint.file_count} 文件</small></button>)}</div></details>}
            </>}
            {rightTab === "skills" && <>
              <div className="side-title"><span>可用技能</span><small>{skills.length}</small></div>
              <p className="tree-help">只向模型展示名称和简介；需要时才读取完整 SKILL.md。点击“使用”可显式调用。</p>
              {skills.length === 0 && <p className="side-empty">当前项目没有发现 Skill。可在 .pi/skills/ 或 .agents/skills/ 下添加。</p>}
              <div className="skill-list">{skills.map((skill) => <article className="skill-item" key={skill.name}>
                <div><strong>{skill.name}</strong><small>{skill.source === "project" ? "项目" : skill.source === "global" ? "全局" : "TraceForge"}</small></div>
                <p>{skill.description}</p>
                <span title={skill.path}>{skill.path}</span>
                <button type="button" disabled={!sessionId || busy || activeSessionArchived} onClick={() => { setPrompt(`/skill:${skill.name} `); composerRef.current?.focus(); }}>使用</button>
                {skill.disable_model_invocation && <em>仅显式调用</em>}
              </article>)}</div>
              {skillWarnings.length > 0 && <details className="skill-warnings"><summary>{skillWarnings.length} 条加载提示</summary>{skillWarnings.map((warning) => <p key={warning}>{warning}</p>)}</details>}
            </>}
            {rightTab === "stats" && <>
              <div className="side-title"><span>当前分支用量</span><small>{runStats.requests} 次模型响应</small></div>
              <div className="stats-grid">
                <div><small>输入 token</small><strong>{runStats.input.toLocaleString()}</strong></div>
                <div><small>输出 token</small><strong>{runStats.output.toLocaleString()}</strong></div>
                <div><small>缓存输入</small><strong>{runStats.cached.toLocaleString()}</strong></div>
                <div><small>推理 token</small><strong>{runStats.reasoning.toLocaleString()}</strong></div>
                <div><small>运行耗时</small><strong>{runStats.elapsed.toFixed(1)} 秒</strong></div>
                <div><small>工具成功 / 总数</small><strong>{runStats.tools - runStats.toolErrors} / {runStats.tools}</strong></div>
              </div>
              <p className="stats-cost">费用估算：{runStats.cost == null ? "未配置模型单价" : `$${runStats.cost.toFixed(5)}`}</p>
              <p className="tree-help">统计来自 API 返回的实际用量；部分兼容服务不返回用量时，这里不会猜测 token 数。费用按环境中配置的每百万 token 单价估算。</p>
            </>}
            {rightTab === "questions" && <><div className="side-title"><span>澄清问题</span></div><div className="question-list">{clarificationQuestions.length === 0 && <p className="side-empty">暂无待确认的问题</p>}{clarificationQuestions.map((question) => {
              const answer = branchEntries.find((entry) => entry.type === "clarification_answer" && entry.payload.question_id === question.payload.question_id);
              return <details key={question.id} open={!answer}><summary><span>{answer ? <Check size={13} /> : <HelpCircle size={13} />}{question.payload.question}</span><small>{answer ? "已回答" : "待回答"}</small></summary><div><code>{question.payload.gap_id}</code>{question.payload.parent_question_id && <small>追问自 {question.payload.parent_question_id}</small>}{answer && <p>{answer.payload.answer}</p>}</div></details>;
            })}</div></>}
          </div>
        </aside>
      </main>
    </div>
  );
}
