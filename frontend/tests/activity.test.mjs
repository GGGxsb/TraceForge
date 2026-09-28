import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const sourcePath = fileURLToPath(new URL("../src/activity.ts", import.meta.url));
const source = readFileSync(sourcePath, "utf8");
const javascript = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const { buildActivityGroups, reasoningSections, reasoningText } = await import(
  `data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`
);

const entry = (type, id, payload = {}) => ({ type, id, payload, seq: Number(id.replace(/\D/g, "")) || 1 });

test("activity groups keep tool and reasoning details out of the conversation turn", () => {
  const first = entry("user_message", "u1", { content: "inspect" });
  const second = entry("user_message", "u2", { content: "continue" });
  const groups = buildActivityGroups([
    first,
    entry("model_reasoning", "r1", { item: { content: [{ type: "reasoning_text", text: "look first" }] } }),
    entry("tool_call", "c1", { call_id: "call-1", name: "read_file" }),
    entry("tool_result", "o1", { call_id: "call-1", output: "ok" }),
    entry("assistant_message", "a1", { content: "done" }),
    second,
    entry("tool_call", "c2", { call_id: "call-2", name: "run_command" }),
  ]);
  assert.deepEqual(groups.map((group) => [group.id, group.toolCount, group.reasoningCount]), [
    ["u1", 1, 1], ["u2", 1, 0],
  ]);
  assert.deepEqual(groups[0].entries.map((item) => item.type), ["model_reasoning", "tool_call", "tool_result"]);
});

test("only readable model reasoning is shown", () => {
  assert.equal(reasoningText(entry("model_reasoning", "r1", {
    item: { content: [{ type: "reasoning_text", text: "step one" }], encrypted_content: "secret" },
  })), "step one");
  assert.equal(reasoningText(entry("model_reasoning", "r2", {
    item: { summary: [{ type: "summary_text", text: "short summary" }] },
  })), "short summary");
  assert.equal(reasoningText(entry("model_reasoning", "r3", {
    item: { encrypted_content: "secret" },
  })), "");
});

test("inline reasoning keeps earlier steps without duplicating the streamed final step", () => {
  const group = {
    entries: [
      entry("model_reasoning", "r1", { item: { content: [{ type: "reasoning_text", text: "first step" }] } }),
      entry("model_reasoning", "r2", { item: { content: [{ type: "reasoning_text", text: "second step" }] } }),
    ],
  };
  assert.deepEqual(reasoningSections(group, "second step"), ["first step", "second step"]);
  assert.deepEqual(reasoningSections(group, "third step"), ["first step", "second step", "third step"]);
  assert.deepEqual(reasoningSections(undefined, "streaming now"), ["streaming now"]);
});
