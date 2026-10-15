import { useEffect } from "react";
import { api, getToken } from "../api";

export function useClientEvents() {
  useEffect(() => {
    let stopped = false;
    let controller: AbortController | null = null;
    let cursor = 0;
    let initialized = false;
    const connect = async () => {
      if (stopped || document.hidden || !getToken() || controller) return;
      controller = new AbortController();
      try {
        if (!initialized) {
          const state = await api<{ cursor: number }>("/client/state");
          cursor = state.cursor;
          initialized = true;
          window.dispatchEvent(
            new CustomEvent("client-change", { detail: { kind: "reset" } }),
          );
        }
        const response = await fetch(`/api/client/events?after=${cursor}`, {
          headers: { Authorization: `Bearer ${getToken()}` },
          signal: controller.signal,
        });
        if (!response.ok || !response.body) throw new Error("连接暂不可用");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let pending = "";
        while (!stopped) {
          const { value, done } = await reader.read();
          if (done) break;
          pending += decoder.decode(value, { stream: true });
          let end;
          while ((end = pending.indexOf("\n\n")) >= 0) {
            const packet = pending.slice(0, end);
            pending = pending.slice(end + 2);
            const lines = packet.split("\n");
            const id = lines.find((l) => l.startsWith("id: "));
            const data = lines.find((l) => l.startsWith("data: "));
            if (id) cursor = Number(id.slice(4));
            if (!data) continue;
            const detail = packet.includes("event: reset")
              ? { kind: "reset" }
              : JSON.parse(data.slice(6));
            window.dispatchEvent(new CustomEvent("client-change", { detail }));
            if (["invalidated", "reset"].includes(detail.kind))
              window.dispatchEvent(new Event("memory-changed"));
          }
        }
      } catch {
        /* Retry retains the last durable cursor; resuming a tab revalidates content. */
      } finally {
        controller = null;
      }
    };
    const visibility = () => {
      if (document.hidden) controller?.abort();
      else {
        window.dispatchEvent(new Event("memory-changed"));
        void connect();
      }
    };
    document.addEventListener("visibilitychange", visibility);
    const retry = setInterval(() => void connect(), 2000);
    void connect();
    return () => {
      stopped = true;
      controller?.abort();
      clearInterval(retry);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, []);
}
