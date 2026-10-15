import { useEffect, useState } from "react";
import { Bell, Check, Clock3, Plus } from "lucide-react";
import { api, post } from "../api";
import type { Project, Reminder, Todo } from "../api";

const repeatNames: Record<string, string> = { once: "一次性", daily: "每天", weekdays: "周一至周五", weekly: "每周指定星期" };
export function localInput(iso: string, timezone: string) {
  const parts = new Intl.DateTimeFormat("sv-SE", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(iso));
  return parts.replace(" ", "T");
}
function localToISO(value: string, timezone: string) {
  const target = Date.parse(value + ":00Z");
  let candidate = target;
  for (let i = 0; i < 4; i++) {
    const shown = Date.parse(localInput(new Date(candidate).toISOString(), timezone) + ":00Z");
    const difference = target - shown;
    if (!difference) return new Date(candidate).toISOString();
    candidate += difference;
  }
  throw new Error("该时间在所选时区不存在，请选择有效时间。");
}
export function reminderAction(item: Reminder, action: "complete" | "snooze" | "dismiss") {
  return post<Reminder>(`/reminders/${item.id}/action`, { action, expected_revision: item.revision }).then((result) => {
    window.dispatchEvent(new CustomEvent("client-change", { detail: { kind: "reminder" } }));
    return result;
  });
}
export function openReminder(item: Reminder) {
  if (!item.conversation_id) return;
  sessionStorage.setItem("workmemory-conversation", item.conversation_id);
  if (item.todo_id && item.todo_revision) sessionStorage.setItem("workmemory-object", JSON.stringify({ kind: "todo", id: item.todo_id, revision: item.todo_revision }));
  window.dispatchEvent(new CustomEvent("open-reminder", { detail: item }));
}
export function ReminderList({ items, onOpen, onChange }: { items: Reminder[]; onOpen: (r: Reminder) => void; onChange?: () => void }) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  async function act(item: Reminder, action: "complete" | "snooze" | "dismiss") {
    setBusy(item.id); setError("");
    try { await reminderAction(item, action); onChange?.(); }
    catch (e) { setError((e as Error).message); onChange?.(); }
    finally { setBusy(""); }
  }
  return <section className="reminder-list" aria-label="到点提醒">
    {error && <p role="alert">{error}</p>}
    {!items.length && <p className="muted">现在没有待处理提醒。</p>}
    {items.map((r) => <article className="scheduled-reminder" key={r.id}>
      <span className="eyebrow"><Bell size={13} />{r.kind === "todo" ? "小猫的提醒" : r.kind === "daily" ? "每日回顾" : "项目提醒"}</span>
      <p className="preserve-lines">{r.text}</p>
      <div className="schedule-buttons">
        {r.conversation_id && <button onClick={() => onOpen(r)}>打开对话</button>}
        {r.kind === "todo" && <><button disabled={!!busy} onClick={() => void act(r, "complete")}><Check size={14} />完成{r.todo_id ? "本次" : ""}</button><button disabled={!!busy} onClick={() => void act(r, "snooze")}><Clock3 size={14} />10 分钟后提醒</button></>}
        <button className="text-button" disabled={!!busy} onClick={() => void act(r, "dismiss")}>知道了</button>
      </div>
    </article>)}
  </section>;
}

