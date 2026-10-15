import { useCallback, useEffect, useRef, useState } from "react";
import { api, getToken, post } from "../api";
import { petSession, type PendingSend } from "./petSession";
import type { AgentRun, ChatMessage, Conversation, Evidence, MemoryCandidate, ObjectContext } from "../api";
const labels: Record<string, string> = {
  queued: "已加入回答队列",
  checking_model: "检查工具调用能力",
  repairing: "正在修复回答格式",
  thinking: "正在组织回答",
  compaction_start: "正在压缩上下文",
  compaction_summarizing: "正在生成上下文摘要",
  compaction_end: "上下文压缩结束",
  tool_started: "正在读取资料",
  tool_finished: "已取得工具结果",
  validating: "正在核对引用来源",
  completed: "回答完成",
  cancelled: "已取消",
  interrupted: "回答已中断",
  failed: "回答未完成",
  invalidated: "来源已更新，请重新提问",
};
const active = (run: AgentRun | null) =>
  !!run && ["queued", "running"].includes(run.status);
export default function useAgentChat() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selected, commitSelected] = useState(
    petSession.selected ?? sessionStorage.getItem("workmemory-conversation") ?? "",
  );
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [hasOlder, setHasOlder] = useState(false);
  const historyPages = useRef(1);
  const loadSequence = useRef(0);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [candidates, setCandidates] = useState<MemoryCandidate[]>([]);
  const [showAllMemories, setShowAllMemories] = useState(false);
  const [editingMemory, setEditingMemory] = useState<MemoryCandidate | null>(
    null,
  );
  const [memoryText, setMemoryText] = useState("");
  const [memorySource, setMemorySource] = useState<Evidence | null>(null);
  const showAllRef = useRef(showAllMemories);
  showAllRef.current = showAllMemories;
  const [objectContext, setObjectContext] = useState<ObjectContext | null>(() => {
    try { return JSON.parse(petSession.pending()?.context || sessionStorage.getItem("workmemory-object") || "null"); }
    catch { return null; }
  });
  useEffect(() => {
    if (objectContext) sessionStorage.setItem("workmemory-object", JSON.stringify(objectContext));
    else sessionStorage.removeItem("workmemory-object");
  }, [objectContext]);
  useEffect(() => {
    const open = (event: Event) => {
      const item = (event as CustomEvent).detail;
      if (!item?.conversation_id) return;
      setSelected(item.conversation_id);
      if (item.todo_id && item.todo_revision) setObjectContext({ kind: "todo", id: item.todo_id, revision: item.todo_revision });
    };
    window.addEventListener("open-reminder", open);
    window.addEventListener("open-companion", open);
    const companion = new URLSearchParams(location.search).get("companion_conversation");
    if (companion) open(new CustomEvent("open-companion", { detail: { conversation_id: companion } }));
    const id = new URLSearchParams(location.search).get("reminder");
    if (id) void api<import("../api").Reminder[]>("/reminders").then((items) => {
      const item = items.find((r) => r.id === id);
      if (item) open(new CustomEvent("open-reminder", { detail: item }));
    }).catch(() => {});
    return () => { window.removeEventListener("open-reminder", open); window.removeEventListener("open-companion", open); };
  }, []);
  const [question, commitQuestion] = useState(() => petSession.enabled && selected ? "" : petSession.draft(selected));
  const draftHydrated = useRef(!petSession.enabled || !selected);
  const questionRef = useRef(question);
  const setQuestion = useCallback((value: string) => {
    draftHydrated.current = true;
    questionRef.current = value;
    petSession.write(selection.current, value);
    commitQuestion(value);
  }, []);
  const setSelected = useCallback((id: string) => {
    if (selection.current !== id && petSession.enabled) {
      const draft = petSession.draft(id);
      draftHydrated.current = true;
      questionRef.current = draft;
      commitQuestion(draft);
    }
    selection.current = id;
    petSession.select(id);
    commitSelected(id);
  }, []);
  const [progress, setProgress] = useState("");
  const [recent, setRecent] = useState("");
  const sending = useRef<PendingSend | null>(petSession.pending());
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const retrying = useRef<{ runId: string; requestId: string } | null>(null);
  const selection = useRef(selected);
  selection.current = selected;
  const list = useCallback(async () => {
    const value = await api<{ items: Conversation[] }>("/conversations");
    setConversations(value.items);
    petSession.prune(value.items.map((c) => c.id));
    if (!draftHydrated.current && value.items.some((c) => c.id === selection.current)) {
      setQuestion(petSession.draft(selection.current));
    }
    if (
      selection.current &&
      !value.items.some((c) => c.id === selection.current)
    ) {
      petSession.drop(selection.current);
      sending.current = null;
      setSelected("");
    }
  }, []);
  const load = useCallback(async (id: string) => {
    if (!id) return;
    const sequence = ++loadSequence.current;
    type History = {
      items: ChatMessage[];
      run: AgentRun | null;
      next_cursor: string | null;
    };
    const [history, memories] = await Promise.all([
      api<History>(`/conversations/${id}/messages`),
      api<{ items: MemoryCandidate[] }>(
        showAllRef.current
          ? "/memory/candidates"
          : `/memory/candidates?conversation_id=${id}`,
      ),
    ]);
    let cursor = history.next_cursor;
    let items = history.items;
    for (let page = 1; page < historyPages.current && cursor; page++) {
      if (sequence !== loadSequence.current || selection.current !== id) return;
      const older = await api<History>(
        `/conversations/${id}/messages?before=${encodeURIComponent(cursor)}`,
      );
      items = [...older.items, ...items];
      cursor = older.next_cursor;
    }
    if (selection.current !== id || sequence !== loadSequence.current) return;
    setHasOlder(!!cursor);
    setMessages(items);
    setRun(history.run);
    const pending = sending.current;
    if (pending?.conversation === id && petSession.enabled) {
      const receipt = await api<{ accepted: boolean }>(`/conversations/${id}/requests/${pending.id}`);
      if (selection.current === id && sending.current?.id === pending.id && receipt.accepted) {
        sending.current = null; petSession.sending(null);
        const draft = draftHydrated.current ? questionRef.current : petSession.draft(id);
        if (draft.trim() === pending.text) setQuestion("");
      }
    }
    setCandidates(
      showAllRef.current
        ? memories.items.filter((c) => c.status === "confirmed")
        : memories.items,
    );
  }, []);
  useEffect(() => {
    void list().catch((e) => setError(e.message));
    void api<{ recent_conversation_id: string | null }>("/client/state")
      .then((s) => {
        setRecent(s.recent_conversation_id || "");
        if (!selection.current && !petSession.hasSelection && s.recent_conversation_id)
          setSelected(s.recent_conversation_id);
      })
      .catch(() => {});
  }, [list]);
  useEffect(() => {
    sessionStorage.setItem("workmemory-conversation", selected);
    petSession.select(selected);
    historyPages.current = 1;
    setHasOlder(false);
    setMessages([]);
    setRun(null);
    setCandidates([]);
    setProgress("");
    setError("");
    setMemorySource(null);
    setEditingMemory(null);
    if (selected) void load(selected).catch((e) => setError(e.message));
  }, [selected, load]);
  useEffect(() => {
    const changed = () => {
      setMessages([]);
      setMemorySource(null);
      setEditingMemory(null);
      if (selected) void load(selected).catch((e) => setError(e.message));
    };
    window.addEventListener("memory-changed", changed);
    return () => window.removeEventListener("memory-changed", changed);
  }, [selected, load]);
  useEffect(() => {
    let cancelled = false;
    if (showAllMemories)
      void api<{ items: MemoryCandidate[] }>("/memory/candidates")
        .then((value) => {
          if (!cancelled)
            setCandidates(value.items.filter((c) => c.status === "confirmed"));
        })
        .catch((e) => setError(e.message));
    else if (selected) void load(selected).catch((e) => setError(e.message));
    else setCandidates([]);
    return () => {
      cancelled = true;
    };
  }, [showAllMemories, selected, load]);
  const runId = active(run) ? run!.id : null;
  useEffect(() => {
    if (!runId || !selected) return;
    const controller = new AbortController();
    const observe = async () => {
      const response = await fetch(`/api/agent/runs/${runId}/events`, {
        headers: { Authorization: "Bearer " + getToken() },
        signal: controller.signal,
      });
      if (!response.ok || !response.body)
        throw new Error("无法读取回答进度，请刷新会话。");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const value = await reader.read();
        if (value.done) break;
        buffer += decoder.decode(value.value, { stream: true });
        let boundary: number;
        while ((boundary = buffer.indexOf("\n\n")) >= 0) {
          const packet = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const data = packet
            .split("\n")
            .find((line) => line.startsWith("data: "));
          if (!data) continue;
          const event = JSON.parse(data.slice(6));
          if (event.kind === "tool_started") {
            const tool = event.tool || "";
            setProgress(["read", "grep", "find", "ls"].includes(tool) ? "正在读取项目文件" : ["write", "edit"].includes(tool) ? "正在修改文件" : tool === "bash" ? "正在执行命令" : tool.startsWith("mcp_") ? "正在联网查询" : tool.startsWith("weather__") ? "正在查询天气"
              : ["propose_operation", "propose_merge"].includes(tool) ? "正在准备确认卡片" : tool === "get_work_day" ? "正在读取当天工作记录" : "正在查找记忆与工作对象");
          } else if (event.kind) setProgress(labels[event.kind] || "正在处理");
        }
      }
      if (!controller.signal.aborted) {
        await load(selected);
        await list();
      }
    };
    void observe().catch((e) => {
      if (!controller.signal.aborted) setError(e.message);
    });
    return () => controller.abort();
  }, [runId, selected, load, list]);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const changed = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (document.hidden) return;
        void list().catch((e) => setError(e.message));
        if (selected) void load(selected).catch((e) => setError(e.message));
        void api<{ recent_conversation_id: string | null }>("/client/state")
          .then((s) => setRecent(s.recent_conversation_id || ""))
          .catch(() => {});
      }, 100);
    };
    window.addEventListener("client-change", changed);
    return () => {
      clearTimeout(timer);
      window.removeEventListener("client-change", changed);
    };
  }, [selected, list, load]);
  useEffect(() => {
    const state = active(run)
      ? progress.includes("查找")
        ? "searching"
        : "thinking"
      : run?.status === "failed"
        ? "error"
        : "idle";
    const bridge = (
      window as unknown as {
        webkit?: {
          messageHandlers?: { petState?: { postMessage: (s: string) => void } };
        };
      }
    ).webkit;
    bridge?.messageHandlers?.petState?.postMessage(state);
  }, [run, progress]);
  async function send(value = question) {
    if (!value.trim() || busyRef.current || active(run)) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    try {
      let id = selected;
      if (!id) {
        const conversation = await post<Conversation>("/conversations");
        id = conversation.id;
        if (petSession.enabled) { petSession.write(id, value); petSession.drop(""); }
        setSelected(id);
      }
      if (
        !sending.current ||
        sending.current.text !== value.trim() ||
        sending.current.conversation !== id ||
        sending.current.context !== JSON.stringify(objectContext)
      )
        sending.current = {
          id: crypto.randomUUID(),
          text: value.trim(),
          conversation: id,
          context: JSON.stringify(objectContext),
        };
      petSession.sending(sending.current);
      await post(`/conversations/${id}/messages`, {
        content: value.trim(),
        request_id: sending.current.id,
        context: objectContext,
      });
      sending.current = null;
      petSession.sending(null);
      petSession.write(id, "");
      if (selection.current === id) { setQuestion(""); setObjectContext(null); }
      await load(id);
      await list();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }
  async function changeCandidate(
    candidate: MemoryCandidate,
    action: "confirm" | "dismiss",
    replace?: { id: string; revision: number },
  ) {
    try {
      await api(`/memory/candidates/${candidate.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          action,
          expected_revision: candidate.revision,
          ...(replace
            ? { replace_id: replace.id, replace_revision: replace.revision }
            : {}),
        }),
      });
      if (selected) await load(selected);
      if (showAllRef.current) {
        const values = await api<{ items: MemoryCandidate[] }>(
          "/memory/candidates",
        );
        setCandidates(values.items.filter((c) => c.status === "confirmed"));
      }
      setMemorySource(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function removeConversation() {
    if (
      !selected ||
      !window.confirm(
        "删除这个会话及其后续回答，也会删除由它确认的长期记忆。继续删除？",
      )
    )
      return;
    try {
      await api(`/conversations/${selected}`, { method: "DELETE" });
      petSession.drop(selected);
      sending.current = null;
      setSelected("");
      await list();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function retry() {
    if (!run?.retryable || busyRef.current) return;
    const id = selected;
    busyRef.current = true;
    setBusy(true);
    setError("");
    if (retrying.current?.runId !== run.id)
      retrying.current = { runId: run.id, requestId: crypto.randomUUID() };
    try {
      await post(`/agent/runs/${run.id}/retry`, { request_id: retrying.current.requestId });
      retrying.current = null;
      await load(id);
      await list();
    } catch (e) {
      setError((e as Error).message);
      // Refresh eligibility after another client wins the retry race.
      await load(id).catch(() => {});
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }
  async function cancel() {
    if (!run || !active(run)) return;
    try {
      await post(`/agent/runs/${run.id}/cancel`);
      await load(selected);
    } catch (e) { setError((e as Error).message); }
  }
  function loadOlder() {
    historyPages.current += 1;
    void load(selected).catch((e) => setError(e.message));
  }
  function refresh() {
    setError("");
    void Promise.all([load(selected), list()]).catch((e) => setError(e.message));
  }
  async function previewMemory() {
    if (!editingMemory) return;
    try {
      let id = selected;
      if (!id) {
        id = (await post<Conversation>("/conversations")).id;
        selection.current = id;
        setSelected(id);
      }
      await post(`/conversations/${id}/operations`, {
        action: "memory.update", target_id: editingMemory.id, text: memoryText,
      });
      setEditingMemory(null);
      window.dispatchEvent(new CustomEvent("client-change", {detail: {kind: "operation"}}));
      return true;
    } catch (e) { setError((e as Error).message); return false; }
  }
  return {
    conversations, selected, setSelected, messages, hasOlder, loadOlder,
    run, running: active(run), candidates, showAllMemories, setShowAllMemories,
    editingMemory, setEditingMemory, memoryText, setMemoryText, memorySource, setMemorySource,
    objectContext, setObjectContext, question, setQuestion, progress, recent, error, setError, busy,
    send, retry, cancel, refresh, changeCandidate, removeConversation, previewMemory,
  };
}
export type AgentChatState = ReturnType<typeof useAgentChat>;
