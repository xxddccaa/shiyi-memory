import { ReminderList, openReminder } from "./ScheduledTodos";
import CodingControls from "./CodingControls";
import { useEffect, useRef, useState } from "react";
import { ArrowDown, Bell, BookOpen, Cat, ChevronRight, Ellipsis, MessageSquare, Plus, Trash2 } from "lucide-react";
import { api } from "../api";
import type { Reminder } from "../api";
import useAgentChat from "../hooks/useAgentChat";
import useChatScroll from "../hooks/useChatScroll";
import { ChatComposer, ChatMessages, MemoryControls, MemorySource, RunFeedback } from "./ChatParts";
import ChatSheet from "./ChatSheet";
import ChatTools from "./ChatTools";
import OperationCards from "./OperationCards";

export default function PetChat() {
  const chat = useAgentChat();
  const scroll = useChatScroll(chat.selected, chat.messages);
  const [drawer, setDrawer] = useState<"menu" | "memories" | "reminders" | null>(new URLSearchParams(location.search).get("reminders") === "all" ? "reminders" : null);
  const [source, setSource] = useState<{ text: string; title: string } | null>(null);
  const sourceSequence = useRef(0);
  useEffect(() => {
    const show = () => setDrawer("reminders");
    window.addEventListener("show-reminders", show);
    return () => window.removeEventListener("show-reminders", show);
  }, []);
  const [reminders, setReminders] = useState<Reminder[]>([]);
  useEffect(() => {
    let closed = false;
    const load = () => void api<{ reminders_enabled: boolean }>("/client/state")
      .then(async (s) => {
        const items = s.reminders_enabled ? await api<Reminder[]>("/reminders?pet=true") : [];
        if (!closed) setReminders(items);
      }).catch(() => {});
    load();
    window.addEventListener("client-change", load);
    const clear = () => { sourceSequence.current++; setSource(null); };
    window.addEventListener("memory-changed", clear);
    return () => {
      closed = true;
      sourceSequence.current++;
      window.removeEventListener("client-change", load);
      window.removeEventListener("memory-changed", clear);
    };
  }, []);
  async function openSource(id: string) {
    const sequence = ++sourceSequence.current;
    try {
      const value = await api<{ episode: { summary: string }; activities: { text: string }[] }>(`/episodes/${id}/sources`);
      if (sequence !== sourceSequence.current) return;
      setSource({ title: "工作记录来源", text: value.episode.summary || value.activities?.map((a) => a.text).join("\n") || "原文已到期" });
    } catch (e) { chat.setError((e as Error).message); }
  }
  const title = chat.conversations.find((c) => c.id === chat.selected)?.title || "和拾忆聊聊";
  const pendingMemories = chat.candidates.filter((c) => c.status === "pending").length;
  return <main className="pet-chat">
    <header className="pet-header">
      <span className="pet-avatar" aria-hidden="true"><Cat size={22} strokeWidth={1.6} /></span>
      <div className="pet-heading"><h1 title={title}>{title}</h1><p><i className={chat.running ? "is-busy" : ""} />{chat.running ? chat.progress || "准备回答" : "拾忆 · 和网页同步"}</p></div>
      <button className="icon-button" aria-label="新对话" title="新对话" onClick={() => chat.setSelected("")}><Plus size={20} /></button>
      <button className="icon-button pet-menu-button" aria-label="聊天菜单" title="对话与记忆" onClick={() => setDrawer("menu")}><Ellipsis size={21} />{(pendingMemories > 0 || reminders.length > 0) && <i />}</button>
    </header>
    <div className="pet-transcript" ref={scroll.ref} onScroll={scroll.onScroll} aria-label="聊天记录" tabIndex={0}>
      <div ref={scroll.content} className="pet-transcript-content">
        {!chat.messages.length && !chat.error && <div className="pet-welcome">
          <span className="pet-welcome-icon"><MessageSquare size={25} strokeWidth={1.4} /></span>
          <h2>今天想聊些什么？</h2>
          <p>聊聊近况，或一起找回工作线索。</p>
          <div className="pet-suggestions">
            {["最近做了哪些项目？", "有哪些还没完成的事情？"].map((q) => <button key={q} disabled={chat.busy || chat.running}
              onClick={() => { scroll.goBottom(); void chat.send(q); }}>{q}<ChevronRight size={14} /></button>)}
          </div>
        </div>}
        <ChatMessages chat={chat} onSource={(id) => void openSource(id)} />
        <RunFeedback chat={chat} composerStop />
        {chat.selected && <OperationCards key={chat.selected} conversation={chat.selected} />}
      </div>
    </div>
    <footer className="pet-composer">
      {scroll.unread && <button className="pet-new-messages" onClick={scroll.goBottom}><ArrowDown size={14} />有新消息</button>}
      <CodingControls chat={chat} />
      <ChatTools chat={chat} compact />
      <ChatComposer chat={chat} stop onSend={scroll.goBottom} />
      <p className="pet-input-hint">Enter 发送<span> · </span>Shift + Enter 换行</p>
    </footer>
    {drawer === "menu" && <ChatSheet title="对话与记忆" onClose={() => setDrawer(null)}>
      <label className="pet-conversation-picker">继续一段对话
        <select aria-label="选择会话" value={chat.selected} onChange={(e) => { chat.setSelected(e.target.value); setDrawer(null); }}>
          <option value="">新对话</option>{chat.conversations.map((c) => <option value={c.id} key={c.id}>{c.title}</option>)}
        </select>
      </label>
      {chat.recent && chat.recent !== chat.selected && <button className="pet-menu-item" onClick={() => { chat.setSelected(chat.recent); setDrawer(null); }}><MessageSquare size={17} />继续另一端最近的对话<ChevronRight size={15} /></button>}
      <button className="pet-menu-item" onClick={() => setDrawer("memories")}><BookOpen size={17} />记忆管理{pendingMemories > 0 && <small>{pendingMemories} 条待确认</small>}<ChevronRight size={15} /></button>
      <button className="pet-menu-item" onClick={() => setDrawer("reminders")}><Bell size={17} />提醒{reminders.length > 0 && <small>{reminders.length}</small>}<ChevronRight size={15} /></button>
      {chat.selected && <button className="pet-menu-item danger" onClick={() => void chat.removeConversation().then(() => setDrawer(null))}><Trash2 size={17} />删除会话</button>}
      <p className="pet-menu-note">对话保留 30 天，确认的记忆长期保留。应用内修改会先生成卡片，等你确认。</p>
    </ChatSheet>}
    {drawer === "memories" && <ChatSheet title="记忆管理" onClose={() => setDrawer(null)}>
      <MemoryControls chat={chat} onPreview={() => { setDrawer(null); scroll.goBottom(); }} />
      {chat.error && <p role="alert" className="agent-error">{chat.error}</p>}
    </ChatSheet>}
    {drawer === "reminders" && <ChatSheet title="提醒" onClose={() => setDrawer(null)}>
      <ReminderList items={reminders} onChange={() => window.dispatchEvent(new CustomEvent("client-change"))} onOpen={(r) => { openReminder(r); setDrawer(null); }} />
    </ChatSheet>}
    {source && <ChatSheet title={source.title} closeLabel="关闭来源" onClose={() => { sourceSequence.current++; setSource(null); }}><p className="preserve-lines">{source.text}</p></ChatSheet>}
    <MemorySource chat={chat} />
  </main>;
}
