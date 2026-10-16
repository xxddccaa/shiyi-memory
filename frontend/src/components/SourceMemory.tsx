import { useEffect, useState } from "react";
import { api, post, DAILY_WORK_LABEL } from "../api";
import type { Episode } from "../api";

type Proposal = { id: string; revision: number; status: string };
export default function SourceMemory({
  episode,
  text,
  factId,
}: {
  episode: Episode;
  text: string;
  factId?: string;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(text);
  const [day, setDay] = useState(
    new Date(episode.started_at).toLocaleDateString("sv-SE"),
  );
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [requestId, setRequestId] = useState(() => crypto.randomUUID());
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const [projectId, setProjectId] = useState(episode.project_id || "");
  useEffect(() => {
    if (editing)
      void api<{ id: string; name: string }[]>("/projects")
        .then(setProjects)
        .catch((e) => setMessage(e.message));
  }, [editing]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  async function submit() {
    setBusy(true);
    setMessage("");
    try {
      if (proposal) {
        const result = await post<Proposal>(
          `/operations/${proposal.id}/decision`,
          { action: "confirm", expected_revision: proposal.revision },
        );
        if (result.status !== "executed") {
          setProposal(null);
          setRequestId(crypto.randomUUID());
          throw new Error("来源已变化，请重新核对后生成预览");
        }
        setEditing(false);
        setProposal(null);
        setMessage("已确认并长期保存");
        setRequestId(crypto.randomUUID());
        window.dispatchEvent(new Event("memory-changed"));
      } else {
        setProposal(
          await post<Proposal>(`/episodes/${episode.id}/memory-proposals`, {
            request_id: requestId,
            expected_revision: episode.revision,
            text: value,
            date: day,
            project_id: projectId || null,
            fact_id: factId || null,
          }),
        );
      }
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="source-memory">
      {!editing ? (
        <button
          onClick={() => {
            setEditing(true);
            setValue(text);
          }}
        >
          编辑并长期留存
        </button>
      ) : (
        <div>
          <label>
            确认保存的内容
            <textarea
              aria-label="长期记忆内容"
              value={value}
              maxLength={2000}
              disabled={!!proposal}
              onChange={(e) => {
                setValue(e.target.value);
                setRequestId(crypto.randomUUID());
              }}
            />
          </label>
          <label>
            回顾日期
            <input
              type="date"
              value={day}
              disabled={!!proposal}
              onChange={(e) => {
                setDay(e.target.value);
                setRequestId(crypto.randomUUID());
              }}
            />
          </label>
          <label>
            归属项目
            <select
              aria-label="长期记忆项目"
              disabled={!!proposal}
              value={projectId}
              onChange={(e) => {
                setProjectId(e.target.value);
                setRequestId(crypto.randomUUID());
              }}
            >
              <option value="">{DAILY_WORK_LABEL}（默认分类）</option>
              {projects.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <p className="muted">
            原文出现过这些文字，不等于你完成了其中事项。确认内容会长期保存；主动删改原文时会联动删除或要求重新核对。
          </p>
          <button
            disabled={busy || !value.trim()}
            onClick={() => void submit()}
          >
            {proposal ? "确认并长期保存" : "预览确认内容"}
          </button>
          <button
            disabled={busy}
            onClick={() => {
              if (proposal)
                void post(`/operations/${proposal.id}/decision`, {
                  action: "reject",
                  expected_revision: proposal.revision,
                }).catch((e) => setMessage(e.message));
              setProposal(null);
              setRequestId(crypto.randomUUID());
              setEditing(false);
            }}
          >
            取消
          </button>
        </div>
      )}
      {message && <p role="status">{message}</p>}
    </div>
  );
}
