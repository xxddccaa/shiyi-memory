import { useEffect, useState } from "react";
import { api, post } from "../api";
import type { Conversation } from "../api";
import OperationCards from "./OperationCards";

type Item = { id: string; text: string; revision: number };
type Suggestion = { left: Item; right: Item };
export default function TodoSuggestions() {
  const [items, setItems] = useState<Suggestion[]>([]);
  const [conversation, setConversation] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let closed = false;
    const load = () =>
      void api<{ items: Suggestion[] }>("/todos/merge-suggestions")
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
  }, []);
  async function decide(value: Suggestion, merge: boolean) {
    try {
      if (merge) {
        let id = conversation;
        if (!id) {
          id = (
            await post<Conversation>("/conversations", { title: "待办整理" })
          ).id;
          setConversation(id);
        }
        await post(`/conversations/${id}/operations`, {
          action: "todo.merge",
          target_id: value.left.id,
          other_id: value.right.id,
        });
      } else {
        await post("/todos/merge-suggestions/reject", {
          left_id: value.left.id,
          right_id: value.right.id,
          left_revision: value.left.revision,
          right_revision: value.right.revision,
        });
      }
      window.dispatchEvent(
        new CustomEvent("client-change", { detail: { kind: "operation" } }),
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }
  if (!items.length && !conversation && !error) return null;
  return (
    <section className="panel">
      <h2>可能重复的待办</h2>
      <p className="muted">合并后保留第一条的完成与确认状态，并汇总来源。</p>
      {error && <p role="alert">{error}</p>}
      {items.map((v) => (
        <div className="memory-candidate" key={v.left.id + v.right.id}>
          <p>{v.left.text}</p>
          <p>{v.right.text}</p>
          <button onClick={() => void decide(v, true)}>预览合并</button>
          <button onClick={() => void decide(v, false)}>不是同一个任务</button>
        </div>
      ))}
      {conversation && <OperationCards conversation={conversation} />}
    </section>
  );
}
