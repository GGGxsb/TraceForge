import { GitBranch, Loader2, MessageSquareMore, Navigation2, RotateCcw } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent } from "react";
import type { TreeNode } from "./types";

const NODE_WIDTH = 226;
const NODE_HEIGHT = 126;
const COLUMN_STEP = 290;
const ROW_STEP = 158;

type PlacedNode = { node: TreeNode; x: number; y: number; ordinal: number };
type TreeEdge = { parent: PlacedNode; child: PlacedNode };

function excerpt(value: string, limit: number): string {
  const clean = value.replace(/\s+/g, " ").trim();
  return clean.length > limit ? `${clean.slice(0, limit)}…` : clean;
}

function label(node: TreeNode): string {
  const value = String(node.entry.payload.content ?? node.entry.payload.answer ?? "");
  if (node.entry.type === "clarification_answer") return `补充：${excerpt(value, 70) || "澄清回答"}`;
  if (node.entry.type === "user_message") return excerpt(value, 70) || "新对话";
  return node.orphaned ? "恢复记录" : "会话记录";
}

export function countTurnTree(nodes: TreeNode[]): { turns: number; forks: number } {
  return nodes.reduce((count, node) => {
    const descendants = countTurnTree(node.children);
    return {
      turns: count.turns + 1 + descendants.turns,
      forks: count.forks + Number(node.children.length > 1) + descendants.forks,
    };
  }, { turns: 0, forks: 0 });
}

function layoutTree(roots: TreeNode[]) {
  const items: PlacedNode[] = [];
  const edgeIds: { parentId: string; childId: string }[] = [];
  let leaf = 0;
  let maxDepth = 0;

  function place(node: TreeNode, depth: number): number {
    maxDepth = Math.max(maxDepth, depth);
    const childYs = node.children.map((child) => {
      edgeIds.push({ parentId: node.entry.id, childId: child.entry.id });
      return place(child, depth + 1);
    });
    const y = childYs.length ? (childYs[0] + childYs[childYs.length - 1]) / 2 : 24 + leaf++ * ROW_STEP;
    items.push({ node, x: 24 + depth * COLUMN_STEP, y, ordinal: 0 });
    return y;
  }

  roots.forEach((root) => place(root, 0));
  const byId = new Map(items.map((item) => [item.node.entry.id, item]));
  const edges: TreeEdge[] = edgeIds.flatMap(({ parentId, childId }) => {
    const parent = byId.get(parentId);
    const child = byId.get(childId);
    return parent && child ? [{ parent, child }] : [];
  });
  [...items].sort((a, b) => a.node.entry.seq - b.node.entry.seq).forEach((item, index) => { item.ordinal = index + 1; });
  return {
    items,
    edges,
    width: 48 + maxDepth * COLUMN_STEP + NODE_WIDTH,
    height: 48 + Math.max(1, leaf) * ROW_STEP - (ROW_STEP - NODE_HEIGHT),
  };
}

function edgePath(edge: TreeEdge): string {
  const fromX = edge.parent.x + NODE_WIDTH;
  const fromY = edge.parent.y + NODE_HEIGHT / 2;
  const toX = edge.child.x;
  const toY = edge.child.y + NODE_HEIGHT / 2;
  const bend = Math.max(28, (toX - fromX) / 2);
  return `M ${fromX} ${fromY} C ${fromX + bend} ${fromY}, ${toX - bend} ${toY}, ${toX} ${toY}`;
}

type Props = {
  nodes: TreeNode[];
  activeTurnId: string | null;
  activePath: Set<string>;
  selectedId: string;
  onSelect: (id: string) => void;
  onFork: (targetEntryId: string, title: string) => void;
  onResume: (targetEntryId: string, title: string) => void;
  onRollback: (targetEntryId: string, title: string) => void;
  busy: boolean;
  forkingId: string;
  expanded?: boolean;
};

