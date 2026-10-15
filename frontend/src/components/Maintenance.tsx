import { useEffect, useState } from "react";
import { api, post } from "../api";

type Data = {
  database_bytes: number;
  backup_bytes: number;
  schedule: string;
  policy: { enabled: boolean; revision: number };
  backends: Record<string, { ready: boolean; retry_at: string | null }>;
  backups: {
    id: string;
    created_at: string;
    size: number;
    automatic: boolean;
    verification: { verified_at: string } | null;
  }[];
  tasks: {
    id: string;
    kind: string;
    status: string;
    error: string | null;
    created_at: string;
  }[];
};
const size = (value: number) => (value / 1024 / 1024).toFixed(1) + " MB";
const time = (value: string) => new Date(value).toLocaleString("zh-CN");
export default function Maintenance() {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = () =>
    api<Data>("/maintenance")
      .then(setData)
      .catch((e) => setError(e.message));
  useEffect(() => {
    void load();
    const timer = setInterval(() => {
      if (!document.hidden) void load();
    }, 10000);
    return () => clearInterval(timer);
  }, []);
  async function action(
    path: string,
    body: unknown = { request_id: crypto.randomUUID() },
  ) {
    setBusy(true);
    setError("");
    try {
      await post(path, body);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel settings-card">
      <h2>备份与维护</h2>
      {error && <p role="alert">{error}</p>}
      {data && (
        <>
          <p>
            数据库 {size(data.database_bytes)} · 备份 {size(data.backup_bytes)}
          </p>
          <p>{data.schedule}</p>
          <label>
            <input
              type="checkbox"
              checked={data.policy.enabled}
              disabled={busy}
              onChange={(e) => {
                setBusy(true);
                void api("/maintenance/policy", {
                  method: "PATCH",
                  body: JSON.stringify({
                    enabled: e.target.checked,
                    expected_revision: data.policy.revision,
                  }),
                })
                  .then(load)
                  .catch((e) => setError(e.message))
                  .finally(() => setBusy(false));
              }}
            />
            每天自动备份
          </label>
          <div className="agent-toolbar">
            <button
              disabled={busy}
              onClick={() => void action("/maintenance/backups")}
            >
              立即备份
            </button>
            <button
              onClick={() =>
                void action("/capture/actions", { action: "backup_folder" })
              }
            >
              打开备份目录
            </button>
          </div>
          <p>
            最近成功备份：
            {data.backups[0] ? time(data.backups[0].created_at) : "尚无备份"}
          </p>
          <details>
            <summary>备份文件与验证</summary>
            {data.backups.map((item) => (
              <div key={item.id} className="source-excerpt">
                <strong>{time(item.created_at)}</strong> · {size(item.size)} ·{" "}
                {item.automatic ? "自动" : "手动或升级备份"}
                <p>
                  {item.verification
                    ? `独立恢复验证通过：${time(item.verification.verified_at)}`
                    : "尚未进行独立恢复验证"}
                </p>
                <button
                  disabled={busy}
                  onClick={() =>
                    void action(`/maintenance/backups/${item.id}/verify`)
                  }
                >
                  验证可恢复性
                </button>
              </div>
            ))}
          </details>
          <details>
            <summary>最近维护任务</summary>
            {data.tasks.map((task) => (
              <p key={task.id}>
                {time(task.created_at)} ·{" "}
                {task.kind === "backup" ? "备份" : "恢复验证"} ·{" "}
                {
                  (
                    {
                      done: "完成",
                      pending: "等待",
                      running: "进行中",
                      failed: "失败",
                      superseded: "已合并到最近一次备份",
                    } as Record<string, string>
                  )[task.status]
                }
                {task.error && "；未成功，请检查磁盘空间和数据库状态后重试"}
              </p>
            ))}
          </details>
          {Object.entries(data.backends).map(([name, state]) => (
            <p key={name}>
              {name}：
              {state.ready
                ? "最近连接成功"
                : `等待恢复${state.retry_at ? "，下次检查 " + time(state.retry_at) : ""}`}{" "}
              <button
                disabled={busy}
                onClick={() =>
                  void action(`/maintenance/backends/${name}/retry`, {})
                }
              >
                立即重试
              </button>
            </p>
          ))}
          <details>
            <summary>恢复与迁移指引</summary>
            <ol>
              <li>
                先在这里验证选定备份可恢复，再保留一份当前数据的手动备份。
              </li>
              <li>
                从拾忆管理入口停止服务，保留当前数据目录，使用与备份结构匹配的代码进行恢复。完整步骤见项目
                docs/management.md。
              </li>
              <li>
                迁移机器还需安全保存配置、凭据、皮肤与项目文件；这些不包含在数据库备份中。系统权限需要在新机器重新授予。
              </li>
            </ol>
            <p>
              本页的恢复验证只使用独立临时库，不替换当前数据。备份保存创建时的历史内容，业务记录删除不会改写旧备份。
            </p>
          </details>
        </>
      )}
    </section>
  );
}
