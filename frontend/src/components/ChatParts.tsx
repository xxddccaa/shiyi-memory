import { useLayoutEffect, useRef } from "react";
import { LoaderCircle, RotateCcw, Send, Sparkles, Square } from "lucide-react";
import { post } from "../api";
import type { Evidence } from "../api";
import type { AgentChatState } from "../hooks/useAgentChat";
import ChatSheet from "./ChatSheet";

export function ChatMessages({ chat, onSource }: { chat: AgentChatState; onSource: (id: string) => void }) {
  const { hasOlder, loadOlder, messages, setMemorySource, setError, refresh } = chat;
  return (
      <div aria-live="polite">
        {hasOlder && (
          <button
            onClick={loadOlder}
          >
            加载更早的消息
          </button>
        )}
        {messages.map((message) => (
          <div className="chat-turn" key={message.id}>
            <div
              className={
                message.role === "user" ? "question-bubble" : "answer-bubble"
              }
            >
              {message.role === "assistant" && (
                <span className="answer-label">
                  <Sparkles size={16} />
                  拾忆
                </span>
              )}
              <p className="preserve-lines">
                {message.content?.replace(
                  /\[(?:证据[:：])?(?:summary|raw|fact|memory|todo|project):[a-f0-9-]+\]/g,
                  (id) => {
                    const index = message.sources.findIndex(
                      (source) => source.id === id.slice(1, -1).replace(/^证据[:：]/, ""),
                    );
                    return index >= 0 ? `[${index + 1}]` : "";
                  },
                ) || "这条回答的来源已更新或到期，请重新提问。"}
              </p>
              <div className="citations">
                {message.sources.map((s, index) => (
                  <button
                    key={s.id}
                    onClick={() =>
                      s.episode_id
                        ? onSource(s.episode_id)
                        : void post<{ items: Evidence[] }>(
                            "/memory/evidence/resolve",
                            { ids: [s.id] },
                          )
                            .then((value) => {
                              setMemorySource(value.items[0] || null);
                              if (!value.items.length) {
                                setError("这条记忆已更新或删除。");
                                refresh();
                              }
                            })
                            .catch((e) => setError(e.message))
                    }
                    title={s.content}
                  >
                    {index + 1}.{" "}
                    {s.kind === "raw"
                      ? "原文"
                      : s.kind === "user_confirmed"
                        ? "确认的记忆"
                        : "工作摘要"}{" "}
                    · {s.captured_at ? new Date(s.captured_at).toLocaleDateString("zh-CN") : "未记录时间"}
                  </button>
                ))}
                {message.external_sources?.filter((s) => /^https?:\/\//i.test(s.url)).map((s) => (
                  <a className="external-citation" key={s.id} href={s.url} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">
                    ↗ {s.title}<small>{s.provider} · 查询于 {new Date(s.retrieved_at).toLocaleString("zh-CN")}</small>
                  </a>
                ))}
              </div>
            </div>
          </div>
        ))}
      </div>
  );
}

export function RunFeedback({ chat, composerStop = false }: { chat: AgentChatState; composerStop?: boolean }) {
  const { running, run, busy, error, progress, cancel, retry, refresh } = chat;
  return <>
    {running && <div className="loading chat-progress" role="status">
      <LoaderCircle className="spin" size={16} />{progress || "准备回答"}
      {!composerStop && <button className="text-button" onClick={() => void cancel()}><Square size={13} />停止</button>}
    </div>}
    {!running && run?.error && <div role="status" className={`chat-run-status ${run.status === "failed" ? "is-error" : ""}`}>
      <p>{run.error}</p>
      {run.retryable ? <button className="text-button" disabled={busy} onClick={() => void retry()}><RotateCcw size={14} />重新回答</button>
        : run.status === "failed" && run.retry_blocked_reason && <small>{run.retry_blocked_reason}</small>}
    </div>}
    {error && <div role="alert" className="agent-error"><p>{error}</p><button className="text-button" onClick={refresh}>刷新会话</button></div>}
  </>;
}

