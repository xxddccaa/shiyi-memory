import { useEffect, useState } from "react";
import { api } from "../api";

type History = {
  items: {
    trigger?: string;
    id: string;
    topic: string;
    text: string | null;
    status: string;
    label: string;
    detail: string;
    created_at: string;
    shown_at: string | null;
  }[];
  total: number;
  retention_hours: number;
  status: {
    reason: string | null;
    text: string;
    next_at: string | null;
    today_shown: number;
    daily_limit: number;
    timezone: string;
    muted_until: string | null;
    checked_at: string;
  };
};

export default function ProactiveHistory({ revision }: { revision: string }) {
  const [data, setData] = useState<History | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [limit, setLimit] = useState(30);
  useEffect(() => {
    let disposed = false;
    let controller: AbortController | null = null;
    const load = async () => {
      controller?.abort();
      const request = new AbortController();
      controller = request;
      setLoading(true);
      try {
        const value = await api<History>(
          `/client/proactive/history?limit=${limit}`,
          { signal: request.signal },
        );
        if (!disposed && !request.signal.aborted) {
          setData(value);
          setError("");
        }
      } catch (e) {
        if (!disposed && !request.signal.aborted) {
          setData(null);
          setError((e as Error).message);
        }
      } finally {
        if (!disposed && !request.signal.aborted) setLoading(false);
      }
    };
    const update = () => {
      if (!document.hidden) void load();
    };
    void load();
    const timer = setInterval(update, 10000);
    window.addEventListener("client-change", update);
    document.addEventListener("visibilitychange", update);
    return () => {
      disposed = true;
      controller?.abort();
      clearInterval(timer);
      window.removeEventListener("client-change", update);
      document.removeEventListener("visibilitychange", update);
    };
  }, [revision, refresh, limit]);
  function time(value: string) {
    return new Date(value).toLocaleString("zh-CN", {
      timeZone: data?.status.timezone,
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }
  return (
    <section className="proactive-history" aria-label="陪伴记录">
      <div className="proactive-history-heading">
        <h4>陪伴记录</h4>
        <button
          disabled={loading}
          onClick={() => setRefresh((value) => value + 1)}
        >
          {loading ? "正在刷新…" : "刷新记录"}
        </button>
      </div>
      {error && <p role="alert">陪伴记录暂不可用：{error}</p>}
      {data && (
        <>
          <div className="proactive-history-status" aria-live="polite">
            <strong>{data.status.text}</strong>
            {data.status.next_at && (
              <p>
                下一次尝试：{time(data.status.next_at)}
                。到时若模型忙或不便展示，会跳过。
              </p>
            )}
            {data.status.muted_until && (
              <p>安静到：{time(data.status.muted_until)}</p>
            )}
            <p>
              今日桌宠确认展示 {data.status.today_shown} /{" "}
              {data.status.daily_limit} 次 · 时区 {data.status.timezone}
            </p>
          </div>
          <p className="muted">
            保留最近{data.retention_hours}
            小时的自动陪伴记录，不含“试说一句”。气泡约10秒后消失，展示回执不代表你一定看到了。这里的记录不会自动加入聊天。
          </p>
          {data.items.length === 0 ? (
            <p className="proactive-history-empty">
              最近24小时还没有自动陪伴记录。首次需要等待一个完整间隔；恢复展示后会重新计时。
            </p>
          ) : (
            <ol className="proactive-history-list">
              {data.items.map((item) => (
                <li key={item.id}>
                  <div className="proactive-history-meta">
                    <time dateTime={item.created_at}>
                      {time(item.created_at)}
                    </time>
                    <span>{item.topic}</span>
                    <strong>{item.label}</strong>
                  </div>
                  {item.text && (
                    <p className="proactive-history-text">{item.text}</p>
                  )}
                  {item.detail && <p className="muted">{item.detail}</p>}
                  {item.shown_at &&
                    item.text &&
                    item.trigger !== "completion" && (
                      <div>
                        {(
                          [
                            ["more", "喜欢这类话"],
                            ["less", "这个话题少说"],
                            ["normal", "恢复默认偏好"],
                          ] as const
                        ).map(([preference, label]) => (
                          <button
                            key={preference}
                            onClick={() =>
                              void api<{ revision: number }>("/client/state")
                                .then((prefs) =>
                                  api(`/client/proactive/${item.id}/feedback`, {
                                    method: "POST",
                                    body: JSON.stringify({
                                      preference,
                                      expected_revision: prefs.revision,
                                    }),
                                  }),
                                )
                                .then(() =>
                                  window.dispatchEvent(
                                    new Event("client-change"),
                                  ),
                                )
                                .catch((e) => setError(e.message))
                            }
                          >
                            {label}
                          </button>
                        ))}
                      </div>
                    )}
                  {item.shown_at && (
                    <small className="muted">
                      展示回执：{time(item.shown_at)}
                    </small>
                  )}
                </li>
              ))}
            </ol>
          )}
          {data.total > data.items.length && (
            <p className="muted">
              已显示 {data.items.length} / {data.total} 条
              {limit < 200 && (
                <button
                  onClick={() => setLimit((value) => Math.min(200, value + 30))}
                >
                  显示更多记录
                </button>
              )}
            </p>
          )}
          <small className="muted">
            检查于 {time(data.status.checked_at)} · 页面可见时每10秒更新
          </small>
        </>
      )}
    </section>
  );
}
