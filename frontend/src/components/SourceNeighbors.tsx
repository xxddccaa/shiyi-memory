import { useEffect, useState } from "react";
import { api } from "../api";
import type { Evidence } from "../api";
export default function SourceNeighbors({
  id,
  onSource,
}: {
  id: string;
  onSource: (id: string) => void;
}) {
  const [items, setItems] = useState<(Evidence & { gap_seconds: number })[]>(
    [],
  );
  const [error, setError] = useState("");
  useEffect(() => {
    let closed = false;
    setItems([]);
    setError("");
    void api<{ items: (Evidence & { gap_seconds: number })[] }>(
      `/episodes/${id}/neighbors`,
    )
      .then((value) => {
        if (!closed) setItems(value.items);
      })
      .catch((e) => {
        if (!closed) setError(e.message);
      });
    return () => {
      closed = true;
    };
  }, [id]);
  return (
    <section className="source-neighbors">
      <h3>相邻的工作记录</h3>
      {error && <p role="status">{error}</p>}
      {items.map((item) => (
        <button
          className="text-button"
          key={item.id}
          onClick={() => onSource(item.episode_id!)}
        >
          {item.captured_at ? new Date(item.captured_at).toLocaleString("zh-CN") : "未记录时间"} · 相隔{" "}
          {Math.round(item.gap_seconds / 60)} 分钟
        </button>
      ))}
      {!items.length && !error && <p className="muted">暂无相邻记录</p>}
      <p className="muted">间隔内可能没有采集记录，不代表持续工作时长。</p>
    </section>
  );
}
