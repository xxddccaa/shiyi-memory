import { useEffect, useState } from "react";
import { api, post } from "../api";
import type { Conversation } from "../api";
import type { AgentChatState } from "./useAgentChat";

type Workspace = {
  id: string;
  name: string;
  path: string;
  revision: number;
  extensions: string[];
};
type Context = {
  status: string;
  tokens?: number | null;
  contextWindow?: number;
  percent?: number | null;
  estimated?: boolean;
  capacity_estimated?: boolean;
  compacting?: boolean;
  compactions?: number;
};
export type Call = {
  id: string;
  name: string;
  arguments: Record<string, unknown> | null;
  status: string;
  result: {
    content?: { type: string; text?: string }[];
    details?: { output_id?: string; exit_code?: number };
    change?: { path: string; diff: string };
  } | null;
};

export default function useCodingControls(chat: AgentChatState) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [path, setPath] = useState("");
  const [chosen, setChosen] = useState("");
  const [extensions, setExtensions] = useState("");
  const [context, setContext] = useState<Context | null>(null);
  const [calls, setCalls] = useState<Call[]>([]);
  const [nextCalls, setNextCalls] = useState<number | null>(null);
  const [pending, setPending] = useState(false);
  const current = chat.conversations.find((c) => c.id === chat.selected);
  const workspace = workspaces.find((w) => w.id === current?.workspace_id);
  useEffect(() => {
    void api<{ items: Workspace[] }>("/agent/workspaces")
      .then((v) => setWorkspaces(v.items))
      .catch((e) => chat.setError(e.message));
  }, []);
  useEffect(() => {
    setChosen(current?.workspace_id || "");
  }, [current?.workspace_id, chat.selected]);
  useEffect(() => {
    setExtensions(
      workspaces.find((w) => w.id === chosen)?.extensions.join("\n") || "",
    );
  }, [chosen, workspaces]);
  useEffect(() => {
    let closed = false;
    setContext(null);
    setCalls([]);
    setNextCalls(null);
    async function load() {
      if (!chat.selected) return;
      const value = await api<Context>(
        `/conversations/${chat.selected}/context`,
      );
      if (!closed) setContext(value);
      if (chat.run) {
        const result = await api<{ items: Call[]; next_offset: number | null }>(
          `/agent/runs/${chat.run.id}/tool-calls`,
        );
        if (!closed) {
          setCalls(result.items);
          setNextCalls(result.next_offset);
        }
      }
    }
    const refresh = () =>
      void load().catch((e) => {
        if (!closed) chat.setError(e.message);
      });
    const invalidate = () => {
      setContext(null);
      setCalls([]);
      setNextCalls(null);
      refresh();
    };
    refresh();
    const timer = chat.running ? setInterval(refresh, 2000) : undefined;
    window.addEventListener("memory-changed", invalidate);
    return () => {
      closed = true;
      clearInterval(timer);
      window.removeEventListener("memory-changed", invalidate);
    };
  }, [chat.selected, chat.run?.id, chat.run?.status]);
  async function act(fn: () => Promise<void>) {
    setPending(true);
    chat.setError("");
    try {
      await fn();
    } catch (e) {
      chat.setError((e as Error).message);
    } finally {
      setPending(false);
    }
  }
  async function createCoding() {
    const created = await post<Conversation>("/conversations", {
      workspace_id: chosen,
      coding_enabled: true,
      title: "新对话",
    });
    chat.setSelected(created.id);
    chat.refresh();
  }
  async function toggle() {
    if (!current || current.workspace_id !== chosen) {
      await createCoding();
      return;
    }
    await api(`/conversations/${current.id}`, {
      method: "PATCH",
      body: JSON.stringify({
        workspace_id: chosen,
        coding_enabled: !current.coding_enabled,
        expected_revision: current.revision,
      }),
    });
    chat.refresh();
  }
  return {
    workspaces,
    setWorkspaces,
    path,
    setPath,
    chosen,
    setChosen,
    extensions,
    setExtensions,
    context,
    calls,
    setCalls,
    nextCalls,
    setNextCalls,
    pending,
    current,
    workspace,
    act,
    toggle,
  };
}
export type CodingState = ReturnType<typeof useCodingControls>;
