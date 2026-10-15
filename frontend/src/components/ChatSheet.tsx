import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import { X } from "lucide-react";

export default function ChatSheet({ title, children, onClose, closeLabel = "关闭面板" }: {
  title: string; children: ReactNode; onClose: () => void; closeLabel?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = ref.current!;
    const previous = document.activeElement as HTMLElement | null;
    element.showModal();
    return () => { element.close(); previous?.focus(); };
  }, []);
  return <dialog ref={ref} className="chat-sheet" aria-label={title}
    onCancel={(e) => { e.preventDefault(); onClose(); }}
    onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
    <div className="chat-sheet-panel">
      <header><h2>{title}</h2><button className="icon-button" aria-label={closeLabel} onClick={onClose}><X size={18} /></button></header>
      <div className="chat-sheet-body">{children}</div>
    </div>
  </dialog>;
}
