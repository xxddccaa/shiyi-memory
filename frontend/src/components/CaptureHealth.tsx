import { useEffect, useState } from "react";
import { api, post, type Status } from "../api";

const time = (value?: string) =>
  value ? new Date(value).toLocaleString("zh-CN") : "尚未确认";
export default function CaptureHealth({ status }: { status: Status | null }) {
  const [help, setHelp] = useState(false);
  const [error, setError] = useState("");
  const [events, setEvents] = useState<
    {
      id: string;
      reason: string;
      started_at: string;
      ended_at: string | null;
    }[]
  >([]);
  useEffect(() => {
    if (help)
      void api<{ items: typeof events }>("/capture/health-events")
        .then((v) => setEvents(v.items))
        .catch((e) => setError(e.message));
  }, [help]);
  const act = (action: string) =>
    void post("/capture/actions", { action }).catch((e) => setError(e.message));
  if (!status?.capture_health) return null;
  const health = status.capture_health;
  const permission = health.state === "permission_required";
  return (
    <section
      className={`capture-health ${permission || health.state === "error" ? "needs-attention" : ""}`}
      aria-label="采集状态"
    >
      <div className="agent-toolbar">
        <strong>{health.label}</strong>
        {
          <button className="secondary" onClick={() => setHelp(!help)}>
            查看诊断与修复
          </button>
        }
      </div>
      <p>{health.detail}</p>
      {error && <p role="alert">{error}</p>}
      {help && (
        <div className="permission-help">
          <div className="agent-toolbar">
            <button onClick={() => act("accessibility")}>
              打开辅助功能设置
            </button>
            <button onClick={() => act("screen_recording")}>
              打开屏幕录制设置
            </button>
            <button onClick={() => act("locate_app")}>定位当前应用</button>
            <button onClick={() => act("reopen")}>重开采集端并复查</button>
          </div>
          {events.map((event) => (
            <p key={event.id}>
              {time(event.started_at)} ·{" "}
              {(
                {
                  permission_required: "权限尚未生效",
                  error: "链路故障",
                  stalled: "链路停滞",
                  offline: "采集端离线",
                } as Record<string, string>
              )[event.reason] || "采集异常"}{" "}
              · {event.ended_at ? "已结束 " + time(event.ended_at) : "仍需检查"}
            </p>
          ))}
          <p>
            在 macOS
            系统设置的“隐私与安全性”中，分别检查“辅助功能”和“屏幕录制”，移除旧拾忆条目后，添加本项目
            macos/WorkMemory.app，再重开拾忆。
          </p>
          <p>
            旧条目仍无法更新时，可双击项目内“修复拾忆权限.command”，按提示完成系统授权。它会清除本应用的旧授权，不会重新构建或签名。
          </p>
          <p>
            默认定时模式无需输入监控。完成后切到普通工作窗口，在这里核对截图、OCR与新入库时间；暂停状态不会自动开启。
          </p>
        </div>
      )}
      <details>
        <summary>查看采集链路时间</summary>
        <dl>
          <dt>最近截图</dt>
          <dd>{time(status.collector?.last_screenshot_at)}</dd>
          <dt>最近OCR</dt>
          <dd>{time(status.collector?.last_ocr_at)}</dd>
          <dt>最近收到上传</dt>
          <dd>{time(status.capture_receipts?.received_at)}</dd>
          <dt>最新入库记录的采集时间</dt>
          <dd>{time(status.capture_receipts?.last_observation_at)}</dd>
        </dl>
        {status.collector?.telemetry_version === 2 && (
          <p>
            实际截图 {status.collector.screenshots ?? 0} · OCR请求{" "}
            {status.collector.ocr_attempts ?? 0} / 成功{" "}
            {status.collector.ocr_successes ?? 0} · 忙碌跳过{" "}
            {status.collector.busy_skips ?? 0}
          </p>
        )}
        <p className="muted small">
          收到上传可能来自旧队列；重复页面被过滤时，入库时间不会前进。
        </p>
      </details>
    </section>
  );
}
