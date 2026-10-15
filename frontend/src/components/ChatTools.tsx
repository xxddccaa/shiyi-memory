import { useEffect, useState } from "react";
import { Blocks, Paperclip, Search, X } from "lucide-react";
import { api } from "../api";
import type { Evidence, ObjectContext, WorkspaceObject } from "../api";
import type { AgentChatState } from "../hooks/useAgentChat";
import ChatSheet from "./ChatSheet";

const kinds = { todo: "待办", project: "项目", memory: "长期记忆", episode: "工作记录", session: "工作段", review: "每日回顾" };
interface Page { items: WorkspaceObject[]; next_cursor: string | null; total: number }
interface Capabilities { operations: Record<string, string>; tools: {name: string; description: string}[]; services: Record<string, {available: boolean; error?: string}> }

export default function ChatTools({chat, compact = false}: {chat: AgentChatState; compact?: boolean}) {
  const [sheet, setSheet] = useState<"objects" | "capabilities" | null>(null);
  const [kind, setKind] = useState<ObjectContext["kind"]>("todo");
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<Page | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [selectedText, setSelectedText] = useState("");
  useEffect(() => {
    let closed = false;
    const selection = chat.objectContext;
    setSelectedText("");
    if (selection) void api<Evidence>(`/workspace/objects/${selection.kind}/${selection.id}`).then((v) => {
      if (closed) return;
      if (v.revision !== selection.revision) { chat.setError("选中对象已更新，请重新选择。"); chat.setObjectContext(null); }
      else setSelectedText(v.content);
    }).catch((e) => { if (!closed) { chat.setError(e.message); chat.setObjectContext(null); } });
    return () => { closed = true; };
  }, [chat.objectContext]);
  useEffect(() => {
    if (!sheet) return;
    const controller = new AbortController();
    setError(""); setLoading(true);
    const timer = setTimeout(() => {
      if (sheet === "capabilities") {
        void api<Capabilities>("/agent/capabilities", {signal: controller.signal}).then(setCapabilities)
          .catch((e) => { if (!controller.signal.aborted) setError(e.message); })
          .finally(() => { if (!controller.signal.aborted) setLoading(false); });
      } else {
        const params = new URLSearchParams({kind, q: query, limit: "15"});
        if (cursor) params.set("cursor", cursor);
        void api<Page>(`/workspace/objects?${params}`, {signal: controller.signal}).then(setResult)
          .catch((e) => { if (!controller.signal.aborted) setError(e.message); })
          .finally(() => { if (!controller.signal.aborted) setLoading(false); });
      }
    }, 180);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [sheet, kind, query, cursor]);
  return <>
    <div className={`chat-tools ${compact ? "compact" : ""}`}>
      <button className="text-button" disabled={chat.running || chat.busy} onClick={() => { setCursor(null); setResult(null); setSheet("objects"); }}><Paperclip size={14} />选择对象</button>
      <button className="text-button" onClick={() => setSheet("capabilities")}><Blocks size={14} />我能做什么</button>
    </div>
    {chat.objectContext && <div className="chat-selection" role="status">
      <span><small>{kinds[chat.objectContext.kind]} · 本次消息附带</small>{selectedText || "正在核对对象…"}</span>
      <button className="icon-button" aria-label="取消选中对象" onClick={() => chat.setObjectContext(null)}><X size={14} /></button>
    </div>}
    {sheet === "objects" && <ChatSheet title="选择要处理的对象" onClose={() => setSheet(null)}>
      <p className="muted">选中后可以说“删除这个”或描述修改要求，执行前会请你确认。</p>
      <div className="object-search">
        <select aria-label="对象类型" value={kind} onChange={(e) => { setKind(e.target.value as ObjectContext["kind"]); setCursor(null); setResult(null); }}>
          {Object.entries(kinds).map(([k, name]) => <option key={k} value={k}>{name}</option>)}
        </select>
        <label><Search size={15} /><input aria-label="搜索工作对象" placeholder="输入关键词查找" maxLength={300} value={query} onChange={(e) => {setQuery(e.target.value); setCursor(null);}} /></label>
      </div>
      {loading && <p role="status">正在查找…</p>}
      {!loading && !result?.items.length && !error && <p className="muted">没有匹配的有效对象，可换个关键词。</p>}
      {!loading && result?.items.map((item) => <button className="workspace-object" key={item.id} onClick={() => {
        chat.setObjectContext({id: item.id, kind: item.kind, revision: item.revision}); setSelectedText(item.text); setSheet(null);
      }}><span>{item.text}</span><small>{kinds[item.kind]}{item.done !== null ? item.done ? " · 已完成" : " · 未完成" : ""}</small></button>)}
      {result?.next_cursor && <button disabled={loading} onClick={() => setCursor(result.next_cursor)}>下一页</button>}
      {cursor && <button onClick={() => setCursor(null)}>回到第一页</button>}
      {error && <p role="alert">{error}</p>}
    </ChatSheet>}
    {sheet === "capabilities" && <ChatSheet title="我能做什么" onClose={() => setSheet(null)}>
      <p>可以闲聊、解释知识、查找工作记忆和总结某天的工作。没有检索到记忆也能正常交流。</p>
      <h3>应用内操作</h3><p className="muted">以下修改都会先生成卡片，由你确认后执行。</p>
      <div className="capability-tags">{Object.values(capabilities?.operations || {}).map((label) => <span key={label}>{label}</span>)}</div>
      <h3>联网能力</h3>
      {capabilities && !capabilities.tools.some((t) => /^(mcp_|weather__)/.test(t.name)) && <p className="muted">尚未启用可用的联网工具。可在网页“偏好与状态 → 联网与工具”中启用搜索、天气或配置只读服务。</p>}
      <ul className="capability-list">{capabilities?.tools.filter((t) => /^(mcp_|weather__)/.test(t.name)).map((t) => <li key={t.name}>{t.description.split(/[。；]/)[0]}</li>)}</ul>
      {Object.entries(capabilities?.services || {}).filter(([, s]) => !s.available).map(([id]) => <p className="muted" key={id}>{id} 暂时连接失败，本地记忆仍可使用。</p>)}
      {loading && <p role="status">正在检查工具…</p>}{error && <p role="alert">{error}</p>}
    </ChatSheet>}
  </>;
}