export function SessionTree({ nodes, activeTurnId, activePath, selectedId, onSelect, onFork, onResume, onRollback, busy, forkingId, expanded = false }: Props) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ pointerId: number; x: number; y: number; left: number; top: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const layout = useMemo(() => layoutTree(nodes), [nodes]);
  const selected = layout.items.find((item) => item.node.entry.id === selectedId)
    ?? layout.items.find((item) => item.node.entry.id === activeTurnId)
    ?? layout.items[0];
  const selectedReturnTips = (selected?.node.branch_tips ?? []).filter((tip) => !tip.active);

  function centerCurrent() {
    const viewport = viewportRef.current;
    const current = layout.items.find((item) => item.node.entry.id === activeTurnId);
    if (!viewport || !current) return;
    viewport.scrollTo({
      left: Math.max(0, current.x + NODE_WIDTH / 2 - viewport.clientWidth / 2),
      top: Math.max(0, current.y + NODE_HEIGHT / 2 - viewport.clientHeight / 2),
      behavior: "smooth",
    });
  }

  useEffect(() => { centerCurrent(); }, [activeTurnId, expanded, layout]);

  function startDrag(event: PointerEvent<HTMLDivElement>) {
    if (event.pointerType !== "mouse" || event.button !== 0 || (event.target as Element).closest(".session-tree-node")) return;
    const viewport = viewportRef.current;
    if (!viewport) return;
    dragRef.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, left: viewport.scrollLeft, top: viewport.scrollTop };
    viewport.setPointerCapture(event.pointerId);
    setDragging(true);
  }

  function moveDrag(event: PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    const viewport = viewportRef.current;
    if (!drag || !viewport || drag.pointerId !== event.pointerId) return;
    viewport.scrollLeft = drag.left - (event.clientX - drag.x);
    viewport.scrollTop = drag.top - (event.clientY - drag.y);
  }

  function stopDrag(event: PointerEvent<HTMLDivElement>) {
    if (dragRef.current?.pointerId !== event.pointerId) return;
    dragRef.current = null;
    setDragging(false);
  }

  return <div className={`session-tree ${expanded ? "expanded" : "compact"}`}>
    <div className="session-tree-tools"><span>拖动空白处浏览分支</span><button type="button" onClick={centerCurrent} disabled={!activeTurnId}><Navigation2 size={13} />定位当前</button></div>
    <div ref={viewportRef} className={`session-tree-viewport ${dragging ? "dragging" : ""}`} onPointerDown={startDrag} onPointerMove={moveDrag} onPointerUp={stopDrag} onPointerCancel={stopDrag}>
      <div className="session-tree-canvas" style={{ width: layout.width, height: layout.height }}>
        <svg className="session-tree-links" width={layout.width} height={layout.height} aria-hidden="true">
          {layout.edges.map((edge) => <path key={`${edge.parent.node.entry.id}:${edge.child.node.entry.id}`} d={edgePath(edge)} className={activePath.has(edge.parent.node.entry.id) && activePath.has(edge.child.node.entry.id) ? "on-path" : ""} />)}
        </svg>
        {layout.items.map(({ node, x, y, ordinal }) => {
          const active = node.entry.id === activeTurnId;
          const selectedNode = node.entry.id === selected?.node.entry.id;
          const preview = excerpt(String(node.question || node.assistant_preview), 80);
          const latestReturnTip = (node.branch_tips ?? []).find((tip) => !tip.active);
          return <article key={node.entry.id} className={`session-tree-node ${active ? "active" : ""} ${activePath.has(node.entry.id) ? "on-path" : ""} ${selectedNode ? "selected" : ""}`} style={{ left: x, top: y }}>
            <button type="button" className="session-tree-node-main" aria-pressed={selectedNode} onClick={() => onSelect(node.entry.id)} title={label(node)}>
              <span className="session-tree-node-top"><span><MessageSquareMore size={13} />第 {ordinal} 轮</span><em>{active ? "当前" : node.children.length > 1 ? `${node.children.length} 分支` : node.status ? "已记录" : "进行中"}</em></span>
              <strong>{label(node)}</strong>
              <small>{preview ? `${node.question ? "澄清" : "Agent"}：${preview}` : "点击查看这一轮"}</small>
            </button>
            <div className="session-tree-node-footer"><span>{node.tool_count} 工具 · {node.event_count} 记录</span><div className="session-tree-node-actions">{latestReturnTip && <button type="button" onClick={() => onResume(latestReturnTip.entry_id, label(node))} disabled={busy} title={`切换回第 ${ordinal} 轮所在的已有分支`}>{forkingId === latestReturnTip.entry_id ? <Loader2 size={12} className="spin" /> : <Navigation2 size={12} />}切回</button>}<button type="button" onClick={() => onFork(node.target_entry_id, label(node))} disabled={busy} title={`从第 ${ordinal} 轮结束处创建新分支`}>{forkingId === node.target_entry_id ? <Loader2 size={12} className="spin" /> : <GitBranch size={12} />}分叉</button></div></div>
          </article>;
        })}
      </div>
    </div>
    {selected && <div className="session-tree-selection"><span>已选 · 第 {selected.ordinal} 轮</span><strong>{label(selected.node)}</strong><small>{selected.node.children.length > 1 ? `${selected.node.children.length} 条分支` : `${selected.node.children.length} 条后续对话`} · {selected.node.event_count} 条原始记录</small>{selected.node.checkpoint_id && <button type="button" className="checkpoint-rollback-button" disabled={busy} onClick={() => onRollback(selected.node.target_entry_id, label(selected.node))}><RotateCcw size={13} />恢复此轮代码</button>}{selectedReturnTips.length > 1 && <div className="session-tree-resume-list"><span>可返回的位置</span>{selectedReturnTips.map((tip) => <button type="button" key={tip.entry_id} disabled={busy} onClick={() => onResume(tip.entry_id, label(selected.node))}>记录 #{tip.seq} · 切换回此分支</button>)}</div>}</div>}
  </div>;
}
