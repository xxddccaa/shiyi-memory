import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { MemoryCandidate } from "../api";
export default function MemoryReview({
  onSource,
}: {
  onSource: (id: string) => void;
}) {
  const [items, setItems] = useState<MemoryCandidate[]>([]);
  const [filter, setFilter] = useState("confirmed");
  const [expiring, setExpiring] = useState<
    { id: string; summary: string; expires_at: string }[]
  >([]);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<MemoryCandidate | null>(null);
  const [cursor, setCursor] = useState<number | null>(null);
  const load = useCallback(
    () =>
      Promise.all([
        api<{ items: MemoryCandidate[]; next_cursor: number | null }>(
          `/memory/candidates?status=${filter === "expiring" ? "confirmed" : filter}`,
        ).then((v) => {
          setItems(v.items);
          setCursor(v.next_cursor);
        }),
        api<{ items: typeof expiring }>("/memory/expiring").then((v) =>
          setExpiring(v.items),
        ),
      ]).catch((e) => setError(e.message)),
    [filter],
  );
  useEffect(() => {
    void load();
    window.addEventListener("memory-changed", load);
    window.addEventListener("client-change", load);
    return () => {
      window.removeEventListener("memory-changed", load);
      window.removeEventListener("client-change", load);
    };
  }, [load]);
  async function save(action: "confirm" | "dismiss") {
    if (!editing) return;
    try {
      await api(`/memory/candidates/${editing.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          action,
          expected_revision: editing.revision,
          text: editing.text,
        }),
      });
      setEditing(null);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <section className="panel settings-card">
      <h2>确认记忆与到期核对</h2>
      <select
        aria-label="记忆核对范围"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      >
        <option value="confirmed">已确认记忆</option>
        <option value="needs_review">待重新核对</option>
        <option value="expiring">7天内到期的摘录</option>
      </select>
      {error && <p role="alert">{error}</p>}
      {filter === "expiring"
        ? expiring.map((item) => (
            <div key={item.id}>
              <p>{item.summary}</p>
              <button onClick={() => onSource(item.id)}>
                查看原文并确认保存
              </button>
            </div>
          ))
        : items
            .filter((item) => item.status === filter)
            .map((item) => (
              <div key={item.id} className="source-excerpt">
                <p>{item.text}</p>
                {item.origin?.episode_id && (
                  <button onClick={() => onSource(item.origin!.episode_id!)}>
                    查看来源
                  </button>
                )}
                <button onClick={() => setEditing(item)}>编辑或忘记</button>
              </div>
            ))}
      {cursor !== null && filter !== "expiring" && (
        <button
          onClick={() =>
            void api<{ items: MemoryCandidate[]; next_cursor: number | null }>(
              `/memory/candidates?status=${filter}&offset=${cursor}`,
            )
              .then((v) => {
                setItems((items) => [...items, ...v.items]);
                setCursor(v.next_cursor);
              })
              .catch((e) => setError(e.message))
          }
        >
          加载更多记忆
        </button>
      )}
      {editing && (
        <div>
          <textarea
            aria-label="编辑确认记忆"
            value={editing.text}
            onChange={(e) => setEditing({ ...editing, text: e.target.value })}
            maxLength={2000}
          />
          <button
            disabled={!editing.text.trim()}
            onClick={() => void save("confirm")}
          >
            确认保存
          </button>
          <button onClick={() => void save("dismiss")}>确认忘记这条记忆</button>
          <button onClick={() => setEditing(null)}>取消</button>
        </div>
      )}
    </section>
  );
}
