import { useEffect, useState } from "react";
import { api, post } from "../api";

export interface Operation {
  id: string;
  action: string;
  status: string;
  revision: number;
  preview: {
    label: string;
    count?: number;
    before: Record<string, unknown>[];
    after: Record<string, unknown>;
    notice: string;
  } | null;
  result: { label: string; notice?: string } | null;
}
const names: Record<string, string> = {
  name: "名称",
  text: "内容",
  summary: "摘要",
  title: "标题",
  category: "分类",
  episode_count: "保留的工作记录数",
  todo_count: "保留的待办数",
  memory_count: "保留的确认记忆数",
  aliases: "项目别名",
  due_at: "提醒时间",
  timezone: "时区", repeat: "重复规则", weekdays: "星期", paused: "暂停整个日程", notify_pet: "通过小猫提醒",
  done: "已完成",
  dismissed: "已忽略",
};
function displayValue(key: string, value: unknown, data: Record<string, unknown>) {
  if (key === "aliases" && Array.isArray(value)) return value.join("、") || "无";
  if (key === "due_at") {
    if (!value) return "未设置时间";
    const date = new Date(String(value));
    if (!Number.isNaN(date.getTime())) {
      try { return new Intl.DateTimeFormat("zh-CN", {dateStyle:"full", timeStyle:"short", timeZone:String(data.timezone || "Asia/Shanghai")}).format(date); } catch { /* Keep source time if zone is invalid. */ }
    }
  }
  if (key === "weekdays" && Array.isArray(value)) return value.length ? value.map(d => `周${"一二三四五六日"[Number(d)-1]}`).join("、") : "按重复规则";
  if (key === "repeat") return ({once:"一次性",daily:"每天",weekdays:"周一至周五",weekly:"每周指定星期"}[String(value)] || String(value));
  return typeof value === "boolean" ? (value ? "是" : "否") : String(value);
}
function values(data: Record<string, unknown>) {
  return Object.entries(data)
    .filter(([k, v]) => k in names && (v !== null || k === "due_at"))
    .map(([k, v]) => (
      <p key={k}>
        <span className="muted">{names[k]}：</span>
        {displayValue(k, v, data)}
      </p>
    ));
}
export default function OperationCards({
  conversation,
}: {
  conversation: string;
}) {
  const [items, setItems] = useState<Operation[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  useEffect(() => {
    let closed = false;
    const load = () =>
      void api<{ items: Operation[] }>(
        `/operations?conversation_id=${conversation}`,
      )
        .then((v) => {
          if (!closed) setItems(v.items);
        })
        .catch((e) => {
          if (!closed) setError(e.message);
        });
    load();
    window.addEventListener("client-change", load);
    return () => {
      closed = true;
      window.removeEventListener("client-change", load);
    };
  }, [conversation]);
  async function decide(item: Operation, action: "confirm" | "reject") {
    setBusy(item.id);
    try {
      const result = await post<Operation>(`/operations/${item.id}/decision`, {
        action,
        expected_revision: item.revision,
      });
      setItems((old) => old.map((i) => (i.id === result.id ? result : i)));
      window.dispatchEvent(
        new CustomEvent("client-change", { detail: { kind: "operation" } }),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }
  return (
    <section className="operation-list" aria-label="应用内操作">
      {error && <p role="alert">{error}</p>}
      {items.map((item) => (
        <article className="operation-card" key={item.id}>
          {item.preview ? (
            <>
              <h3>{item.preview.label}</h3>
              {!!item.preview.before.length && (
                <details open={["todo.delete", "todo.delete_many", "project.delete", "project.delete_many"].includes(item.action)}>
                  <summary>查看修改前的内容与影响范围</summary>
                  <div className={item.action.endsWith(".delete_many") ? "operation-targets" : undefined}>
                  {item.preview.before.map((v, i) => (
                    <div key={String(v.id ?? i)} className="operation-target">
                      {item.action.endsWith(".delete_many") && <strong>第 {i + 1} 项</strong>}
                      {values(v)}
                      {Array.isArray(v.records) && (
                        <p>影响 {v.records.length} 条项目记录</p>
                      )}
                    </div>
                  ))}
                  </div>
                </details>
              )}
              {values(item.preview.after)}
              <p className="muted">{item.preview.notice}</p>
              <button
                className="primary"
                disabled={!!busy}
                onClick={() => void decide(item, "confirm")}
              >
                {item.action === "todo.delete_many" ? `确认删除 ${item.preview.count} 条待办` : item.action === "project.delete_many" ? `确认删除 ${item.preview.count} 个项目` : "确认执行"}
              </button>
              <button
                disabled={!!busy}
                onClick={() => void decide(item, "reject")}
              >
                拒绝
              </button>
            </>
          ) : (
            <p>
              {item.status === "executed"
                ? `已执行：${item.result?.label || "操作"}`
                : item.status === "rejected"
                  ? "已拒绝操作"
                  : "操作已到期或来源发生变化，请重新提出。"}
              {item.result?.notice && <span className="muted">{item.result.notice}</span>}
            </p>
          )}
        </article>
      ))}
    </section>
  );
}
