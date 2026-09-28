import { FileDiff, Maximize2, X } from "lucide-react";
import { memo } from "react";
import type { DiffFile } from "./diff";

const sectionLabels = {
  unstaged: "未暂存",
  staged: "已暂存",
  untracked: "未跟踪",
  workspace: "工作区",
};

const kindLabels = {
  added: "新增",
  deleted: "删除",
  modified: "修改",
  renamed: "重命名",
  binary: "二进制",
};

export const DiffViewer = memo(function DiffViewer({ files, selectedId, onSelect, expanded = false, onExpand, onClose }: {
  files: DiffFile[];
  selectedId: string;
  onSelect: (id: string) => void;
  expanded?: boolean;
  onExpand?: () => void;
  onClose?: () => void;
}) {
  const selected = files.find((file) => file.id === selectedId) ?? files[0];
  const additions = files.reduce((total, file) => total + file.additions, 0);
  const deletions = files.reduce((total, file) => total + file.deletions, 0);

  return (
    <div className={`diff-viewer ${expanded ? "expanded" : ""}`}>
      <div className="diff-heading">
        <div><b>工作区变更</b><span>{files.length} 个文件 <em className="add-count">+{additions}</em> <em className="remove-count">−{deletions}</em></span></div>
        {expanded ? <button type="button" onClick={onClose} title="关闭宽屏视图" aria-label="关闭宽屏视图"><X size={16} /></button>
          : files.length > 0 && <button type="button" onClick={onExpand} title="宽屏查看变更" aria-label="宽屏查看变更"><Maximize2 size={15} /></button>}
      </div>
      {files.length === 0 ? <p className="side-empty">暂无文件变更</p> : (
        <div className="diff-layout">
          <div className="diff-file-list" aria-label="变更文件">
            {files.map((file) => (
              <button key={file.id} type="button" className={`diff-file ${selected?.id === file.id ? "selected" : ""}`} onClick={() => onSelect(file.id)} title={file.path}>
                <FileDiff size={14} />
                <span className="diff-file-name"><b>{file.path.split("/").at(-1)}</b><small>{file.path.includes("/") ? file.path.slice(0, file.path.lastIndexOf("/")) : sectionLabels[file.section]}</small></span>
                <span className="diff-file-stats"><span className="add-count">+{file.additions}</span><span className="remove-count">−{file.deletions}</span></span>
              </button>
            ))}
          </div>
          {selected && <div className="diff-detail" key={selected.id}>
            <div className="diff-detail-heading">
              <span className="diff-path" title={selected.path}>{selected.path}</span>
              <span className="diff-tags"><span>{sectionLabels[selected.section]}</span><span>{kindLabels[selected.kind]}</span></span>
            </div>
            {selected.kind === "renamed" && selected.oldPath && selected.oldPath !== selected.path && <p className="diff-note">原路径：{selected.oldPath}</p>}
            {selected.notes.filter((note) => !note.startsWith("diff --git ")).map((note, index) => <p className="diff-note" key={`${index}:${note}`}>{note}</p>)}
            {selected.hunks.length === 0 && <p className="diff-no-hunks">此文件没有可展示的文本代码块。</p>}
            <div className="diff-hunks">
              {selected.hunks.map((hunk, hunkIndex) => (
                <section className="diff-hunk" key={`${hunkIndex}:${hunk.header}`}>
                  <div className="diff-hunk-heading" title={hunk.header}>{hunk.header}</div>
                  <div className="diff-lines">
                    {hunk.lines.map((line, lineIndex) => (
                      <div className={`diff-line ${line.kind}`} key={lineIndex}>
                        <span className="diff-number">{line.oldLine ?? ""}</span>
                        <span className="diff-number">{line.newLine ?? ""}</span>
                        <span className="diff-sign" aria-hidden="true">{line.kind === "add" ? "+" : line.kind === "remove" ? "−" : ""}</span>
                        <code>{line.text || " "}</code>
                      </div>
                    ))}
                  </div>
                </section>
              ))}
            </div>
          </div>}
        </div>
      )}
    </div>
  );
});
