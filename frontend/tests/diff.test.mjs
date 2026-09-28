import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const source = readFileSync(fileURLToPath(new URL("../src/diff.ts", import.meta.url)), "utf8");
const javascript = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const { parseDiff } = await import(`data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}`);

test("Git diff separates staging areas and labels changed lines", () => {
  const files = parseDiff(`UNSTAGED
diff --git a/src/app.py b/src/app.py
index 123..456 100644
--- a/src/app.py
+++ b/src/app.py
@@ -3,2 +3,3 @@ def run():
 keep()
-old()
+new()
+again()
STAGED
diff --git a/src/new.py b/src/new.py
new file mode 100644
--- /dev/null
+++ b/src/new.py
@@ -0,0 +1 @@
+hello()
UNTRACKED
--- /dev/null
+++ b/docs/notes.md
@@ -0,0 +1 @@
+# Notes
`);
  assert.equal(files.length, 3);
  assert.deepEqual(files.map((file) => [file.section, file.path, file.kind, file.additions, file.deletions]), [
    ["unstaged", "src/app.py", "modified", 2, 1],
    ["staged", "src/new.py", "added", 1, 0],
    ["untracked", "docs/notes.md", "added", 1, 0],
  ]);
  assert.deepEqual(files[0].hunks[0].lines.map((line) => [line.kind, line.oldLine, line.newLine]), [
    ["context", 3, 3], ["remove", 4, null], ["add", null, 4], ["add", null, 5],
  ]);
});

test("directory snapshots keep separate files and deleted status", () => {
  const files = parseDiff(`--- a/src\\first.txt
+++ b/src\\first.txt
@@ -1 +1 @@
-before
+after
--- a/second.txt
+++ /dev/null
@@ -1 +0,0 @@
-gone
\\ No newline at end of file
`);
  assert.equal(files.length, 2);
  assert.equal(files[0].path, "src/first.txt");
  assert.equal(files[1].path, "second.txt");
  assert.equal(files[1].kind, "deleted");
  assert.equal(files[1].hunks[0].lines[1].kind, "meta");
});
