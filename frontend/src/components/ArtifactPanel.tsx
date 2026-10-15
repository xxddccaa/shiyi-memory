import { useEffect, useRef, useState } from "react";
import { api, post } from "../api";
import type { WorkArtifact } from "../api";

const sections = {
  observations: "屏幕摘录 · 不代表用户已完成事项",
  progress: "已确认进展",
  decisions: "决策",
  blockers: "阻塞与等待",
  next_steps: "明确的下一步",
} as const;
export default function ArtifactPanel({
  path,
  editable = false,
  onSource,
}: {
  path: string;
  editable?: boolean;
  onSource: (id: string) => void;
}) {
  const [chosen, setChosen] = useState<string[] | null>(null);
  const [sourceText, setSourceText] = useState("");
  const [draftId, setDraftId] = useState<string | null>(null);
  const [draftHash, setDraftHash] = useState<string | null>(null);
  const [artifact, setArtifact] = useState<WorkArtifact | null>(null);
  const [text, setText] = useState("");
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const scope = useRef(path);
  scope.current = path;
  useEffect(() => {
    let closed = false;
    let timer: ReturnType<typeof setTimeout>;
    setArtifact(null);
    setChosen(null);
    setDraftId(null);
    setEditing(false);
    setError("");
    const load = async () => {
      try {
        const value = await api<WorkArtifact>(path);
        if (closed) return;
        setArtifact(value);
        if (
          ["pending", "running", "stale"].includes(
            value.draft?.status || value.status,
          )
        )
          timer = setTimeout(load, 3000);
      } catch (e) {
        if (!closed) setError((e as Error).message);
      }
    };
    void load();
    // Periodic read catches source deletion/correction while this page stays open.
    const interval = setInterval(() => {
      if (!document.hidden) void load();
    }, 15000);
    return () => {
      closed = true;
      clearTimeout(timer);
      clearInterval(interval);
    };
  }, [path, refreshing]);
  const documentText = () =>
    artifact?.user_text ??
    (artifact?.draft?.body || artifact?.body
      ? Object.entries(sections)
          .filter(([key]) => !editable || key !== "observations")
          .map(
            ([key, title]) =>
              title +
              "\n" +
              (artifact?.draft?.body || artifact?.body)![
                key as keyof typeof sections
              ]
                ?.map((item) => "- " + item.text)
                .join("\n"),
          )
          .join("\n\n")
      : "");
  const exportText = () => {
    const url = URL.createObjectURL(
      new Blob([documentText()], { type: "text/plain;charset=utf-8" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = "拾忆日报.txt";
    link.click();
    URL.revokeObjectURL(url);
  };
  async function save() {
    if (!artifact) return;
    const current = path;
    try {
      const value = await api<WorkArtifact>(`/reviews/${artifact.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          text,
          expected_revision: artifact.edit_revision,
          draft_id: draftId,
          draft_hash: draftHash,
        }),
      });
      if (scope.current === current) {
        setArtifact(value);
        setEditing(false);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <section className="artifact-panel">
      <div className="agent-toolbar">
        <span className="muted">
          {artifact
            ? "整理于 " + new Date(artifact.updated_at).toLocaleString("zh-CN")
            : "读取工作上下文…"}
        </span>
        <button
          className="secondary"
          disabled={
            editing ||
            ["pending", "running"].includes(artifact?.draft?.status || "")
          }
          onClick={() => {
            if (editable && artifact) {
              const ids =
                chosen ??
                artifact.materials
                  ?.filter((item) => item.selected)
                  .map((item) => item.id) ??
                [];
              if (!ids.length) {
                setError("先核对并选择至少一条确认材料，也可以直接手工编辑。");
                return;
              }
              void post<WorkArtifact>(`/reviews/${artifact.id}/draft`, {
                request_id: crypto.randomUUID(),
                expected_revision: artifact.edit_revision,
                materials: artifact.materials
                  ?.filter((item) => ids.includes(item.id))
                  .map((item) => ({ id: item.id, revision: item.revision })),
              })
                .then((value) => {
                  setArtifact({ ...artifact, draft: value });
                  setError("");
                  setRefreshing((v) => !v);
                })
                .catch((e) => setError(e.message));
            } else {
              void post<WorkArtifact>(
                path + (path.includes("?") ? "&" : "?") + "refresh=true",
              )
                .then(setArtifact)
                .catch((e) => setError(e.message));
            }
          }}
        >
          {editable ? "一键生成确认材料草稿" : "重新整理"}
        </button>
      </div>
      {editable && artifact?.materials && (
        <details open>
          <summary>选择已确认材料（{artifact.materials.length}条）</summary>
          {Array.from(
            new Set(artifact.materials.map((item) => item.project_name)),
          ).map((project) => (
            <section key={project}>
              <h4>{project}</h4>
              {artifact
                .materials!.filter((item) => item.project_name === project)
                .map((item) => (
                  <label className="report-material" key={item.id}>
                    <input
                      type="checkbox"
                      checked={(
                        chosen ??
                        artifact
                          .materials!.filter((v) => v.selected)
                          .map((v) => v.id)
                      ).includes(item.id)}
                      onChange={(e) => {
                        const ids =
                          chosen ??
                          artifact
                            .materials!.filter((v) => v.selected)
                            .map((v) => v.id);
                        setChosen(
                          e.target.checked
                            ? [...ids, item.id]
                            : ids.filter((id) => id !== item.id),
                        );
                      }}
                    />
                    {item.content}
                  </label>
                ))}
            </section>
          ))}
        </details>
      )}
      {editable && artifact?.observation_groups && (
        <details>
          <summary>未确认屏幕摘录</summary>
          {artifact.observation_groups.map((group, i) => (
            <section key={i}>
              <h4>{group.project_name}</h4>
              {group.items.map((item, n) => (
                <p key={n}>
                  {item.text}
                  <button
                    onClick={() => onSource(item.evidence_ids[0].split(":")[1])}
                  >
                    核对原文
                  </button>
                </p>
              ))}
            </section>
          ))}
        </details>
      )}
      {artifact?.draft && (
        <p role="status">
          确认材料草稿：
          {(
            {
              pending: "等待生成",
              running: "正在生成",
              ready: "已生成，可核对编辑",
              failed: "生成失败，可重试",
              invalidated: "材料已变化，请重新选择",
            } as Record<string, string>
          )[artifact.draft.status] || artifact.draft.status}
        </p>
      )}
      {sourceText && (
        <p className="source-excerpt">
          {sourceText}
          <button onClick={() => setSourceText("")}>关闭</button>
        </p>
      )}
      {error && (
        <p role="alert" className="agent-error">
          {error}
        </p>
      )}
      {artifact &&
        ["pending", "running", "stale"].includes(artifact.status) && (
          <p role="status">
            正在整理 {artifact.processed_sources ?? 0} /{" "}
            {artifact.total_sources ?? 0} 条来源；完整处理后发布。
          </p>
        )}
      {artifact?.status === "failed" && (
        <p role="status">
          这次整理未完成，已尝试 {artifact.attempts}{" "}
          次。检查模型后可手动重新整理。
        </p>
      )}
      {artifact?.needs_review && (
        <p role="status">
          部分来源已被纠正或删除，旧内容已隐藏。请核对你的编辑内容。
          <button
            onClick={() =>
              void api<{ text: string | null; notice: string }>(
                `/reviews/${artifact.id}/saved-edit`,
              )
                .then((v) => {
                  setDraftId(null);
                  setDraftHash(null);
                  setText(v.text || "");
                  setEditing(true);
                })
                .catch((e) => setError(e.message))
            }
          >
            核对保留的人工编辑
          </button>
        </p>
      )}
      {artifact?.body && (
        <div className="artifact-sections">
          {Object.entries(sections)
            .filter(([key]) => !editable || key !== "observations")
            .map(([key, title]) => (
              <section key={key}>
                <h3>{title}</h3>
                {(artifact.draft?.body || artifact.body)![
                  key as keyof typeof sections
                ]?.length ? (
                  (artifact.draft?.body || artifact.body)![
                    key as keyof typeof sections
                  ]?.map((item, i) => (
                    <div key={i}>
                      <p>{item.text}</p>
                      <div className="citations">
                        {item.evidence_ids.map((id, n) => (
                          <button
                            key={id}
                            onClick={() => {
                              if (id.startsWith("memory:"))
                                void api<{ text: string }>(
                                  `/memory/candidates/${id.split(":")[1]}`,
                                )
                                  .then((v) => setSourceText(v.text))
                                  .catch((e) => setError(e.message));
                              else onSource(id.split(":")[1]);
                            }}
                          >
                            来源 {n + 1}
                          </button>
                        ))}
                      </div>
                    </div>
                  ))
                ) : (
                  <p className="muted">暂无明确记录</p>
                )}
              </section>
            ))}
        </div>
      )}
      {!!artifact?.body?.unknowns.length && (
        <div className="muted">
          {artifact.body.unknowns.map((u, i) => (
            <p key={i}>{u}</p>
          ))}
        </div>
      )}
      {artifact?.body?.coverage && !artifact.body.coverage.truncated && (
        <p className="muted">
          已处理范围内全部 {artifact.body.coverage.total}{" "}
          条来源；自动采集部分保留原句，不代表实际工作成果。
        </p>
      )}
      {artifact?.body?.coverage?.truncated && (
        <p className="muted">
          本次覆盖最近 {artifact.body.coverage.source_count} /{" "}
          {artifact.body.coverage.total} 条记录，不能视作完整历史。
        </p>
      )}
      {editable && artifact && (
        <div className="review-editor">
          {editing ? (
            <>
              <textarea
                aria-label="编辑日报"
                value={text}
                maxLength={20000}
                onChange={(e) => setText(e.target.value)}
              />
              <button className="primary" onClick={() => void save()}>
                保存编辑
              </button>
              <button onClick={() => setEditing(false)}>取消</button>
            </>
          ) : (
            <>
              {artifact.user_text !== null && (
                <p className="preserve-lines">{artifact.user_text}</p>
              )}
              <button
                className="secondary"
                onClick={() => {
                  setText(documentText());
                  const draft =
                    artifact.user_text == null ? artifact.draft : null;
                  setDraftId(draft?.id || null);
                  setDraftHash(draft?.requested_hash || null);
                  setEditing(true);
                }}
              >
                编辑日报
              </button>
              {artifact.draft?.status === "ready" &&
                artifact.user_text != null && (
                  <button
                    onClick={() => {
                      const body = artifact.draft!.body!;
                      setText(
                        Object.entries(sections)
                          .filter(([key]) => key !== "observations")
                          .map(
                            ([key, title]) =>
                              title +
                              "\n" +
                              (body[key as keyof typeof sections] || [])
                                .map((item) => "- " + item.text)
                                .join("\n"),
                          )
                          .join("\n\n"),
                      );
                      setDraftId(artifact.draft!.id);
                      setDraftHash(artifact.draft!.requested_hash || null);
                      setEditing(true);
                    }}
                  >
                    用新草稿开始编辑
                  </button>
                )}
              <button className="text-button" onClick={exportText}>
                导出文本
              </button>
            </>
          )}
          <p className="muted">你的编辑单独保存，自动整理不会覆盖。</p>
        </div>
      )}
    </section>
  );
}
