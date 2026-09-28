import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const sourcePath = fileURLToPath(new URL("../src/api.ts", import.meta.url));
const source = readFileSync(sourcePath, "utf8");
const javascript = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const { api } = await import(`data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`);

test("model settings send JSON content type alongside the local UI header", async () => {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (_url, init) => {
    assert.equal(init.method, "PUT");
    assert.equal(init.headers.get("Content-Type"), "application/json");
    assert.equal(init.headers.get("X-TraceForge-UI"), "1");
    assert.equal(JSON.parse(init.body).base_url, "https://gateway.example/v1");
    return new Response(JSON.stringify({ configured: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    const result = await api.updateModelSettings({
      api_key: "sk-test-secret",
      model: "test-model",
      base_url: "https://gateway.example/v1",
      brief_model: null,
    });
    assert.equal(result.configured, true);
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("validation errors do not display echoed request bodies or keys", async () => {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(
    JSON.stringify({ detail: [{ msg: "Input should be a valid dictionary", input: "sk-test-secret" }] }),
    { status: 422, headers: { "Content-Type": "application/json" } },
  );
  try {
    await assert.rejects(
      api.updateModelSettings({
        api_key: "sk-test-secret",
        model: "test-model",
        base_url: "https://gateway.example/v1",
        brief_model: null,
      }),
      (error) => error.message === "Input should be a valid dictionary" && !error.message.includes("sk-test-secret"),
    );
  } finally {
    globalThis.fetch = previousFetch;
  }
});