export function ChatComposer({ chat, stop = false, onSend }: { chat: AgentChatState; stop?: boolean; onSend?: () => void }) {
  const { question, setQuestion, busy, running, send, cancel } = chat;
  const input = useRef<HTMLTextAreaElement>(null);
  const composing = useRef(false);
  useLayoutEffect(() => {
    if (!input.current) return;
    input.current.style.height = "0px";
    input.current.style.height = Math.min(input.current.scrollHeight, 96) + "px";
  }, [question]);
  const submit = () => {
    if (!question.trim() || busy || running) return;
    onSend?.();
    void send();
  };
  return <form className="chat-form chat-composer" onSubmit={(e) => { e.preventDefault(); submit(); }}>
    <textarea ref={input} aria-label="提问" rows={1} placeholder="和拾忆聊聊…" value={question}
      onChange={(e) => setQuestion(e.target.value)}
      onCompositionStart={() => { composing.current = true; }}
      onCompositionEnd={() => { composing.current = false; }}
      onKeyDown={(e) => {
        if (e.key === "Enter" && !e.shiftKey && !composing.current && !e.nativeEvent.isComposing && e.nativeEvent.keyCode !== 229) {
          e.preventDefault(); submit();
        }
      }} />
    {stop && running ? <button type="button" className="primary" aria-label="停止" onClick={() => void cancel()}><Square size={16} /></button>
      : <button type="submit" className="primary" disabled={busy || running || !question.trim()} aria-label="发送问题"><Send size={17} /></button>}
  </form>;
}

export function MemoryControls({ chat, onPreview }: { chat: AgentChatState; onPreview?: () => void }) {
  const { showAllMemories, setShowAllMemories, editingMemory, setEditingMemory, memoryText,
    setMemoryText, candidates, changeCandidate, previewMemory } = chat;
  return <>
      <button
        className="text-button"
        onClick={() => setShowAllMemories((value) => !value)}
      >
        {showAllMemories ? "返回本次对话候选" : "管理长期记忆"}
      </button>
      {editingMemory && (
        <div className="memory-candidate">
          <label>
            修改长期记忆
            <textarea
              aria-label="修改长期记忆"
              value={memoryText}
              maxLength={6000}
              onChange={(e) => setMemoryText(e.target.value)}
            />
          </label>
          <button
            onClick={() => void previewMemory().then((ok) => { if (ok) onPreview?.(); })}
          >
            预览修改
          </button>
          <button onClick={() => setEditingMemory(null)}>取消</button>
        </div>
      )}
      {!!candidates.length && (
        <section className="memory-candidates">
          <h3>{showAllMemories ? "已确认的长期记忆" : "从这次对话记住什么"}</h3>
          <p className="muted">以下是你的原话，确认后才会用于以后的回答。</p>
          {candidates.map((c) => (
            <div className="memory-candidate" key={c.id}>
              <p>{c.text}</p>
              <small>
                {c.status === "confirmed" ? "已确认 · 长期保留" : "待确认"}
              </small>
              {c.status === "confirmed" && (
                <button
                  onClick={() => {
                    setEditingMemory(c);
                    setMemoryText(c.text || "");
                  }}
                >
                  编辑
                </button>
              )}
              {c.status === "pending" && !!c.conflicts?.length && (
                <div className="memory-conflicts">
                  <p className="muted">
                    以下记忆可能相关或冲突，请核对；可以同时保留，也可以明确替换。
                  </p>
                  {c.conflicts.map((old) => (
                    <div key={old.id}>
                      <blockquote>{old.text}</blockquote>
                      <button
                        onClick={() => void changeCandidate(c, "confirm", old)}
                      >
                        用新记忆替换这一条
                      </button>
                    </div>
                  ))}
                </div>
              )}
              {c.status === "pending" && (
                <button onClick={() => void changeCandidate(c, "confirm")}>
                  确认记住
                </button>
              )}
              <button onClick={() => void changeCandidate(c, "dismiss")}>
                {c.status === "confirmed" ? "忘记" : "忽略"}
              </button>
            </div>
          ))}
        </section>
      )}

    {!candidates.length && !editingMemory && <p className="muted">{showAllMemories ? "还没有确认的长期记忆。" : "本次对话还没有待确认的记忆。"}</p>}
  </>;
}

export function MemorySource({ chat }: { chat: AgentChatState }) {
  const { memorySource, setMemorySource } = chat;
  return memorySource && <ChatSheet title={memorySource.source_kind === "memory" ? "你确认的记忆" : "当前有效信息"}
    closeLabel="关闭来源" onClose={() => setMemorySource(null)}>
    <p className="preserve-lines">{memorySource.content}</p>
  </ChatSheet>;
}
