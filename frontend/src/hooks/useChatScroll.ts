import { useLayoutEffect, useRef, useState } from "react";
import type { ChatMessage } from "../api";

export default function useChatScroll(conversation: string, messages: ChatMessage[]) {
  const ref = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  const previous = useRef({ first: "", last: "", height: 0 });
  const [unread, setUnread] = useState(false);
  const goBottom = () => {
    nearBottom.current = true;
    setUnread(false);
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  };
  useLayoutEffect(() => {
    previous.current = { first: "", last: "", height: 0 };
    goBottom();
  }, [conversation]);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const first = messages[0]?.id || "", last = messages.at(-1)?.id || "";
    const old = previous.current;
    if (old.first && old.first !== first && old.last === last) {
      el.scrollTop += el.scrollHeight - old.height;
    } else if (nearBottom.current) {
      el.scrollTop = el.scrollHeight;
    } else if (last !== old.last) {
      setUnread(true);
    }
    previous.current = { first, last, height: el.scrollHeight };
  }, [messages]);
  useLayoutEffect(() => {
    const el = ref.current, body = content.current;
    if (!el || !body) return;
    const resize = new ResizeObserver(() => {
      if (nearBottom.current) el.scrollTop = el.scrollHeight;
      previous.current.height = el.scrollHeight;
    });
    resize.observe(body);
    resize.observe(el);
    return () => resize.disconnect();
  }, []);
  return { ref, content, unread, goBottom, onScroll: () => {
    const el = ref.current;
    if (!el) return;
    nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 64;
    if (nearBottom.current) setUnread(false);
  } };
}
