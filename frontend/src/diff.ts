export type ChangeSection = "unstaged" | "staged" | "untracked" | "workspace";
export type ChangeKind = "added" | "deleted" | "modified" | "renamed" | "binary";
export type DiffLineKind = "add" | "remove" | "context" | "meta";

export interface DiffLine {
  kind: DiffLineKind;
  text: string;
  oldLine: number | null;
  newLine: number | null;
}

export interface DiffHunk {
  header: string;
  lines: DiffLine[];
}

export interface DiffFile {
  id: string;
  path: string;
  oldPath: string;
  section: ChangeSection;
  kind: ChangeKind;
  additions: number;
  deletions: number;
  notes: string[];
  hunks: DiffHunk[];
}

function cleanPath(value: string): string {
  const path = value.trim().replace(/^"|"$/g, "");
  return path.replace(/^[ab]\//, "").replace(/\\/g, "/");
}

export function parseDiff(source: string): DiffFile[] {
  const files: DiffFile[] = [];
  let section: ChangeSection = "workspace";
  let current: DiffFile | null = null;
  let hunk: DiffHunk | null = null;
  let oldLine = 0;
  let newLine = 0;
  let oldHeaderSeen = false;

  function startFile(path: string, fromGit = false): DiffFile {
    const file: DiffFile = {
      id: `${section}:${files.length}`,
      path: cleanPath(path),
      oldPath: "",
      section,
      kind: section === "untracked" ? "added" : "modified",
      additions: 0,
      deletions: 0,
      notes: [],
      hunks: [],
    };
    files.push(file);
    hunk = null;
    oldHeaderSeen = fromGit ? false : true;
    return file;
  }

  for (const line of source.split(/\r?\n/)) {
    if (line === "UNSTAGED" || line === "STAGED" || line === "UNTRACKED") {
      section = line.toLowerCase() as ChangeSection;
      current = null;
      hunk = null;
      continue;
    }
    if (line.startsWith("diff --git ")) {
      const match = /^diff --git (?:"?a\/(.*?)"?) (?:"?b\/(.*?)"?)$/.exec(line);
      current = startFile(match?.[2] ?? line.slice(11), true);
      continue;
    }
    if (line.startsWith("--- ")) {
      if (!current || oldHeaderSeen) current = startFile(line.slice(4));
      if (!current) continue;
      current.oldPath = cleanPath(line.slice(4));
      oldHeaderSeen = true;
      if (line.slice(4).trim() === "/dev/null") current.kind = "added";
      continue;
    }
    if (!current) continue;
    if (line.startsWith("+++ ")) {
      const path = line.slice(4).trim();
      if (path === "/dev/null") current.kind = "deleted";
      else current.path = cleanPath(path);
      continue;
    }
    if (line.startsWith("@@ ")) {
      const match = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
      if (match) {
        oldLine = Number(match[1]);
        newLine = Number(match[2]);
        hunk = { header: line, lines: [] };
        current.hunks.push(hunk);
      } else {
        current.notes.push(line);
        hunk = null;
      }
      continue;
    }
    if (line.startsWith("new file mode ")) current.kind = "added";
    if (line.startsWith("deleted file mode ")) current.kind = "deleted";
    if (line.startsWith("rename from ")) {
      current.kind = "renamed";
      current.oldPath = cleanPath(line.slice("rename from ".length));
    }
    if (line.startsWith("rename to ")) {
      current.kind = "renamed";
      current.path = cleanPath(line.slice("rename to ".length));
    }
    if (line.startsWith("Binary files ") || line.startsWith("GIT binary patch")) current.kind = "binary";

    if (hunk && (line.startsWith("+") || line.startsWith("-") || line.startsWith(" "))) {
      if (line.startsWith("+")) {
        hunk.lines.push({ kind: "add", text: line.slice(1), oldLine: null, newLine: newLine++ });
        current.additions++;
      } else if (line.startsWith("-")) {
        hunk.lines.push({ kind: "remove", text: line.slice(1), oldLine: oldLine++, newLine: null });
        current.deletions++;
      } else {
        hunk.lines.push({ kind: "context", text: line.slice(1), oldLine: oldLine++, newLine: newLine++ });
      }
      continue;
    }
    if (hunk && line.startsWith("\\")) {
      hunk.lines.push({ kind: "meta", text: line, oldLine: null, newLine: null });
      continue;
    }
    if (line && !line.startsWith("index ") && !line.startsWith("similarity index ")) current.notes.push(line);
  }
  const seen = new Map<string, number>();
  return files.filter((file) => file.path || file.hunks.length || file.notes.length).map((file) => {
    const key = `${file.section}:${file.path}`;
    const instance = seen.get(key) ?? 0;
    seen.set(key, instance + 1);
    file.id = `${key}:${instance}`;
    return file;
  });
}
