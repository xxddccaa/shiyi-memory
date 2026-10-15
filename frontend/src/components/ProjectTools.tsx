import { useEffect, useState } from "react";
import { api, post } from "../api";
import type { Project, Conversation } from "../api";
import OperationCards from "./OperationCards";

interface Session {
  id: string;
  title: string;
  category: string;
  revision: number;
  started_at: string;
  ended_at: string;
  sources: { id: string }[];
}
export default function ProjectTools({
  onSource,
}: {
  onSource: (id: string) => void;
}) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selected, setSelected] = useState("");
  const [other, setOther] = useState("");
  const [alias, setAlias] = useState("");
  const [aliases, setAliases] = useState<string[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [conversation, setConversation] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    const changed = () => setGeneration((v) => v + 1);
    window.addEventListener("client-change", changed);
    return () => window.removeEventListener("client-change", changed);
  }, []);
  useEffect(() => {
    void api<Project[]>("/projects")
      .then((v) => {
        const current = v.filter((p) => !["merged", "deleted"].includes(p.state));
        setProjects(current);
        setSelected((id) => current.some((p) => p.id === id) ? id : "");
        setOther((id) => current.some((p) => p.id === id) ? id : "");
      })
      .catch((e) => setError(e.message));
  }, [generation]);
  useEffect(() => {
    if (!selected) {
      setSessions([]);
      return;
    }
    void api<{ items: Session[] }>(`/work-sessions?project_id=${selected}`)
      .then((v) => setSessions(v.items))
      .catch((e) => setError(e.message));
    void api<{ items: string[] }>(`/projects/${selected}/aliases`)
      .then((v) => setAliases(v.items))
      .catch(() => {});
  }, [selected, generation]);
  async function propose(body: Record<string, unknown>) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      let id = conversation;
      if (!id) {
        id = (
          await post<Conversation>("/conversations", {
            title: "工作上下文管理",
          })
        ).id;
        setConversation(id);
      }
      await post(`/conversations/${id}/operations`, body);
      window.dispatchEvent(
        new CustomEvent("client-change", { detail: { kind: "operation" } }),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel project-tools">
      <h2>项目管理与工作段</h2>
      <div className="agent-toolbar">
        <button disabled={busy || projects.length === 0} onClick={() => void propose({action: "project.delete_many", all_projects: true})}>
          预览删除全部项目
        </button>
        <span className="muted">一次核对完整清单并确认；工作记录、待办和记忆保留。</span>
      </div>
      <label>
        选择项目{" "}
        <select
          aria-label="管理项目"
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
        >
          <option value="">请选择</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      {selected && (
        <>
          <button disabled={busy} onClick={() => void propose({action: "project.delete", target_id: selected})}>预览删除当前项目</button>
          <p className="muted">已确认别名：{aliases.join("、") || "暂无"}</p>
          <div className="agent-toolbar">
            <input
              aria-label="项目别名"
              value={alias}
              onChange={(e) => setAlias(e.target.value)}
              maxLength={200}
              placeholder="同一个项目的其他名称"
            />
            <button
              disabled={!alias.trim()}
              onClick={() =>
                void propose({
                  action: "project.alias",
                  target_id: selected,
                  text: alias.trim(),
                })
              }
            >
              提议添加别名
            </button>
          </div>
          <div className="agent-toolbar">
            <select
              aria-label="待合并项目"
              value={other}
              onChange={(e) => setOther(e.target.value)}
            >
              <option value="">选择需要并入的项目</option>
              {projects
                .filter((p) => p.id !== selected)
                .map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
            </select>
            <button
              disabled={!other}
              onClick={() =>
                void propose({
                  action: "project.merge",
                  target_id: selected,
                  other_id: other,
                })
              }
            >
              预览合并
            </button>
          </div>
          <p className="muted">工作段表示实际观察跨度，不代表持续工作时长。</p>
          {sessions.map((s) => (
            <div className="work-session" key={s.id}>
              <p>{s.title}</p>
              <small>
                {new Date(s.started_at).toLocaleString("zh-CN")} —{" "}
                {new Date(s.ended_at).toLocaleTimeString("zh-CN")}
              </small>
              <div className="agent-toolbar">
                <select
                  aria-label="工作段分类"
                  value={s.category}
                  onChange={(e) =>
                    void propose({
                      action: "session.update",
                      target_id: s.id,
                      category: e.target.value,
                    })
                  }
                >
                  {["工作", "开发", "沟通", "会议", "阅读", "其他"].map((v) => (
                    <option key={v}>{v}</option>
                  ))}
                </select>
                {s.sources.map((v) => (
                  <button
                    key={v.id}
                    onClick={() => onSource(v.id.split(":")[1])}
                  >
                    查看记录
                  </button>
                ))}
              </div>
            </div>
          ))}
        </>
      )}
      {error && <p role="alert">{error}</p>}
      {conversation && <OperationCards conversation={conversation} />}
    </section>
  );
}
