import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import PetSkins from "./PetSkins";
import ProactiveSettings, { type ProactivePreferences } from "./ProactiveSettings";

interface Preferences {
  revision: number;
  proactive: ProactivePreferences;
  pet_visible: boolean;
  pet_size: number;
  pet_skin: string;
  pet_motion_enabled: boolean;
  reminders_enabled: boolean;
  hide_fullscreen: boolean;
  resource_policy: "standard" | "saving";
  resource_capture: boolean;
  backfill_enabled: boolean;
}
export default function CompanionSettings() {
  const [state, setState] = useState<Preferences | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const acceptState = useCallback((value: Preferences) => {
    setState((current) => !current || value.revision >= current.revision ? value : current);
  }, []);
  const [progress, setProgress] = useState<Record<string, number>>({});
  useEffect(() => {
    const load = () => {
      void api<Preferences>("/client/state")
        .then((value) => { if (!savingRef.current) acceptState(value); })
        .catch((e) => setError(e.message));
      void api<{ jobs: Record<string, number> }>("/memory/backfill")
        .then((v) => setProgress(v.jobs))
        .catch(() => {});
    };
    load();
    window.addEventListener("client-change", load);
    const timer = setInterval(() => {
      if (!document.hidden) load();
    }, 10000);
    return () => {
      clearInterval(timer);
      window.removeEventListener("client-change", load);
    };
  }, []);
  async function change(key: string, value: unknown, propagate = false) {
    if (!state || savingRef.current) {
      if (propagate) throw new Error("设置正在保存，请稍后再试");
      return;
    }
    savingRef.current = true; setSaving(true);
    setState({ ...state, [key]: value });
    try {
      acceptState(
        await api<Preferences>("/client/state", {
          method: "PATCH",
          body: JSON.stringify({
            expected_revision: state.revision,
            [key]: value,
          }),
        }),
      );
      setError("");
    } catch (e) {
      setError((e as Error).message);
      try { acceptState(await api<Preferences>("/client/state")); }
      catch { setState(state); }
      if (propagate) throw e;
    } finally { savingRef.current = false; setSaving(false); }
  }
  return (
    <section className="panel settings-card companion-settings">
      <h2>桌宠与工作整理</h2>
      <p>网页与桌宠使用同一个拾忆助手。隐藏桌宠不会暂停采集。</p>
      {error && <p role="alert">{error}</p>}
      {state && (
        <>
          {(
            [
              ["pet_visible", "显示桌宠"],
              ["pet_motion_enabled", "单图形象轻微动效"],
              ["hide_fullscreen", "全屏应用时隐藏"],
              ["reminders_enabled", "桌宠工作提醒"],
              ["resource_capture", "记录 Safari / Chrome 网页地址"],
              ["backfill_enabled", "复核历史原文摘录"],
            ] as const
          ).map(([key, label]) => (
            <label key={key}>
              <input
                type="checkbox"
                disabled={saving}
                checked={state[key]}
                onChange={(e) => void change(key, e.target.checked)}
              />
              {label}
            </label>
          ))}
          {state.proactive && <ProactiveSettings value={state.proactive} disabled={saving} save={(value) => change("proactive", value, true)} />}
          <PetSkins selected={state.pet_skin} onSelect={(id) => change("pet_skin", id, true)} />
          <label>
            桌宠大小{" "}
            <input
              aria-label="桌宠大小"
              disabled={saving}
              type="range"
              min="48"
              max="240"
              defaultValue={state.pet_size}
              onPointerUp={(e) =>
                void change("pet_size", Number(e.currentTarget.value))
              }
              onKeyUp={(e) =>
                void change("pet_size", Number(e.currentTarget.value))
              }
            />
          </label>
          <label>
            资源策略{" "}
            <select
              disabled={saving}
              value={state.resource_policy}
              onChange={(e) => void change("resource_policy", e.target.value)}
            >
              <option value="standard">标准 · 每五秒采样</option>
              <option value="saving">节能 · 至少十秒采样，暂停历史整理</option>
            </select>
          </label>
          <p className="muted">
            历史原文复核：完成 {progress.done ?? 0}，等待 {progress.pending ?? 0}
            ，失败 {progress.failed ?? 0}
            。暂停保留进度；新的对话和到期清理继续运行。
          </p>
          <p className="muted">
            网页地址去除查询参数和片段；只在可可靠读取时保留，随原文到期清理。
          </p>
        </>
      )}
    </section>
  );
}
