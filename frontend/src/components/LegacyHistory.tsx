import { useEffect, useRef, useState } from "react";
import { api } from "../api";

type Entry = { id: string; kind: string; label: string; created_at: string; payload: { summary?: string; content?: string; body?: Record<string, unknown>; started_at?: string } };
type Result = { items: Entry[]; next_offset: number | null; notice: string };
function display(entry: Entry) {
  const p = entry.payload;
  if (p.summary || p.content) return p.summary || p.content;
  return Object.entries(p.body || {}).flatMap(([key, value]) =>
    ["progress", "decisions", "blockers", "next_steps", "observations"].includes(key) && Array.isArray(value)
      ? value.map((item: { text?: string }) => item.text || "") : []).join("\n");
}
export default function LegacyHistory({ episodeId }: { episodeId?: string }) {
  const [opened, setOpened] = useState(false);
  const [data, setData] = useState<Result | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    generation.current++; setData(null); setOpened(false); setBusy(false); setError("");
    return () => { generation.current++; };
  }, [episodeId]);
  const load = async (append = false) => {
    const request = ++generation.current;
    setBusy(true); setError("");
    try {
      const result = await api<Result>(`/history/legacy?offset=${append ? data?.next_offset || 0 : 0}${episodeId ? `&episode_id=${episodeId}` : ""}`);
      if (request !== generation.current) return;
      setData(previous => append && previous ? { ...result, items: [...previous.items, ...result.items] } : result);
    } catch (e) { if (request === generation.current) setError((e as Error).message); }
    finally { if (request === generation.current) setBusy(false); }
  };
  // Clear any displayed copy after explicit deletion or expiry on another screen.
  useEffect(() => {
    if (!opened) return;
    const timer = setInterval(() => { if (!document.hidden) void load(); }, 15000);
    return () => clearInterval(timer);
  }, [opened, episodeId]);
  return <section className="legacy-history">
    <button className="text-button" onClick={() => { if (!opened) void load(); setOpened(!opened); }}>{opened ? "收起隔离历史" : "查看隔离历史（仅供本人核对）"}</button>
    {opened && <div>
      <p className="muted">{data?.notice || "旧自动内容尚未核实，不参与模型回答或自动整理。"}</p>
      {error && <p role="alert">{error}</p>}
      {data?.items.map(entry => <article key={entry.id}>
        <strong>{entry.label}</strong><small> · {new Date(entry.payload.started_at || entry.created_at).toLocaleString("zh-CN")}</small>
        <pre>{display(entry) || "没有保留的展示正文。"}</pre>
      </article>)}
      {!busy && data?.items.length === 0 && <p>暂无保留的隔离历史。</p>}
      {busy && <p role="status">正在读取历史…</p>}
      {data?.next_offset != null && <button disabled={busy} onClick={() => void load(true)}>加载更早历史</button>}
    </div>}
  </section>;
}
