import brandIcon from "./assets/brand.svg";
import CaptureHealth from "./components/CaptureHealth";
import LegacyHistory from "./components/LegacyHistory";
import ScheduledTodos from "./components/ScheduledTodos";
import { useCallback, useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  ArrowDown,
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  Check,
  CheckCheck,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Database,
  FileText,
  FolderClosed,
  Leaf,
  LoaderCircle,
  LockKeyhole,
  LogOut,
  MessageCircle,
  Plus,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { api, getToken, post, DAILY_WORK_LABEL, DAILY_WORK_SCOPE, workScopeQuery } from "./api";
import MCPSettings from "./components/MCPSettings";
import ModelSettings from "./ModelSettings";
import SettingsLayout from "./components/SettingsLayout";
import CompanionSettings from "./components/CompanionSettings";
import Maintenance from "./components/Maintenance";
import MemoryReview from "./components/MemoryReview";
import SourceMemory from "./components/SourceMemory";
import ProjectTools from "./components/ProjectTools";
import AgentChat from "./components/AgentChat";
import ArtifactPanel from "./components/ArtifactPanel";
import EvidenceSearch from "./components/EvidenceSearch";
import SourceNeighbors from "./components/SourceNeighbors";
import type { DailyWorkGroup, Episode, Project, Reminder, Sources, Status, Todo } from "./api";

type Page = "timeline" | "projects" | "chat" | "review" | "todos" | "settings";
const nav: { id: Page; label: string; icon: typeof Clock3 }[] = [
  { id: "timeline", label: "工作时间线", icon: Clock3 },
  { id: "projects", label: "项目空间", icon: FolderClosed },
  { id: "chat", label: "问问记忆", icon: MessageCircle },
  { id: "review", label: "每日回顾", icon: BookOpen },
  { id: "todos", label: "待办与提醒", icon: CheckCheck },
];
const fmt = (
  value: string,
  options: Intl.DateTimeFormatOptions = {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  },
) => new Date(value).toLocaleString("zh-CN", options);
const dateInput = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
function Empty({
  icon: Icon = Leaf,
  title,
  children,
}: {
  icon?: typeof Leaf;
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-icon">
        <Icon size={27} />
      </div>
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}
function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  useEffect(() => {
    const close = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [onClose]);
  return (
    <div className="overlay" onClick={onClose}>
      <section
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="modal"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="icon-button" onClick={onClose} aria-label="关闭">
            <X />
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}

export default function App() {
  const [authenticated, setAuthenticated] = useState(!!getToken());
  const [token, setToken] = useState("");
  const [page, setPage] = useState<Page>("timeline");
  const [status, setStatus] = useState<Status | null>(null);
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [projects, setProjects] = useState<Project[]>([]);
  const [dailyWork, setDailyWork] = useState<DailyWorkGroup | null>(null);
  const [todos, setTodos] = useState<Todo[]>([]);
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [project, setProject] = useState("");
  const [day, setDay] = useState(dateInput());
  const [briefProject, setBriefProject] = useState<Project | null>(null);
  const [search, setSearch] = useState("");
  const [searchItems, setSearchItems] = useState<Episode[] | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [note, setNote] = useState("");
  const [noteProject, setNoteProject] = useState("");
  const [source, setSource] = useState<Sources | null>(null);
  const [edit, setEdit] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deletePreview, setDeletePreview] = useState<{revision: number; memory_hash: string; memories: {id: string; text: string}[]} | null>(null);
  const [reviewPeriod, setReviewPeriod] = useState("daily");

  useEffect(() => {
    const expired = () => setAuthenticated(false);
    window.addEventListener("auth-expired", expired);
    return () => window.removeEventListener("auth-expired", expired);
  }, []);
  const refresh = useCallback(async () => {
    if (!authenticated) return;
    const results = await Promise.allSettled([
      api<Status>("/status"),
      page === "timeline"
        ? api<{ items: Episode[]; total: number }>(
            `/timeline?limit=20&offset=${offset}&${workScopeQuery(project)}`,
          )
        : Promise.resolve(null),
      ["timeline", "projects"].includes(page)
        ? api<Project[]>("/projects")
        : Promise.resolve(null),
      page === "todos" ? api<Todo[]>("/todos") : Promise.resolve(null),
      api<Reminder[]>("/reminders"),
      ["timeline", "projects"].includes(page)
        ? api<DailyWorkGroup>("/work-groups/daily")
        : Promise.resolve(null),
    ]);
    if (results[0].status === "fulfilled") setStatus(results[0].value);
    if (results[1].status === "fulfilled" && results[1].value) {
      setEpisodes(results[1].value.items);
      setTotal(results[1].value.total);
    }
    if (results[2].status === "fulfilled" && results[2].value)
      setProjects(results[2].value);
    if (results[3].status === "fulfilled" && results[3].value)
      setTodos(results[3].value);
    if (results[4].status === "fulfilled") setReminders(results[4].value);
    if (results[5].status === "fulfilled" && results[5].value)
      setDailyWork(results[5].value);
    const failed = results.find((r) => r.status === "rejected");
    if (failed?.status === "rejected")
      setError(
        failed.reason instanceof Error
          ? failed.reason.message
          : "无法连接本地服务",
      );
  }, [authenticated, offset, project, page]);
  useEffect(() => {
    const changed = (event: Event) => {
      const kind = (event as CustomEvent).detail?.kind;
      if (
        ["changed", "operation", "invalidated", "reset", "todo", "reminder"].includes(kind) &&
        !document.hidden
      )
        void refresh();
      if (["changed", "operation", "invalidated", "reset", "todo", "reminder"].includes(kind))
        setSource(null);
    };
    window.addEventListener("client-change", changed);
    return () => window.removeEventListener("client-change", changed);
  }, [refresh]);
  useEffect(() => {
    setLoading(true);
    void refresh().finally(() => setLoading(false));
    const timer = setInterval(() => {
      if (!document.hidden) void refresh();
    }, 10000);
    return () => clearInterval(timer);
  }, [refresh]);
  async function run(action: () => Promise<unknown>, message = "") {
    setBusy(true);
    setError("");
    try {
      await action();
      if (message) setNotice(message);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  async function viewSource(id: string) {
    setEdit(null);
    setDeleting(false);
    await run(async () =>
      setSource(await api<Sources>(`/episodes/${id}/sources`)),
    );
  }
  function changePage(next: Page) {
    setPage(next);
    setError("");
    setSearchItems(null);
    setSearch("");
  }
  function selectWorkGroup(value: string) {
    setProject(value);
    setOffset(0);
    changePage("timeline");
  }
  async function login(e: FormEvent) {
    e.preventDefault();
    sessionStorage.setItem("workmemory-token", token.trim());
    try {
      await api("/status");
      setAuthenticated(true);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function addNote(e: FormEvent) {
    e.preventDefault();
    await run(async () => {
      await post("/ingest", {
        events: [
          {
            id: crypto.randomUUID(),
            device_id: "web-manual",
            captured_at: new Date().toISOString(),
            app: "手动记录",
            title: noteProject || "工作笔记",
            text: note,
            method: "manual",
            project_hint: noteProject.trim() || null,
          },
        ],
      });
      setAdding(false);
      setNote("");
      setNoteProject("");
    }, "记录已保存，正在整理记忆。");
  }
  async function searchMemory(e: FormEvent) {
    e.preventDefault();
    if (!search.trim()) {
      setSearchItems(null);
      return;
    }
    await run(async () => {
      const result = await api<{ items: Episode[]; warning?: string }>(
        "/memory/search?q=" + encodeURIComponent(search) + "&" + workScopeQuery(project),
      );
      setSearchItems(result.items);
      if (result.warning) setNotice(result.warning);
    });
  }
  function card(item: Episode, index: number) {
    return (
      <article className="episode" key={item.id}>
        <div className="episode-time">
          <span>
            {fmt(item.started_at, { hour: "2-digit", minute: "2-digit" })}
          </span>
          <small>
            {fmt(item.started_at, { month: "numeric", day: "numeric" })}
          </small>
        </div>
        <div className="episode-line">
          <span />
        </div>
        <div className="episode-body">
          <div className="episode-meta">
            <span className={"project-tag color-" + (index % 3)}>
              <FolderClosed size={12} />
              {item.project || DAILY_WORK_LABEL}
            </span>
            {item.corrected && <span className="subtle">已纠正</span>}
            <span className="subtle">
              {item.evidence_label || (item.status === "pending"
                ? "正在整理"
                : item.status === "expired"
                  ? "原文已到期"
                  : "工作片段")}
            </span>
          </div>
          <p>
            {item.summary || (item.evidence_state === "no_evidence"
              ? "已保留原文，未找到足够明确的摘录，不生成自动记忆。"
              : item.evidence_state === "legacy_unverified"
                ? "旧自动摘要保留在隔离历史中，复核前不参与回答。"
                : item.evidence_state === "expired" || item.status === "expired"
                  ? "原文与未确认的自动摘录已到期。"
                  : "原文已保存，等待选择有来源支撑的完整原句。")}
          </p>
          <button
            className="text-button"
            onClick={() => void viewSource(item.id)}
          >
            <FileText size={14} />
            查看来源 <ArrowUpRight size={13} />
          </button>
        </div>
      </article>
    );
  }

  if (!authenticated)
    return (
      <div className="login-shell">
        <form className="login" onSubmit={login}>
          <div className="brand-icon">
            <img src={brandIcon} width={44} height={44} alt="拾忆" />
          </div>
          <h1>拾忆</h1>
          <p>让每一段工作，都有迹可循。</p>
          <label>
            本机访问令牌
            <input
              type="password"
              autoComplete="off"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              required
              placeholder="粘贴 config/secrets.env 中的 API_TOKEN"
            />
          </label>
          <button className="primary" type="submit">
            进入我的工作空间 <ArrowRight size={16} />
          </button>
          {error && <p className="error-text">{error}</p>}
          <small>
            <LockKeyhole size={13} /> 记忆留在这台 Mac 上
          </small>
          <p className="login-help">
            也可以运行 <code>scripts/open-ui.sh</code> 自动登录。
          </p>
        </form>
      </div>
    );
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            changePage("timeline");
          }}
        >
          <span className="brand-icon">
            <img src={brandIcon} width={38} height={38} alt="" />
          </span>
          <strong>
            拾忆<span>WORK MEMORY</span>
          </strong>
        </a>
        <div className="workspace-label">
          我的工作空间 <span>LOCAL</span>
        </div>
        <nav>
          {nav.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              className={page === id ? "nav-item active" : "nav-item"}
              onClick={() => changePage(id)}
            >
              <Icon size={18} />
              {label}
              {id === "todos" &&
                todos.filter((t) => t.confirmed && !t.done).length > 0 && (
                  <span className="nav-count">
                    {todos.filter((t) => t.confirmed && !t.done).length}
                  </span>
                )}
            </button>
          ))}
        </nav>
        <div className="sidebar-note">
          <div>
            <ShieldCheck size={18} />
            <strong>只属于你的记忆</strong>
          </div>
          <p>
            本地采集，自选模型整理。
            <br />
            带着证据，找回工作的上下文。
          </p>
          <span>
            <i className="dot" /> 云端回退已关闭
          </span>
        </div>
        <button
          className={page === "settings" ? "nav-item active" : "nav-item"}
          onClick={() => changePage("settings")}
        >
          <Settings2 size={18} />
          偏好与状态
        </button>
        <div className="profile">
          <span>我</span>
          <div>
            <strong>本地工作空间</strong>
            <small>私人 · 此设备</small>
          </div>
          <button
            className="icon-button"
            title="退出登录"
            onClick={() => {
              sessionStorage.removeItem("workmemory-token");
              setAuthenticated(false);
            }}
          >
            <LogOut size={16} />
          </button>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <div>
            <span>我的工作空间</span>
            <ChevronRight size={13} />
            <strong>
              {nav.find((n) => n.id === page)?.label || "偏好与状态"}
            </strong>
          </div>
          <span className="private-label">
            <LockKeyhole size={13} />
            本机工作空间
          </span>
        </header>
        <div className={`content ${page === "chat" ? "chat-page" : ""}`}>
          <CaptureHealth status={status} />
          {error && (
            <div role="alert" className="banner error">
              {error}
              <button onClick={() => setError("")} aria-label="关闭错误">
                <X size={16} />
              </button>
            </div>
          )}
          {notice && (
            <div role="status" className="banner">
              {notice}
              <button onClick={() => setNotice("")} aria-label="关闭通知">
                <X size={16} />
              </button>
            </div>
          )}
          <div className="page-heading">
            <div>
              <div className="eyebrow">
                {fmt(new Date().toISOString(), {
                  year: "numeric",
                  month: "long",
                  day: "numeric",
                  weekday: "long",
                })}
              </div>
              <h1>
                {
                  {
                    timeline: "工作脉络，一目了然。",
                    projects: "让项目接着往前走。",
                    chat: "想不起来的，问问记忆。",
                    review: "回看今天，轻装向前。",
                    todos: "记下约定，从容推进。",
                    settings: "你的记忆，由你掌握。",
                  }[page]
                }
              </h1>
              <p>
                {
                  {
                    timeline: "把散落的工作片段，整理成可以找回的记忆。",
                    projects: "没有明确项目的记录归入日常工作，也可以按项目继续整理。",
                    chat: "找回决定、进展和细节，每个回答都有来源。",
                    review: "看看完成了什么，以及下一步要去哪里。",
                    todos: "只有经你确认的待办，才会产生提醒。",
                    settings: "配置模型，查看服务、记忆同步和数据保留状态。",
                  }[page]
                }
              </p>
            </div>
            {["timeline", "projects"].includes(page) && (
              <button className="primary" onClick={() => setAdding(true)}>
                <Plus size={17} />
                添加记录
              </button>
            )}
          </div>
          {page === "timeline" && (
            <>
              <div className="stats-grid">
                <div className="stat">
                  <div>
                    <span>已保存片段</span>
                    <Clock3 size={17} />
                  </div>
                  <strong>
                    {total}
                    <small>段</small>
                  </strong>
                  <p>每段记忆，都可以回到来源</p>
                </div>
                <div className="stat">
                  <div>
                    <span>工作分类</span>
                    <FolderClosed size={17} />
                  </div>
                  <strong>
                    {projects.length + 1}
                    <small>个</small>
                  </strong>
                  <p>日常工作与明确项目分别整理</p>
                </div>
                <div className="stat accent-stat">
                  <div>
                    <span>记忆同步</span>
                    <Sparkles size={17} />
                  </div>
                  <strong>
                    {status?.sync.synced ?? 0}
                    <small>/ {status?.sync.total ?? 0} 段</small>
                  </strong>
                  <p>
                    <i
                      className={
                        "dot " + (!status?.ollama.models_ready ? "amber" : "")
                      }
                    />
                    {status?.ollama.models_ready
                      ? "生成与嵌入服务已就绪"
                      : "等待生成或嵌入服务就绪"}
                  </p>
                </div>
              </div>
              <div className="timeline-layout">
                <section className="panel timeline-panel">
                  <div className="panel-heading">
                    <h2>
                      工作时间线 <span className="count">{total}</span>
                    </h2>
                    <select
                      aria-label="筛选项目"
                      value={project}
                      onChange={(e) => selectWorkGroup(e.target.value)}
                    >
                      <option value="">全部工作</option>
                      <option value={DAILY_WORK_SCOPE}>{DAILY_WORK_LABEL}（默认分类）</option>
                      {projects.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}
                        </option>
                      ))}
                    </select>
                  </div>
                  <form className="search-box" onSubmit={searchMemory}>
                    <Search size={16} />
                    <input
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                      placeholder="搜索记忆中的项目、决策或关键词…"
                      aria-label="搜索记忆"
                    />
                    <button type="submit" disabled={busy}>
                      搜索
                    </button>
                    {searchItems && (
                      <button
                        type="button"
                        onClick={() => {
                          setSearchItems(null);
                          setSearch("");
                        }}
                        aria-label="清除搜索"
                      >
                        <X size={15} />
                      </button>
                    )}
                  </form>
                  <LegacyHistory />
                  <EvidenceSearch
                    projects={projects}
                    onSource={(id) => void viewSource(id)}
                  />
                  {loading ? (
                    <div className="loading">
                      <LoaderCircle className="spin" />
                      正在读取工作记录
                    </div>
                  ) : (searchItems ?? episodes).length ? (
                    (searchItems ?? episodes).map(card)
                  ) : (
                    <Empty
                      title={
                        searchItems ? "还没找到相关记录" : "从一段工作记录开始"
                      }
                    >
                      点击“添加记录”写下进展，或启动菜单栏采集，
                      <br />
                      你的工作脉络会慢慢在这里生长。
                    </Empty>
                  )}
                  {!searchItems && total > 20 && (
                    <div className="pagination">
                      <button
                        disabled={offset === 0}
                        onClick={() => setOffset((v) => Math.max(0, v - 20))}
                      >
                        <ChevronLeft size={16} />
                        上一页
                      </button>
                      <span>
                        {offset + 1}–{Math.min(total, offset + 20)} / {total}
                      </span>
                      <button
                        disabled={offset + 20 >= total}
                        onClick={() => setOffset((v) => v + 20)}
                      >
                        下一页
                        <ChevronRight size={16} />
                      </button>
                    </div>
                  )}
                </section>
                <aside className="right-column">
                  <div className="memory-tip">
                    <Sparkles size={23} />
                    <h3>把上下文找回来</h3>
                    <p>
                      “上次这个项目进行到哪里了？”
                      <br />
                      “为什么调整了上线时间？”
                      <br />
                      让记忆替你翻找那些细节。
                    </p>
                    <button onClick={() => changePage("chat")}>
                      问问我的记忆
                      <ArrowRight size={16} />
                    </button>
                    <div className="tip-orbits">
                      <span />
                      <span />
                      <span />
                    </div>
                  </div>
                  <div className="panel project-preview">
                    <div className="panel-heading">
                      <h3>工作分类</h3>
                      <FolderClosed size={17} />
                    </div>
                    <button onClick={() => selectWorkGroup(DAILY_WORK_SCOPE)}>
                      <span className="project-icon color-0"><FolderClosed size={17} /></span>
                      <span>
                        <strong>{DAILY_WORK_LABEL}</strong>
                        <small>{dailyWork ? `${dailyWork.episodes} 段工作记录` : "正在读取记录"}</small>
                      </span>
                      <ChevronRight size={14} />
                    </button>
                    {projects.slice(0, 4).map((p, i) => (
                      <button
                        key={p.id}
                        onClick={() => selectWorkGroup(p.id)}
                      >
                        <span className={"project-icon color-" + (i % 3)}>
                          <FolderClosed size={17} />
                        </span>
                        <span>
                          <strong>{p.name}</strong>
                          <small>{p.episodes} 段相关记忆</small>
                        </span>
                        <ChevronRight size={14} />
                      </button>
                    ))}
                    {projects.length === 0 && (
                      <p className="muted small">
                        未指定项目的记录归入日常工作，无需先创建项目。
                      </p>
                    )}
                  </div>
                  <div className="quiet-note">
                    <ShieldCheck size={16} />
                    <p>
                      原始文本默认保留 {status?.retention_days ?? 30} 天。
                      <br />
                      未确认摘录随原文到期，确认记忆长期保留。
                    </p>
                  </div>
                </aside>
              </div>
            </>
          )}
          {briefProject && (
            <Modal
              title={briefProject.name + " · 项目上下文"}
              onClose={() => setBriefProject(null)}
            >
              <ArtifactPanel
                path={`/projects/${briefProject.id}/brief`}
                onSource={(id) => {
                  setBriefProject(null);
                  void viewSource(id);
                }}
              />
            </Modal>
          )}
          {page === "projects" && (
            <div className="project-page">
              <ProjectTools onSource={(id) => void viewSource(id)} />
              <div className="project-grid">
              <article className="panel project-card" aria-label="日常工作默认分类">
                <span className="project-icon color-0"><FolderClosed size={23} /></span>
                <span className="project-state">默认分类</span>
                <h2>{DAILY_WORK_LABEL}</h2>
                <p>
                  {dailyWork ? `${dailyWork.episodes} 段工作记录` : "正在读取工作记录"}
                  {dailyWork?.last_active && ` · 最近 ${fmt(dailyWork.last_active, { month: "numeric", day: "numeric" })}`}
                </p>
                <p className="muted">未指定项目的记录自动显示在这里，可以直接查看和回顾。</p>
                <div>
                  <button className="text-button" onClick={() => selectWorkGroup(DAILY_WORK_SCOPE)}>
                    查看日常工作<ArrowRight size={15} />
                  </button>
                  <button className="secondary" onClick={() => { setReviewPeriod("daily"); changePage("review"); }}>每日回顾</button>
                </div>
              </article>
              {projects.map((p, i) => (
                <article className="panel project-card" key={p.id}>
                  <span className={"project-icon color-" + (i % 3)}>
                    <FolderClosed size={23} />
                  </span>
                  <span className="project-state">
                    {p.state === "active"
                      ? "进行中"
                      : p.state === "paused"
                        ? "已暂停"
                        : "已归档"}
                  </span>
                  <h2>{p.name}</h2>
                  <p>
                    {p.episodes} 段工作记忆 · 最近{" "}
                    {fmt(p.last_active, { month: "numeric", day: "numeric" })}
                  </p>
                  <div>
                    <button
                      className="text-button"
                      onClick={() => selectWorkGroup(p.id)}
                    >
                      查看项目记录
                      <ArrowRight size={15} />
                    </button>
                    <button
                      className="secondary"
                      disabled={busy}
                      onClick={() => setBriefProject(p)}
                    >
                      恢复上下文
                    </button>
                  </div>
                </article>
              ))}
              </div>
            </div>
          )}
          {page === "chat" && (
            <AgentChat onSource={(id) => void viewSource(id)} />
          )}
          {page === "review" && (
            <section className="panel review-panel">
              <div className="panel-heading">
                <h2>{reviewPeriod === "weekly" ? "每周回顾" : "每日回顾"}</h2>
                <select
                  aria-label="回顾周期"
                  value={reviewPeriod}
                  onChange={(e) => setReviewPeriod(e.target.value)}
                >
                  <option value="daily">日报</option>
                  <option value="weekly">周报</option>
                </select>
                <input
                  type="date"
                  aria-label="回顾日期"
                  value={day}
                  onChange={(e) => setDay(e.target.value)}
                />
              </div>
              <ArtifactPanel
                path={`/reviews?day=${day}&period=${reviewPeriod}`}
                editable
                onSource={(id) => void viewSource(id)}
              />
            </section>
          )}
          {page === "todos" && <ScheduledTodos todos={todos} reminders={reminders} refresh={refresh} onChat={() => changePage("chat")} />}
          {page === "settings" && (
            <SettingsLayout panels={{
              models: <ModelSettings onSaved={refresh} />,
              pet: <CompanionSettings category="pet" />,
              memory: <>
                <CompanionSettings category="memory" />
                <MemoryReview onSource={id => void viewSource(id)} />
              <section className="panel settings-card">
                <div className="panel-heading">
                  <h2>
                    <Database size={18} />
                    记忆后端
                  </h2>
                  <span className="local-pill">本地运行</span>
                </div>
                <p>切换后会从保留的业务记录补齐历史记忆。</p>
                <div className="backend-options">
                  {(["reme", "graphiti"] as const).map((name) => (
                    <button
                      key={name}
                      disabled={busy}
                      className={status?.backend === name ? "selected" : ""}
                      onClick={() =>
                        void run(
                          () => post("/config/backend", { backend: name }),
                          "后端已切换，历史记忆正在同步。",
                        )
                      }
                    >
                      <strong>{name === "reme" ? "ReMe" : "Graphiti"}</strong>
                      <small>
                        {name === "reme"
                          ? "文件记忆 · 默认"
                          : "图谱记忆 · Neo4j"}
                      </small>
                      {status?.backend === name && <Check size={18} />}
                    </button>
                  ))}
                </div>
                <div className="sync-info">
                  <span>当前同步进度</span>
                  <strong>
                    {status?.sync.synced ?? 0} / {status?.sync.total ?? 0}
                  </strong>
                </div>
                <progress
                  value={status?.sync.synced ?? 0}
                  max={status?.sync.total || 1}
                />
                <p className="muted small">
                  Graphiti 首次使用前运行{" "}
                  <code>scripts/dev-up.sh --graphiti</code>。
                </p>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() =>
                    void run(
                      () => post("/memory/rebuild"),
                      "已安排重建当前后端的记忆。",
                    )
                  }
                >
                  重建记忆索引
                </button>
              </section>
              </>,
              tools: <MCPSettings />,
              maintenance: <Maintenance />,
              status: <>
              <section className="panel settings-card">
                <h2>服务与任务</h2>
                <dl>
                  <div>
                    <dt>业务数据库</dt>
                    <dd>
                      <i className="dot" /> PostgreSQL 17
                    </dd>
                  </div>
                  <div>
                    <dt>生成与嵌入服务</dt>
                    <dd>
                      <i
                        className={
                          "dot " + (!status?.ollama.models_ready ? "amber" : "")
                        }
                      />
                      {status?.ollama.models_ready ? "已就绪" : "未就绪"}
                    </dd>
                  </div>
                  <div>
                    <dt>自动采集</dt>
                    <dd>
                      {!status?.collector?.running
                        ? "未连接"
                        : status.collector.paused
                          ? "已暂停"
                          : status.collector.capture_active === false
                            ? "等待权限或可采集窗口"
                            : "采集中"}
                    </dd>
                  </div>
                  <div>
                    <dt>辅助功能 / 屏幕录制</dt>
                    <dd>
                      {status?.collector?.accessibility_granted
                        ? "已授权"
                        : "未确认"}{" "}
                      /{" "}
                      {status?.collector?.screen_recording_granted
                        ? "已授权"
                        : "未确认"}
                    </dd>
                  </div>
                  <div>
                    <dt>PaddleOCR</dt>
                    <dd>
                      {status?.ocr?.ready
                        ? `已就绪 ${status.ocr.versions?.paddleocr ?? ""}`
                        : "未就绪"}
                    </dd>
                  </div>
                  <div>
                    <dt>OCR 触发方式</dt>
                    <dd>
                      {status?.collector?.ocr_mode === "always"
                        ? "OCR 主采集"
                        : status?.collector?.ocr_mode === "off"
                          ? "已关闭"
                          : "窗口文本不足时"}
                    </dd>
                  </div>
                  <div>
                    <dt>采集触发</dt>
                    <dd>
                      {status?.collector?.capture_mode === "interaction"
                        ? "操作前后 / 页面变化"
                        : `每 ${status?.collector?.interval_seconds ?? 5} 秒一次`}
                    </dd>
                  </div>
                  <div>
                    <dt>输入监控</dt>
                    <dd>
                      {status?.collector?.capture_mode !== "interaction"
                        ? "定时采集无需此权限"
                        : status?.collector?.input_monitoring_granted
                          ? "已授权"
                          : "未确认"}
                    </dd>
                  </div>
                  <div>
                    <dt>待识别 / 待上传</dt>
                    <dd>
                      {status?.collector?.pending_groups ?? 0} /{" "}
                      {status?.collector?.spooled_batches ?? 0}
                    </dd>
                  </div>
                  <div>
                    <dt>合并 / 丢弃操作组</dt>
                    <dd>
                      {status?.collector?.merged_groups ?? 0} /{" "}
                      {status?.collector?.dropped_groups ?? 0}
                    </dd>
                  </div>
                  <div>
                    <dt>文本过滤比例</dt>
                    <dd>
                      {(
                        (status?.capture_statistics?.dedup_ratio ?? 0) * 100
                      ).toFixed(1)}
                      %
                    </dd>
                  </div>
                  <div>
                    <dt>最近一次采集</dt>
                    <dd>
                      {status?.collector?.last_capture_at
                        ? new Date(
                            status.collector.last_capture_at,
                          ).toLocaleTimeString("zh-CN")
                        : "尚无记录"}
                    </dd>
                  </div>
                  <div>
                    <dt>最近一次 OCR</dt>
                    <dd>
                      {status?.collector?.last_ocr_at
                        ? `${new Date(status.collector.last_ocr_at).toLocaleTimeString("zh-CN")} · ${status.collector.last_ocr_seconds ?? "—"} 秒`
                        : "尚未触发"}
                    </dd>
                  </div>
                  <div>
                    <dt>当前记忆后端</dt>
                    <dd>{status?.backend}</dd>
                  </div>
                  <div>
                    <dt>等待 / 执行中</dt>
                    <dd>
                      {status?.jobs.pending ?? 0} / {status?.jobs.running ?? 0}
                    </dd>
                  </div>
                  <div>
                    <dt>失败任务</dt>
                    <dd>{status?.jobs.failed ?? 0}</dd>
                  </div>
                  <div>
                    <dt>原始文本保留</dt>
                    <dd>{status?.retention_days ?? 30} 天</dd>
                  </div>
                  <div>
                    <dt>云端回退</dt>
                    <dd>关闭</dd>
                  </div>
                </dl>
                {status?.collector?.running &&
                  (status.collector.paused ||
                    !status.collector.accessibility_granted ||
                    !status.collector.screen_recording_granted) && (
                    <p className="small">
                      在 Mac
                      菜单栏“拾忆采集”中授予辅助功能和屏幕录制权限，再点击“开始采集”。
                    </p>
                  )}
                {status?.collector?.last_error && (
                  <p className="error-text small">
                    {status.collector.last_error === "ocr_unavailable"
                      ? "OCR 暂不可用，将在下次采集时重试。"
                      : status.collector.last_error ===
                          "screen_recording_permission_required"
                        ? "OCR 需要屏幕录制权限。"
                        : status.collector.last_error ===
                            "accessibility_permission_required"
                          ? "采集需要辅助功能权限。"
                          : "最近一次采集未完成，请查看菜单栏状态。"}
                  </p>
                )}
                {status?.errors.map((e) => (
                  <p className="error-text small" key={e.kind + e.error}>
                    {e.kind}: {e.error}（{e.count}）
                  </p>
                ))}
                <button
                  disabled={busy}
                  className="secondary"
                  onClick={() =>
                    void run(() => post("/jobs/retry"), "已安排失败任务重试。")
                  }
                >
                  重试失败任务
                </button>
              </section>
              <section className="panel settings-card full-width">
                <h2>最近的模型请求</h2>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>模型</th>
                        <th>推理耗时</th>
                        <th>排队</th>
                        <th>加载 / 切换</th>
                      </tr>
                    </thead>
                    <tbody>
                      {status?.inference.recent
                        .slice()
                        .reverse()
                        .slice(0, 8)
                        .map((m, i) => (
                          <tr key={i}>
                            <td>{m.model}</td>
                            <td>{m.seconds.toFixed(1)} 秒</td>
                            <td>{m.wait_seconds.toFixed(1)} 秒</td>
                            <td>
                              {m.load_seconds == null
                                ? "未提供"
                                : `${m.load_seconds.toFixed(1)} 秒`}
                            </td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                  {!status?.inference.recent.length && (
                    <p className="muted small">完成模型调用后显示实测数据。</p>
                  )}
                </div>
              </section>
              </>,
            }} />
          )}
          <footer>
            <Leaf size={13} /> 留住过程，让下一步更清晰。
            <span>拾忆 · Work Memory</span>
          </footer>
        </div>
      </main>
      {adding && (
        <Modal title="添加工作记录" onClose={() => setAdding(false)}>
          <form onSubmit={addNote}>
            <label>
              项目名称 <span className="muted">（可选）</span>
              <input
                value={noteProject}
                onChange={(e) => setNoteProject(e.target.value)}
                maxLength={200}
                placeholder="留空归入日常工作；有明确项目时再填写"
              />
            </label>
            <label>
              今天的进展
              <textarea
                autoFocus
                required
                rows={8}
                value={note}
                onChange={(e) => setNote(e.target.value)}
                maxLength={30000}
                placeholder="记录一个决定、一段进展，或下一步要做的事情…"
              />
            </label>
            <p className="muted small">
              保存原文并选择有来源支撑的摘录，不自动创建待办；长期记忆需要明确确认。
            </p>
            <div className="modal-actions">
              <button
                type="button"
                className="secondary"
                onClick={() => setAdding(false)}
              >
                取消
              </button>
              <button className="primary" disabled={busy || !note.trim()}>
                {busy ? (
                  <LoaderCircle className="spin" size={16} />
                ) : (
                  <Plus size={16} />
                )}
                保存记录
              </button>
            </div>
          </form>
        </Modal>
      )}
      {source && (
        <Modal title="记忆与来源" onClose={() => setSource(null)}>
          <div className="source-summary">
            <span className="eyebrow">
              {fmt(source.episode.started_at)} · 第 {source.episode.revision} 版
            </span>
            <SourceNeighbors
              id={source.episode.id}
              onSource={(id) => void viewSource(id)}
            />
            {edit !== null ? (
              <textarea
                aria-label="纠正后的摘要"
                rows={6}
                maxLength={3000}
                value={edit}
                onChange={(e) => setEdit(e.target.value)}
              />
            ) : (
              <>
                <strong>{source.episode.evidence_label || "来源记录"}</strong>
                <p>{source.episode.summary || "没有可供自动引用的摘录，可核对下方原文或隔离历史。"}</p>
                {source.episode.evidence_expires_at && <p className="muted small">未确认摘录保留至 {fmt(source.episode.evidence_expires_at)}</p>}
                <LegacyHistory episodeId={source.episode.id} />
                {source.excerpts?.map(excerpt => <div className="source-excerpt" key={excerpt.id}>
                  <blockquote>{excerpt.text}</blockquote>
                  <SourceMemory episode={source.episode} text={excerpt.text} factId={excerpt.id} />
                  <button className="text-button" onClick={() => document.getElementById(`raw-${excerpt.provenance.source_id.split(":")[1]}`)?.scrollIntoView({ behavior: "smooth", block: "center" })}>核对 {excerpt.provenance.app} 的原文</button>
                </div>)}
                <div className="agent-toolbar">
                  {(
                    [
                      ["useful", "有用"],
                      ["inaccurate", "不准确"],
                      ["wrong_project", "项目不对"],
                    ] as const
                  ).map(([value, label]) => (
                    <button
                      key={value}
                      onClick={() =>
                        void run(
                          () =>
                            post("/feedback", {
                              object_kind: "episode",
                              object_id: source.episode.id,
                              expected_revision: source.episode.revision,
                              value,
                            }),
                          "已记录反馈",
                        )
                      }
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </>
            )}
            {edit !== null ? (
              <div className="modal-actions">
                <button className="secondary" onClick={() => setEdit(null)}>
                  取消
                </button>
                <button
                  className="primary"
                  disabled={busy || !edit.trim()}
                  onClick={() =>
                    void run(async () => {
                      await api(`/episodes/${source.episode.id}`, {
                        method: "PATCH",
                        body: JSON.stringify({
                          summary: edit,
                          expected_revision: source.episode.revision,
                        }),
                      });
                      setSource(null);
                      window.dispatchEvent(new Event("memory-changed"));
                      setSearchItems(null);
                    }, "纠正已保存，正在同步到使用过的记忆后端。")
                  }
                >
                  保存纠正
                </button>
              </div>
            ) : (
              <div className="source-actions">
                <button
                  className="text-button"
                  onClick={() => setEdit(source.episode.summary || "")}
                >
                  纠正记忆
                </button>
                <button
                  className="text-button"
                  disabled={busy}
                  onClick={() =>
                    void run(
                      () => post(`/episodes/${source.episode.id}/retry`),
                      "已安排重新处理。",
                    )
                  }
                >
                  重新处理
                </button>
                <button
                  className="text-button danger"
                  onClick={() => { setDeletePreview(null); setDeleting(true); void run(async () => { setDeletePreview(await api(`/episodes/${source.episode.id}/delete-preview`)); }); }}
                >
                  <Trash2 size={14} />
                  删除片段
                </button>
              </div>
            )}
          </div>
          {deleting && (
            <div className="delete-confirm">
              <p>
                删除这段原始文本、摘要、相关待办与提醒？所有曾使用的记忆后端都会收到删除任务。删除后不能恢复。
              </p>
              {deletePreview && <div>关联长期记忆 {deletePreview.memories.length} 条也将删除：{deletePreview.memories.map(item => <p key={item.id}>{item.text}</p>)}</div>}
              <button className="secondary" onClick={() => setDeleting(false)}>
                取消
              </button>
              <button
                className="danger-button"
                disabled={busy || !deletePreview}
                onClick={() =>
                  void run(async () => {
                    await api(`/episodes/${source.episode.id}?expected_revision=${deletePreview!.revision}&memory_hash=${deletePreview!.memory_hash}`, {
                      method: "DELETE",
                    });
                    setSource(null);
                    window.dispatchEvent(new Event("memory-changed"));
                    setSearchItems(null);
                  }, "片段已删除，后台正在清理派生记忆。")
                }
              >
                确认删除
              </button>
            </div>
          )}
          <h3 className="source-heading">
            <ArrowDown size={16} />
            原始文本来源
          </h3>
          {!!source.observations?.length &&
            Array.from(
              new Set(source.observations.map((o) => o.interaction_id ?? o.id)),
            ).map((group) => (
              <section className="source-item" key={group}>
                {source
                  .observations!.filter(
                    (o) => (o.interaction_id ?? o.id) === group,
                  )
                  .map((o) => (
                    <div key={o.id} id={`raw-${o.id}`}>
                      {o.resource_uri && /^https?:/.test(o.resource_uri) && (
                        <a
                          href={o.resource_uri}
                          target="_blank"
                          rel="noreferrer"
                        >
                          打开原网页
                        </a>
                      )}
                      <strong>
                        {(
                          {
                            enter: "Enter 提交",
                            click: "点击",
                            app_switch: "切换应用",
                            window_switch: "切换窗口",
                            visual: "页面变化",
                            timer: "定时",
                            resume: "恢复采集",
                          } as Record<string, string>
                        )[o.trigger ?? ""] ?? "采集记录"}{" "}
                        ·{" "}
                        {(
                          {
                            before: "操作前",
                            after: "操作后",
                            supplement: "加载后补充",
                            snapshot: "当前画面",
                          } as Record<string, string>
                        )[o.phase ?? ""] ?? "正文"}
                      </strong>
                      <p className="muted small">
                        {fmt(o.captured_at, {
                          hour: "2-digit",
                          minute: "2-digit",
                          second: "2-digit",
                        })}{" "}
                        · {o.app} ·{" "}
                        {o.meaningful
                          ? "参与整理"
                          : `相似 ${(o.similarity * 100).toFixed(1)}%，已去重`}
                      </p>
                      {o.missing_before && (
                        <p className="small">未取得有效的操作前画面。</p>
                      )}
                      {o.missing_after && (
                        <p className="small">未取得有效的操作后画面。</p>
                      )}
                      {(o.merged_count > 0 || o.dropped_groups > 0) && (
                        <p className="small">
                          合并操作 {o.merged_count} 次 · 本次会话丢弃操作组{" "}
                          {o.dropped_groups}
                        </p>
                      )}
                      {o.title && <h4>{o.title}</h4>}
                      <pre>
                        {o.text ?? "原始文本已到期清理，或已被用户纠正替代。"}
                      </pre>
                    </div>
                  ))}
              </section>
            ))}
          {source.activities
            .filter(
              (a) => !source.observations?.some((o) => o.activity_id === a.id),
            )
            .map((a) => (
              <div className="source-item" key={a.id} id={`raw-${a.id}`}>
                <div>
                  <strong>{a.app}</strong>
                  <small>
                    {fmt(a.captured_at, { hour: "2-digit", minute: "2-digit" })}{" "}
                    · {a.method}
                  </small>
                </div>
                {a.title && <h4>{a.title}</h4>}
                <pre>
                  {a.text || "原始文本已到期清理，或已被用户纠正替代。"}
                </pre>
              </div>
            ))}
        </Modal>
      )}
    </div>
  );
}
