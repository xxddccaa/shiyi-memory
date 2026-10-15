import { useEffect, useRef, useState } from "react";
import { Check, LoaderCircle, Settings2 } from "lucide-react";
import { api, post } from "./api";
import type { GenerationConfig, ModelConfiguration, ModelTest } from "./api";

export default function ModelSettings({
  onSaved,
}: {
  onSaved: () => Promise<void>;
}) {
  const [saved, setSaved] = useState<ModelConfiguration | null>(null);
  const [draft, setDraft] = useState<GenerationConfig | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<"test" | "save" | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const form = useRef<HTMLFormElement>(null);

  async function load() {
    setError("");
    try {
      const value = await api<ModelConfiguration>("/config/models");
      setSaved(value);
      setDraft(value.generation);
      setDirty(false);
      setApiKey("");
      setClearKey(false);
      setMessage("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "无法读取模型配置");
    }
  }
  useEffect(() => {
    void load();
  }, []);

  function change(values: Partial<GenerationConfig>) {
    setDraft((old) => (old ? { ...old, ...values } : old));
    setDirty(true);
    setError("");
    setMessage("");
  }
  function provider(value: GenerationConfig["provider"]) {
    if (!saved) return;
    change(
      value === "ollama"
        ? saved.local_default
        : {
            provider: value,
            base_url: "",
            model: "",
            thinking: null,
            api_key_configured: false,
            timeout_seconds: 120,
            structured_output: "json_schema",
            temperature: 0,
            max_tokens: 32768,
            context_window: null,
          },
    );
    setApiKey("");
    setClearKey(false);
  }
  async function submit(action: "test" | "save") {
    if (!draft || !saved || !form.current?.reportValidity()) return;
    setBusy(action);
    setError("");
    setMessage("");
    const { api_key_configured: _configured, ...fields } = draft;
    const body = {
      ...fields,
      revision: saved.revision,
      api_key: apiKey || null,
      clear_api_key: clearKey,
    };
    try {
      if (action === "test") {
        const result = await post<ModelTest>("/config/models/test", body);
        setMessage(
          `连接与结构化输出测试通过，用时 ${result.seconds.toFixed(2)} 秒。当前配置尚未改变。`,
        );
      } else {
        const result = await post<
          Pick<
            ModelConfiguration,
            "generation" | "revision" | "local_default"
          > & { test: ModelTest }
        >("/config/models", body);
        setSaved({ ...saved, ...result });
        setDraft(result.generation);
        setApiKey("");
        setClearKey(false);
        setDirty(false);
        setMessage(
          `已保存并应用：${result.generation.model}。后续整理和问答使用此模型。`,
        );
        await onSaved();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "模型配置操作失败");
    } finally {
      setBusy(null);
    }
  }

  return (
    <section
      className="panel settings-card full-width model-settings"
      aria-label="模型配置"
    >
      <div className="panel-heading">
        <h2>
          <Settings2 size={18} />
          整理与问答模型
        </h2>
        <span className="local-pill">
          {saved?.generation.provider === "openai_compatible"
            ? "自定义服务"
            : "本地 Ollama"}
        </span>
      </div>
      <p>
        选择用于整理工作记录和回答问题的模型。嵌入检索与 OCR 继续在本机运行。
      </p>
      {saved && (
        <p className="model-current">
          当前生效：<strong>{saved.generation.model}</strong>
          <span>{saved.generation.base_url}</span>
        </p>
      )}
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
      {!draft || !saved ? (
        <button className="secondary" onClick={() => void load()}>
          重新加载模型配置
        </button>
      ) : (
        <>
          <form
            ref={form}
            onSubmit={(e) => {
              e.preventDefault();
              void submit("save");
            }}
          >
            <fieldset disabled={busy !== null}>
              <div className="model-fields">
                <label>
                  生成服务
                  <select
                    value={draft.provider}
                    onChange={(e) =>
                      provider(e.target.value as GenerationConfig["provider"])
                    }
                  >
                    <option value="ollama">本地 Ollama</option>
                    <option value="openai_compatible">
                      OpenAI 兼容接口（局域网 / 云端）
                    </option>
                  </select>
                </label>
                <label>
                  模型名称
                  <input
                    required
                    maxLength={200}
                    list={
                      draft.provider === "ollama"
                        ? "local-generation-models"
                        : undefined
                    }
                    value={draft.model}
                    onChange={(e) => change({ model: e.target.value })}
                    placeholder={
                      draft.provider === "ollama"
                        ? "选择已安装的模型"
                        : "填写服务中的模型名称"
                    }
                  />
                </label>
                <label className="model-wide">
                  接口基础地址
                  <input
                    required
                    type="url"
                    maxLength={500}
                    readOnly={draft.provider === "ollama"}
                    value={draft.base_url}
                    onChange={(e) => change({ base_url: e.target.value })}
                    placeholder="http://服务器地址:8000/v1"
                  />
                  <small>
                    {draft.provider === "ollama"
                      ? "本地服务由拾忆管理器启动。"
                      : "通常以 /v1 结尾，不要加 /chat/completions。"}
                  </small>
                </label>
                {draft.provider === "openai_compatible" && (
                  <label className="model-wide">
                    API Key（可选）
                    <input
                      type="password"
                      autoComplete="new-password"
                      spellCheck={false}
                      value={apiKey}
                      disabled={clearKey}
                      onChange={(e) => {
                        setApiKey(e.target.value);
                        setDirty(true);
                        setMessage("");
                      }}
                      placeholder={
                        saved.generation.api_key_configured &&
                        draft.base_url.replace(/\/$/, "") ===
                          saved.generation.base_url
                          ? "已保存，留空保留"
                          : "无需认证的局域网服务可以留空"
                      }
                    />
                    <small>
                      密钥保存在本机，不会回显。更换服务地址不会沿用原密钥。
                    </small>
                  </label>
                )}
              </div>
              <datalist id="local-generation-models">
                {saved.local_models.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
              {draft.provider === "openai_compatible" &&
                saved.generation.api_key_configured && (
                  <label className="model-checkbox">
                    <input
                      type="checkbox"
                      checked={clearKey}
                      onChange={(e) => {
                        setClearKey(e.target.checked);
                        setDirty(true);
                      }}
                    />
                    清除已保存的 API Key
                  </label>
                )}
              <details className="model-advanced">
                <summary>高级参数</summary>
                <div className="model-fields">
                  <label>
                    思考模式
                    <select
                      value={
                        draft.thinking === null
                          ? "default"
                          : draft.thinking
                            ? "on"
                            : "off"
                      }
                      onChange={(e) =>
                        change({
                          thinking:
                            e.target.value === "default"
                              ? null
                              : e.target.value === "on",
                        })
                      }
                    >
                      <option value="default">
                        服务默认（不发送额外参数）
                      </option>
                      <option value="off">
                        关闭（Ollama / Qwen 兼容服务）
                      </option>
                      <option value="on">开启（Ollama / Qwen 兼容服务）</option>
                    </select>
                  </label>
                  <label>
                    结构化输出
                    <select
                      value={draft.structured_output}
                      disabled={draft.provider === "ollama"}
                      onChange={(e) =>
                        change({
                          structured_output: e.target
                            .value as GenerationConfig["structured_output"],
                        })
                      }
                    >
                      <option value="json_schema">JSON Schema</option>
                      <option value="json_object">JSON 对象（兼容模式）</option>
                    </select>
                  </label>
                  <label>
                    请求超时（秒）
                    <input
                      type="number"
                      required
                      min={10}
                      max={86400}
                      step={1}
                      value={draft.timeout_seconds}
                      onChange={(e) =>
                        change({ timeout_seconds: Number(e.target.value) })
                      }
                    />
                  </label>
                  <label>
                    最大输出 Token
                    <input
                      type="number"
                      required
                      min={256}
                      step={1}
                      value={draft.max_tokens}
                      onChange={(e) =>
                        change({ max_tokens: Number(e.target.value) })
                      }
                    />
                  </label>
                  <label>
                    模型上下文 Token（可选）
                    <input
                      type="number"
                      min={1024}
                      step={1}
                      placeholder="按服务提供的容量填写，如 1000000"
                      value={draft.context_window ?? ""}
                      onChange={(e) => change({ context_window: e.target.value === "" ? null : Number(e.target.value) })}
                    />
                  </label>
                  <label>
                    温度
                    <input
                      type="number"
                      required
                      min={0}
                      max={2}
                      step={0.1}
                      value={draft.temperature}
                      onChange={(e) =>
                        change({ temperature: Number(e.target.value) })
                      }
                    />
                  </label>
                </div>
                <p className="small muted">
                  最大输出用于每次聊天模型请求，不再额外压低。上下文容量包含输入和输出；
                  Pi 会预留输出空间，接近容量时自动总结历史并继续任务；用量优先采用服务返回的 token 数。
                  本机编程需要填写真实容量。普通聊天留空时仅用临时估算，最终以服务端为准。
                </p>
                <p className="small muted">
                  Qwen
                  服务可选择关闭思考。服务不接受额外参数时选择“服务默认”；不支持
                  JSON Schema 时可尝试 JSON 对象模式。
                </p>
              </details>
              {draft.provider === "openai_compatible" && (
                <p className="model-data-note">
                  保存后，需要整理的工作文字及问答上下文会发送到此地址。截图仍在本机处理，不会自动回退到其他服务。
                </p>
              )}
              <div className="model-actions">
                <button
                  type="button"
                  className="secondary"
                  onClick={() => void submit("test")}
                >
                  {busy === "test" && (
                    <LoaderCircle className="spin" size={15} />
                  )}
                  仅测试连接
                </button>
                <button type="submit" className="primary">
                  {busy === "save" ? (
                    <LoaderCircle className="spin" size={15} />
                  ) : (
                    <Check size={15} />
                  )}
                  验证并保存
                </button>
                <button
                  type="button"
                  className="text-button"
                  onClick={() => void load()}
                >
                  重新加载
                </button>
                {dirty && <span className="small muted">有未保存的修改</span>}
              </div>
            </fieldset>
          </form>
          {message && (
            <p className="model-message" role="status">
              {message}
            </p>
          )}
          <div className="model-supporting">
            <div>
              <strong>嵌入检索 · 本机 Ollama</strong>
              <span>
                {saved.embedding.model} · {saved.embedding.dimensions} 维
              </span>
              <small>保留当前模型，避免已有记忆索引失效。</small>
            </div>
            <div>
              <strong>截图识别 · 本机 PaddleOCR</strong>
              <span>
                {saved.ocr.detection_model}
                <br />
                {saved.ocr.recognition_model}
              </span>
              <small>每五秒采集及去重规则保持不变。</small>
            </div>
          </div>
          <p className="small muted">
            保存会先使用合成文字验证接口，成功后即时应用并在重启后保留。已发出的请求会完成，尚未发出的旧配置任务将重新尝试。
          </p>
        </>
      )}
    </section>
  );
}
