import { useEffect, useState } from "react";
import { api, post } from "../api";
import ProactiveHistory from "./ProactiveHistory";

export type ProactivePreferences = {
  completion_enabled?: boolean;
  topic_preferences?: Record<string, "more" | "normal" | "less">;
  enabled: boolean;
  frequency: "lively" | "balanced" | "quiet";
  topics: ("playful" | "work" | "weather" | "break")[];
  style: string;
  quiet_start: string;
  quiet_end: string;
};
export default function ProactiveSettings({
  value,
  disabled,
  save,
}: {
  value: ProactivePreferences;
  disabled: boolean;
  save: (value: ProactivePreferences) => Promise<void>;
}) {
  const [draft, setDraft] = useState(value);
  const [dirty, setDirty] = useState(false);
  const [trying, setTrying] = useState(false);
  const [sample, setSample] = useState("");
  const [error, setError] = useState("");
  const [base, setBase] = useState(JSON.stringify(value));
  const conflict = dirty && base !== JSON.stringify(value);
  useEffect(() => {
    if (!dirty) {
      setDraft(value);
      setBase(JSON.stringify(value));
    }
  }, [value, dirty]);
  function change(next: Partial<ProactivePreferences>) {
    setDraft({ ...draft, ...next });
    setDirty(true);
  }
  async function commit() {
    try {
      await save(draft);
      setDirty(false);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function preview() {
    setTrying(true);
    setError("");
    setSample("");
    try {
      setSample(
        (await post<{ text: string }>("/client/proactive/preview", draft)).text,
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setTrying(false);
    }
  }
  return (
    <div className="proactive-settings">
      <h3>主动陪伴</h3>
      <p className="muted">
        偶尔探头说句俏皮话。点击气泡才接入聊天，不响铃、不抢焦点。模型忙时安静陪着你。
      </p>
      <label>
        <input
          type="checkbox"
          checked={draft.enabled}
          onChange={(e) => change({ enabled: e.target.checked })}
        />
        让桌宠主动冒泡
      </label>
      <label>
        <input
          type="checkbox"
          checked={draft.completion_enabled ?? true}
          onChange={(e) => change({ completion_enabled: e.target.checked })}
        />
        完成事项后轻轻鼓励（每天最多3次，间隔至少30分钟）
      </label>
      <label>
        活跃程度
        <select
          aria-label="陪伴活跃程度"
          value={draft.frequency}
          onChange={(e) =>
            change({
              frequency: e.target.value as ProactivePreferences["frequency"],
            })
          }
        >
          <option value="lively">活泼 · 10–20分钟，每天最多30次</option>
          <option value="balanced">适中 · 30–60分钟，每天最多12次</option>
          <option value="quiet">安静 · 60–90分钟，每天最多6次</option>
        </select>
      </label>
      <div className="proactive-topics">
        {(
          [
            ["playful", "俏皮闲聊"],
            ["work", "工作鼓励"],
            ["weather", "天气关心"],
            ["break", "休息邀请"],
          ] as const
        ).map(([key, label]) => (
          <label key={key}>
            <input
              type="checkbox"
              checked={draft.topics.includes(key)}
              onChange={(e) =>
                change({
                  topics: e.target.checked
                    ? [...draft.topics, key]
                    : draft.topics.filter((v) => v !== key),
                })
              }
            />
            {label}
          </label>
        ))}
      </div>
      <div className="proactive-topics">
        {(["playful", "work", "weather", "break"] as const).map((key, i) => (
          <label key={key}>
            {["俏皮闲聊", "工作鼓励", "天气关心", "休息邀请"][i]}
            <select
              value={draft.topic_preferences?.[key] || "normal"}
              onChange={(e) =>
                change({
                  topic_preferences: {
                    ...draft.topic_preferences,
                    [key]: e.target.value as "more" | "normal" | "less",
                  },
                })
              }
            >
              <option value="more">多一些</option>
              <option value="normal">正常</option>
              <option value="less">少一些</option>
            </select>
          </label>
        ))}
      </div>
      <p className="muted">
        工作鼓励不露出具体工作内容；天气需要在“联网与工具”中启用天气并设置常用城市。
      </p>
      <div className="proactive-times">
        <label>
          安静开始
          <input
            aria-label="陪伴安静开始"
            type="time"
            value={draft.quiet_start}
            onChange={(e) => change({ quiet_start: e.target.value })}
          />
        </label>
        <label>
          安静结束
          <input
            aria-label="陪伴安静结束"
            type="time"
            value={draft.quiet_end}
            onChange={(e) => change({ quiet_end: e.target.value })}
          />
        </label>
      </div>
      <p className="muted">
        按应用时区生效；开始与结束相同表示不限时段。全屏、锁屏、聊天或节能时暂停，恢复后不会补弹。
      </p>
      <label>
        专属俏皮风格
        <textarea
          aria-label="专属俏皮风格"
          rows={4}
          maxLength={2000}
          value={draft.style}
          onChange={(e) => change({ style: e.target.value })}
        />
      </label>
      <p className="muted">
        只影响主动气泡，不改变普通聊天。试说使用当前草稿，仅预览轻松闲聊。
      </p>
      <div className="proactive-actions">
        <button
          disabled={disabled || !dirty || conflict}
          onClick={() => void commit()}
        >
          保存陪伴设置
        </button>
        <button disabled={trying} onClick={() => void preview()}>
          {trying ? "正在想一句…" : "试说一句"}
        </button>
        <button
          onClick={() =>
            void api<{ style: string }>("/client/proactive/defaults")
              .then((v) => change({ style: v.style }))
              .catch((e) => setError(e.message))
          }
        >
          恢复默认风格
        </button>
        <button
          onClick={() =>
            void post("/client/proactive/mute", { scope: "today" })
              .then(() => setSample("今天先安静陪着你，正式提醒继续保留。"))
              .catch((e) => setError(e.message))
          }
        >
          今天先安静
        </button>
        <button
          onClick={() =>
            void post("/client/proactive/mute", {})
              .then(() => setSample("好呀，接下来一小时安静陪着你。"))
              .catch((e) => setError(e.message))
          }
        >
          安静一小时
        </button>
      </div>
      {conflict && (
        <p role="alert">
          陪伴设置已在另一端更新。
          <button
            onClick={() => {
              setDirty(false);
              setError("");
            }}
          >
            读取新设置
          </button>
        </p>
      )}
      {error && <p role="alert">{error}</p>}
      {sample && (
        <p className="proactive-sample" aria-live="polite">
          {sample}
        </p>
      )}
      <ProactiveHistory revision={JSON.stringify(value)} />
    </div>
  );
}
