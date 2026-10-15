import CodingControls from "./CodingControls";
import { Plus, Sparkles, Trash2 } from "lucide-react";
import useAgentChat from "../hooks/useAgentChat";
import { ChatMessages, RunFeedback, ChatComposer, MemoryControls, MemorySource } from "./ChatParts";
import ChatTools from "./ChatTools";
import OperationCards from "./OperationCards";

export default function AgentChat({ onSource }: { onSource: (id: string) => void }) {
  const chat = useAgentChat();
  const { conversations, selected, setSelected, removeConversation, recent, messages } = chat;
  return (
    <section className="panel chat-panel agent-panel">
      <div className="agent-toolbar">
        <select
          aria-label="选择会话"
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
        >
          <option value="">新对话</option>
          {conversations.map((c) => (
            <option key={c.id} value={c.id}>
              {c.title}
            </option>
          ))}
        </select>
        <button className="secondary" onClick={() => setSelected("")}>
          <Plus size={15} />
          新对话
        </button>
        {selected && (
          <button
            className="icon-button"
            aria-label="删除会话"
            onClick={() => void removeConversation()}
          >
            <Trash2 size={17} />
          </button>
        )}
      </div>
      {recent && recent !== selected && (
        <button className="text-button" onClick={() => setSelected(recent)}>
          继续另一端最近的对话
        </button>
      )}
      {!messages.length && (
        <div className="chat-welcome">
          <div className="empty-icon">
            <Sparkles size={28} />
          </div>
          <h2>让过去的工作，帮到现在的你。</h2>
          <p>
            我是拾忆。可以陪你聊聊，也可以主动查找记忆、补充上下文，再回答你的问题。
          </p>
          <div className="suggestions">
            {[
              "最近做了哪些项目？",
              "最近的决定发生过什么变化？",
              "有哪些还没完成的事情？",
            ].map((q) => (
              <button
                key={q}
                disabled={chat.busy || chat.running}
                onClick={() => void chat.send(q)}
              >
                {q}
              </button>
            ))}
          </div>
        </div>
      )}
      <ChatMessages chat={chat} onSource={onSource} />
      <RunFeedback chat={chat} />
      {selected && <OperationCards key={selected} conversation={selected} />}
      <CodingControls chat={chat} />
      <ChatTools chat={chat} />
      <ChatComposer chat={chat} />
      <p className="chat-footnote">对话保留 30 天；你确认的记忆长期保留。回答附带可核验的来源。</p>
      <MemoryControls chat={chat} />
      <MemorySource chat={chat} />
    </section>
  );
}
