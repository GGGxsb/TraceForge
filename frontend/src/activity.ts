import type { SessionEntry } from "./types";

export type ActivityGroup = {
  id: string;
  turn: SessionEntry | null;
  entries: SessionEntry[];
  toolCount: number;
  reasoningCount: number;
};

export function reasoningText(entry: SessionEntry): string {
  if (entry.type !== "model_reasoning") return "";
  const item = entry.payload.item;
  if (!item || typeof item !== "object") return "";
  const content = Array.isArray(item.content) ? item.content : [];
  const text = content
    .filter((part: unknown) => !!part && typeof part === "object" && typeof (part as { text?: unknown }).text === "string")
    .map((part: { text: string }) => part.text.trim())
    .filter(Boolean)
    .join("\n\n");
  if (text) return text;
  const summary = Array.isArray(item.summary) ? item.summary : [];
  return summary
    .filter((part: unknown) => !!part && typeof part === "object" && typeof (part as { text?: unknown }).text === "string")
    .map((part: { text: string }) => part.text.trim())
    .filter(Boolean)
    .join("\n\n");
}

export function reasoningSections(group: ActivityGroup | undefined, liveText: string): string[] {
  const sections = (group?.entries ?? [])
    .filter((entry) => entry.type === "model_reasoning")
    .map(reasoningText)
    .filter(Boolean);
  const live = liveText.trim();
  if (live && !sections.some((section) => section === live || section.includes(live))) sections.push(live);
  return sections;
}

export function buildActivityGroups(entries: SessionEntry[]): ActivityGroup[] {
  const groups: ActivityGroup[] = [];
  let current: ActivityGroup | null = null;
  for (const entry of entries) {
    if (entry.type === "user_message" || entry.type === "clarification_answer") {
      current = { id: entry.id, turn: entry, entries: [], toolCount: 0, reasoningCount: 0 };
      groups.push(current);
      continue;
    }
    if (!["tool_call", "tool_result", "model_reasoning", "subagent_spawn", "subagent_update"].includes(entry.type)) continue;
    if (!current) {
      current = { id: `history-${entry.id}`, turn: null, entries: [], toolCount: 0, reasoningCount: 0 };
      groups.push(current);
    }
    current.entries.push(entry);
    if (entry.type === "tool_call") current.toolCount += 1;
    if (entry.type === "model_reasoning" && reasoningText(entry)) current.reasoningCount += 1;
  }
  return groups.filter((group) => group.toolCount > 0 || group.reasoningCount > 0);
}