export default function ScheduledTodos({ todos, reminders, refresh, onChat }: { todos: Todo[]; reminders: Reminder[]; refresh: () => Promise<void>; onChat: () => void }) {
  const [editing, setEditing] = useState<Todo | null | undefined>(undefined);
  const [text, setText] = useState("");
  const [when, setWhen] = useState("");
  const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [repeat, setRepeat] = useState("once");
  const [weekdays, setWeekdays] = useState<number[]>([]);
  const [notify, setNotify] = useState(true);
  const [petEnabled, setPetEnabled] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState("");
  const [tab, setTab] = useState("active");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [requestId, setRequestId] = useState(() => crypto.randomUUID());
  useEffect(() => {
    const load = () => void api<{ timezone: string; reminders_enabled: boolean }>("/client/state").then((s) => { setTimezone(s.timezone || "Asia/Shanghai"); setPetEnabled(s.reminders_enabled); }).catch(() => {});
    load();
    window.addEventListener("client-change", load);
    void api<Project[]>("/projects").then(setProjects).catch(() => {});
    return () => window.removeEventListener("client-change", load);
  }, []);
  function edit(item: Todo | null) {
    setEditing(item); setText(item?.text || ""); setError(""); setRepeat(item?.repeat || "once");
    const tz = item?.timezone || timezone; setTimezone(tz);
    setWhen(item?.due_at ? localInput(item.due_at, tz) : ""); setWeekdays(item?.weekdays || []);
    setNotify(item?.notify_pet ?? true); setProject(item?.project_id || ""); setRequestId(crypto.randomUUID());
  }
  async function save() {
    setBusy(true); setError("");
    try {
      if (!when || !text.trim()) throw new Error("请填写事项和具体提醒时间。");
      const schedule = { text: text.trim(), due_at: localToISO(when, timezone), timezone, repeat, weekdays: repeat === "weekly" ? weekdays : [], notify_pet: notify };
      if (editing) await api(`/todos/${editing.id}`, { method: "PATCH", body: JSON.stringify({ ...schedule, expected_revision: editing.revision, paused: false, done: false }) });
      else await post("/todos", { ...schedule, request_id: requestId, project_id: project || null });
      setEditing(undefined); if (notify) setPetEnabled(true); await refresh();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function change(item: Todo, fields: Record<string, unknown>) {
    setBusy(true); setError("");
    try { await api(`/todos/${item.id}`, { method: "PATCH", body: JSON.stringify({ ...fields, expected_revision: item.revision }) }); await refresh(); }
    catch (e) { setError((e as Error).message); await refresh(); }
    finally { setBusy(false); }
  }
  const filtered = todos.filter((t) => tab === "completed" ? t.done : tab === "paused" ? t.paused && !t.done : !t.done && !t.paused);
  return <div className="todo-layout schedule-layout">
    <section className="panel">
      <div className="panel-heading"><div><h2>定时事项</h2><p className="muted">你来设置时间，小猫到点提醒。</p></div><button className="primary" onClick={() => edit(null)}><Plus size={16} />设置提醒</button></div>
      {error && <p role="alert" className="agent-error">{error}</p>}
      {editing !== undefined && <form className="schedule-form" aria-label="设置提醒" onSubmit={(e) => { e.preventDefault(); void save(); }} onChange={() => setRequestId(crypto.randomUUID())}>
        <label>提醒我做什么<input aria-label="事项内容" maxLength={1000} required value={text} onChange={(e) => setText(e.target.value)} placeholder="例如：写周报" /></label>
        <div className="schedule-fields"><label>日期和时间<input aria-label="提醒时间" type="datetime-local" required value={when} onChange={(e) => setWhen(e.target.value)} /></label><label>时区<input aria-label="提醒时区" required value={timezone} onChange={(e) => setTimezone(e.target.value)} /></label></div>
        <label>重复<select aria-label="重复规则" value={repeat} onChange={(e) => setRepeat(e.target.value)}>{Object.entries(repeatNames).map(([key,label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        {repeat === "weekly" && <div className="schedule-weekdays">{[1,2,3,4,5,6,7].map((day) => <label key={day}><input type="checkbox" checked={weekdays.includes(day)} onChange={(e) => setWeekdays((old) => e.target.checked ? [...old,day].sort() : old.filter((d) => d !== day))} />周{"一二三四五六日"[day-1]}</label>)}</div>}
        {!editing && <label>所属项目<select aria-label="提醒项目" value={project} onChange={(e) => setProject(e.target.value)}><option value="">不指定项目</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>}
        <label className="schedule-check"><input type="checkbox" checked={notify} onChange={(e) => setNotify(e.target.checked)} />通过小猫提醒</label>
        <p className="muted">小猫提醒通道当前{petEnabled ? "已开启" : "已关闭"}。{notify && !petEnabled ? "保存时开启。" : ""}电脑休眠或应用关闭期间，恢复后补报；全屏隐藏时保留未读。</p>
        <div className="schedule-buttons"><button className="primary" disabled={busy}>保存提醒</button><button type="button" disabled={busy} onClick={() => setEditing(undefined)}>取消</button></div>
      </form>}
      <div className="schedule-tabs" role="tablist" aria-label="事项状态">{[["active","待提醒"],["paused","已暂停"],["completed","已完成"]].map(([id,label]) => <button key={id} role="tab" aria-selected={tab===id} onClick={() => setTab(id)}>{label}</button>)}</div>
      {!filtered.length && <p className="schedule-empty">{tab === "active" ? "还没有定时事项。设置一个时间，让小猫来提醒你。" : "这里暂时没有事项。"}</p>}
      {filtered.map((t) => <article className="scheduled-todo" key={t.id}><div><strong>{t.text}</strong><p className="muted">{t.due_at ? localInput(t.due_at,t.timezone).replace("T"," ") : "未设置时间"} · {t.timezone}<br />{repeatNames[t.repeat]}{t.repeat === "weekly" ? "：" + t.weekdays.map((d) => "周"+"一二三四五六日"[d-1]).join("、") : ""}{t.paused ? " · 已暂停" : ""}</p></div>
        <div className="schedule-buttons"><button disabled={busy} onClick={() => edit(t)}>{t.paused || t.done ? "重新设置" : "修改"}</button>{!t.paused && !t.done && <button disabled={busy} onClick={() => void change(t,{paused:true})}>暂停</button>}{t.repeat === "once" && !t.done && <button disabled={busy} onClick={() => void change(t,{done:true})}>完成事项</button>}<button disabled={busy} onClick={() => { sessionStorage.setItem("workmemory-object", JSON.stringify({kind:"todo",id:t.id,revision:t.revision})); onChat(); }}>交给助手</button><button className="text-button" disabled={busy} onClick={() => { if (confirm(`取消“${t.text}”的整个提醒日程？`)) void change(t,{dismissed:true}); }}>取消日程</button></div>
      </article>)}
    </section>
    <section className="panel"><div className="panel-heading"><h2>到点提醒</h2><span className="count">{reminders.length}</span></div><ReminderList items={reminders} onChange={() => void refresh()} onOpen={(r) => { openReminder(r); onChat(); }} /></section>
  </div>;
}
