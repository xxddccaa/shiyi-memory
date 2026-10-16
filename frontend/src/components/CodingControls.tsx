import { useEffect, useState } from "react";
import { FolderOpen, Terminal } from "lucide-react";
import { api, post } from "../api";
import type { AgentChatState } from "../hooks/useAgentChat";
import type { CodingState, Call } from "../hooks/useCodingControls";

type Workspace = CodingState["workspaces"][number];
export default function CodingControls({
  chat,
  state,
  tab,
}: {
  chat: AgentChatState;
  state: CodingState;
  tab: string;
}) {
  const {
    workspaces,
    setWorkspaces,
    path,
    setPath,
    chosen,
    setChosen,
    extensions,
    setExtensions,
    context,
    calls,
    setCalls,
    nextCalls,
    setNextCalls,
    pending,
    current,
    act,
    toggle,
  } = state;
  return (
    <section className="coding-controls">
      <div hidden={tab !== "coding"}>
        <div className="coding-settings">
          <p className="muted">
            绑定项目后，拾忆可自主读写文件和运行命令。以你的本机用户权限执行，目录不是沙箱。
          </p>
          <label>
            工作目录
            <select
              aria-label="编程工作目录"
              value={chosen}
              disabled={chat.running || pending}
              onChange={(e) => setChosen(e.target.value)}
            >
              <option value="">选择工作目录</option>
              {workspaces.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name} · {w.path}
                </option>
              ))}
            </select>
          </label>
          <div className="coding-path-entry">
            <input
              aria-label="添加本机目录"
              placeholder="项目目录的绝对路径"
              value={path}
              onChange={(e) => setPath(e.target.value)}
            />
            <button
              disabled={!path.trim() || pending || chat.running}
              onClick={() =>
                void act(async () => {
                  const value = await post<Workspace>("/agent/workspaces", {
                    path: path.trim(),
                  });
                  setWorkspaces((old) => [
                    ...old.filter((w) => w.id !== value.id),
                    value,
                  ]);
                  setChosen(value.id);
                  setPath("");
                })
              }
            >
              <FolderOpen size={14} />
              添加
            </button>
          </div>
          {chosen && (
            <>
              <button
                className="primary"
                disabled={pending || chat.running}
                onClick={() => void act(toggle)}
              >
                {current?.workspace_id === chosen && current.coding_enabled
                  ? "关闭此会话编程"
                  : current?.workspace_id === chosen
                    ? "开启此会话编程"
                    : "新建编程会话"}
              </button>
              <details className="coding-extensions">
                <summary>项目代码扩展</summary>
                <p className="muted">
                  自动读取项目 AGENTS.md、.pi/skills、.agents/skills 和
                  .pi/prompts。下列 JS／TS
                  扩展仅在你保存后加载，以本机权限执行；只添加你信任的代码。
                </p>
                <textarea
                  aria-label="Pi 扩展路径"
                  placeholder="每行一个本机扩展文件的绝对路径"
                  value={extensions}
                  onChange={(e) => setExtensions(e.target.value)}
                />
                <button
                  disabled={pending || chat.running}
                  onClick={() =>
                    void act(async () => {
                      const w = workspaces.find((item) => item.id === chosen)!;
                      const saved = await api<Workspace>(
                        `/agent/workspaces/${w.id}`,
                        {
                          method: "PATCH",
                          body: JSON.stringify({
                            ...w,
                            extensions: extensions
                              .split("\n")
                              .map((s) => s.trim())
                              .filter(Boolean),
                            expected_revision: w.revision,
                          }),
                        },
                      );
                      setWorkspaces((old) =>
                        old.map((item) =>
                          item.id === saved.id ? saved : item,
                        ),
                      );
                    })
                  }
                >
                  保存扩展
                </button>
              </details>
              <button
                className="text-button"
                disabled={pending || chat.running}
                onClick={() =>
                  void act(async () => {
                    await api(`/agent/workspaces/${chosen}`, {
                      method: "DELETE",
                    });
                    setWorkspaces((old) => old.filter((w) => w.id !== chosen));
                    setChosen("");
                    chat.refresh();
                  })
                }
              >
                移除目录登记（保留文件）
              </button>
            </>
          )}
        </div>
      </div>
      <div hidden={tab !== "context"}>
        {!chat.selected && (
          <p className="muted">开始对话后，可以查看上下文用量和进行压缩。</p>
        )}
        {chat.selected && (
          <div className="context-status">
            <span>
              {context?.compacting
                ? "正在压缩上下文…"
                : context?.tokens != null
                  ? `${context.estimated ? "估算 " : ""}${context.tokens.toLocaleString()} / ${context.contextWindow?.toLocaleString() || "未知"} tokens`
                  : "上下文用量待生成"}
              {context?.capacity_estimated &&
                " · 容量为临时估算，请在模型设置填写"}
              {!!context?.compactions && ` · 已压缩 ${context.compactions} 次`}
            </span>
            <button
              className="text-button"
              disabled={pending || chat.running || !chat.messages.length}
              onClick={() =>
                void act(async () => {
                  await post(`/conversations/${chat.selected}/compact`, {
                    request_id: crypto.randomUUID(),
                  });
                  chat.refresh();
                })
              }
            >
              立即压缩
            </button>
          </div>
        )}
      </div>
      <div hidden={tab !== "calls"} className="coding-call-list">
        <h3>
          <Terminal size={14} />
          工具执行记录 · {calls.length}
        </h3>
        {!calls.length && <p className="muted">本轮还没有工具执行记录。</p>}
        {calls.map((call) => (
          <ToolCall key={call.id} call={call} setError={chat.setError} />
        ))}
        {nextCalls !== null && (
          <button
            onClick={() =>
              void act(async () => {
                const v = await api<{
                  items: Call[];
                  next_offset: number | null;
                }>(
                  `/agent/runs/${chat.run!.id}/tool-calls?offset=${nextCalls}`,
                );
                setCalls((old) => [...old, ...v.items]);
                setNextCalls(v.next_offset);
              })
            }
          >
            加载更多执行记录
          </button>
        )}
      </div>
    </section>
  );
}
function ToolCall({
  call,
  setError,
}: {
  call: Call;
  setError: (s: string) => void;
}) {
  const [output, setOutput] = useState("");
  const [next, setNext] = useState<number | null>(0);
  useEffect(() => {
    setOutput("");
    setNext(0);
  }, [call.status]);
  const labels: Record<string, string> = {
    running: "执行中",
    completed: "已完成",
    failed: "失败",
    uncertain: "结果待核实",
    invalidated: "来源已失效",
  };
  return (
    <details className="coding-call">
      <summary>
        {call.name}
        <span>{labels[call.status] || call.status}</span>
      </summary>
      {call.arguments && <pre>{JSON.stringify(call.arguments, null, 2)}</pre>}
      {call.result?.change && (
        <pre className="coding-diff">
          {call.result.change.diff || "文件内容未变化"}
        </pre>
      )}
      {call.result?.content?.map(
        (c, i) => c.type === "text" && <pre key={i}>{c.text}</pre>,
      )}
      {call.name === "bash" && call.status !== "invalidated" && (
        <>
          {output && <pre>{output}</pre>}
          {next !== null && (
            <button
              onClick={() =>
                void api<{
                  text: string;
                  next_offset: number | null;
                  invalidated?: boolean;
                }>(`/agent/tool-calls/${call.id}/output?offset=${next}`)
                  .then((v) => {
                    setOutput((old) => (v.invalidated ? "" : old + v.text));
                    setNext(v.next_offset);
                  })
                  .catch((e) => setError(e.message))
              }
            >
              {output ? "继续读取输出" : "读取完整输出"}
            </button>
          )}
        </>
      )}
    </details>
  );
}
