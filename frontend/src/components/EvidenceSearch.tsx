import { useEffect, useState } from "react";
import { api } from "../api";
import type { Evidence, Project } from "../api";
export default function EvidenceSearch({
  projects,
  onSource,
}: {
  projects: Project[];
  onSource: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [project, setProject] = useState("");
  const [app, setApp] = useState("");
  const [day, setDay] = useState("");
  const [items, setItems] = useState<Evidence[] | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [cursor, setCursor] = useState<number | null>(null);
  useEffect(() => {
    const changed = () => {
      setItems(null);
      setCursor(null);
    };
    window.addEventListener("memory-changed", changed);
    return () => window.removeEventListener("memory-changed", changed);
  }, []);
  async function search(offset = 0) {
    setBusy(true);
    setNotice("");
    try {
      const params = new URLSearchParams({ q: query, cursor: String(offset) });
      if (project) params.set("project_id", project);
      if (app) params.set("app", app);
      if (day) {
        const start = new Date(day + "T00:00:00");
        const end = new Date(start);
        end.setDate(end.getDate() + 1);
        params.set("from", start.toISOString());
        params.set("to", end.toISOString());
      }
      const result = await api<{
        items: Evidence[];
        warning: string | null;
        next_cursor: number | null;
      }>("/memory/evidence/search?" + params);
      setItems((old) =>
        offset ? [...(old || []), ...result.items] : result.items,
      );
      setCursor(result.next_cursor);
      setNotice(result.warning || "");
    } catch (e) {
      setNotice((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <details className="evidence-search">
      <summary>按原文、项目和时间查找证据</summary>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void search();
        }}
      >
        <input
          aria-label="搜索证据"
          placeholder="输入关键词或问题"
          value={query}
          maxLength={2000}
          onChange={(e) => {
            setQuery(e.target.value);
            setCursor(null);
          }}
        />
        <select
          aria-label="证据项目"
          value={project}
          onChange={(e) => {
            setProject(e.target.value);
            setCursor(null);
          }}
        >
          <option value="">全部项目</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <input
          aria-label="证据应用"
          placeholder="应用名称（可选）"
          value={app}
          onChange={(e) => {
            setApp(e.target.value);
            setCursor(null);
          }}
        />
        <input
          aria-label="证据日期"
          type="date"
          value={day}
          onChange={(e) => {
            setDay(e.target.value);
            setCursor(null);
          }}
        />
        <button disabled={busy || !query.trim()}>
          {busy ? "检索中…" : "查找证据"}
        </button>
      </form>
      {notice && (
        <p role="status" className="muted">
          {notice}
        </p>
      )}
      {items?.map((item) => (
        <article key={item.id}>
          <small>
            {item.kind === "raw"
              ? "原文片段"
              : item.kind === "user_confirmed"
                ? "确认的记忆"
                : "摘要"}{" "}
            · {item.captured_at ? new Date(item.captured_at).toLocaleString("zh-CN") : "未记录时间"}
          </small>
          <p>{item.content}</p>
          {item.episode_id && (
            <button
              className="text-button"
              onClick={() => onSource(item.episode_id!)}
            >
              查看来源与相邻记录
            </button>
          )}
        </article>
      ))}
      {items && !items.length && (
        <p className="muted">没有找到符合范围的有效证据。</p>
      )}
      {cursor !== null && (
        <button disabled={busy} onClick={() => void search(cursor)}>
          更多匹配
        </button>
      )}
    </details>
  );
}
