// Native-only, process-lifetime state. No localStorage, API writes, or captured text.
export type PendingSend = { id: string; text: string; conversation: string; context: string };
type Snapshot = { selected?: string; drafts: Record<string, string>; sending: PendingSend | null };
type NativeWindow = Window & {
  __petChatSession?: Partial<Snapshot>;
  webkit?: { messageHandlers?: { petSession?: { postMessage: (value: Snapshot) => void } } };
};
const native = window as NativeWindow;
const enabled = Boolean(native.webkit?.messageHandlers?.petSession);
const initial = enabled ? native.__petChatSession : undefined;
const snapshot: Snapshot = { selected: initial?.selected, drafts: initial?.drafts || {}, sending: initial?.sending || null };
function publish() {
  if (enabled) {
    native.__petChatSession = snapshot;
    native.webkit?.messageHandlers?.petSession?.postMessage(snapshot);
  }
}
export const petSession = {
  enabled,
  hasSelection: typeof snapshot.selected === "string",
  selected: snapshot.selected,
  draft: (id: string) => enabled ? snapshot.drafts[id] || "" : "",
  pending: () => enabled ? snapshot.sending : null,
  select(id: string) { if (enabled) { snapshot.selected = id; publish(); } },
  write(id: string, value: string) { if (enabled) { if (value) snapshot.drafts[id] = value; else delete snapshot.drafts[id]; publish(); } },
  sending(value: PendingSend | null) { if (enabled) { snapshot.sending = value; publish(); } },
  drop(id: string) {
    if (!enabled) return;
    delete snapshot.drafts[id];
    if (snapshot.sending?.conversation === id) snapshot.sending = null;
    if (snapshot.selected === id) snapshot.selected = "";
    publish();
  },
  prune(ids: string[]) {
    if (!enabled) return;
    for (const id of Object.keys(snapshot.drafts)) if (id && !ids.includes(id)) this.drop(id);
    if (snapshot.sending && !ids.includes(snapshot.sending.conversation)) this.drop(snapshot.sending.conversation);
  },
};
