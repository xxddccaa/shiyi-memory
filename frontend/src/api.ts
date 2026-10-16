const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has("token")) {
  sessionStorage.setItem("workmemory-token", fragment.get("token")!);
  history.replaceState(null, "", location.pathname + location.search);
}
export const getToken = () => sessionStorage.getItem("workmemory-token") || "";
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch("/api" + path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: "Bearer " + getToken(),
      ...init?.headers,
    },
  });
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401)
      window.dispatchEvent(new Event("auth-expired"));
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "请求无法处理，请检查输入。",
    );
  }
  return data as T;
}
export const post = <T>(path: string, data: unknown = {}) =>
  api<T>(path, { method: "POST", body: JSON.stringify(data) });
export interface Episode {
  id: string;
  started_at: string;
  ended_at: string;
  summary: string | null;
  project: string | null;
  project_id: string | null;
  status: string;
  revision: number;
  corrected: boolean;
  evidence_state?: string;
  evidence_label?: string;
  evidence_expires_at?: string | null;
  evidence_kind?: "screen_text" | "user_confirmed";
}
export interface Project {
  id: string;
  name: string;
  state: string;
  episodes: number;
  last_active: string;
}
export const DAILY_WORK_LABEL = "日常工作";
export const DAILY_WORK_SCOPE = "__daily_work__";
export interface DailyWorkGroup {
  name: string;
  episodes: number;
  last_active: string | null;
}
export function workScopeQuery(value: string) {
  return value === DAILY_WORK_SCOPE
    ? "unassigned=true"
    : value ? "project_id=" + encodeURIComponent(value) : "";
}
export interface Todo {
  id: string;
  revision: number;
  project_id: string | null;
  episode_id: string | null;
  text: string;
  confirmed: boolean;
  paused: boolean;
  timezone: string;
  repeat: string;
  weekdays: number[];
  notify_pet: boolean;
  conversation_id: string | null;
  done: boolean;
  due_at: string | null;
}
export interface Reminder {
  revision: number;
  due_at: string;
  planned_at: string | null;
  status: string;
  needs_display: boolean;
  missed_count: number;
  todo_id: string | null;
  todo_revision: number | null;
  conversation_id: string | null;
  id: string;
  kind: string;
  text: string;
  episode_id: string | null;
}
export interface Status {
  capture_health?: { state: string; label: string; detail: string };
  capture_receipts?: { received_at?: string; event_at?: string; last_observation_at?: string };
  collector?: {
    telemetry_version?: number;
    screenshots?: number;
    ocr_attempts?: number;
    ocr_successes?: number;
    busy_skips?: number;
    last_screenshot_at?: string;
    running: boolean;
    paused: boolean | null;
    accessibility_granted: boolean | null;
    screen_recording_granted: boolean | null;
    ocr_mode: "fallback" | "always" | "off";
    capture_mode?: "interaction" | "timer";
    interval_seconds?: number;
    input_monitoring_granted?: boolean;
    capture_active?: boolean;
    screen_locked?: boolean;
    pending_groups?: number;
    merged_groups?: number;
    dropped_groups?: number;
    spooled_batches?: number;
    image_bytes?: number;
    last_capture_at?: string;
    last_ocr_at?: string;
    last_ocr_seconds?: number;
    last_method?: string;
    last_error?: string;
  };
  ocr?: {
    ready: boolean;
    versions?: { paddleocr: string };
    detection_model?: string;
    recognition_model?: string;
  };
  capture_statistics?: {
    observations: number;
    filtered: number;
    dedup_ratio: number;
  };
  backend: "reme" | "graphiti";
  jobs: Record<string, number>;
  sync: { total: number; synced: number };
  ollama: { reachable: boolean; models_ready: boolean };
  inference: {
    queued: number;
    current_model: string | null;
    recent: {
      model: string;
      seconds: number;
      wait_seconds: number;
      load_seconds: number | null;
    }[];
  };
  retention_days: number;
  errors: { kind: string; error: string; count: number }[];
}
export interface Answer {
  answer: string;
  sources: Episode[];
  warning?: string;
}
export interface Observation {
  id: string;
  captured_at: string;
  event_at?: string;
  app: string;
  title: string;
  activity_id?: string | null;
  resource_uri?: string | null;
  interaction_id: string | null;
  trigger: string | null;
  phase: string | null;
  text: string | null;
  meaningful: boolean;
  similarity: number;
  reason: string;
  method: string;
  missing_before: boolean;
  missing_after?: boolean;
  merged_count: number;
  dropped_groups: number;
}
export interface Sources {
  excerpts?: { id: string; text: string; provenance: { source_id: string; app: string; captured_at: string } }[];
  observations?: Observation[];
  episode: Episode;
  activities: {
    id: string;
    captured_at: string;
    app: string;
    title: string;
    text: string | null;
    expired: boolean;
    method: string;
  }[];
}

export interface GenerationConfig {
  provider: "ollama" | "openai_compatible";
  base_url: string;
  model: string;
  api_key_configured: boolean;
  timeout_seconds: number;
  temperature: number;
  max_tokens: number;
  context_window: number | null;
  thinking: boolean | null;
  structured_output: "json_schema" | "json_object";
}
export interface ModelConfiguration {
  revision: number;
  generation: GenerationConfig;
  local_default: GenerationConfig;
  local_models: string[];
  embedding: {
    provider: "ollama";
    base_url: string;
    model: string;
    dimensions: number;
  };
  ocr: {
    provider: "paddleocr";
    detection_model: string;
    recognition_model: string;
  };
}
export interface ModelTest {
  ok: boolean;
  seconds: number;
  structured_output_valid: boolean;
  answer: string;
  product: number;
}

export interface Evidence {
  source_kind?: "episode" | "memory" | "todo" | "project" | "chat" | "session" | "review";
  id: string;
  kind: "raw" | "summary" | "fact" | "user_confirmed" | "todo" | "project" | "session" | "review";
  content: string;
  revision: number;
  episode_id: string | null;
  project_id: string | null;
  app: string | null;
  captured_at: string | null;
  expires_at: string | null;
}
export interface Conversation {
  workspace_id: string | null;
  coding_enabled: boolean;
  revision: number;
  id: string;
  title: string;
  updated_at: string;
}
export interface AgentRun {
  error_code: string | null;
  retryable: boolean;
  retry_blocked_reason: string | null;
  id: string;
  status: string;
  error: string | null;
  model_calls: number;
  search_calls: number;
}
export interface ObjectContext {
  kind: "todo" | "project" | "memory" | "episode" | "session" | "review";
  id: string;
  revision: number;
}
export interface WorkspaceObject extends ObjectContext {
  text: string;
  evidence_id: string;
  project_id: string | null;
  done: boolean | null;
}
export interface ExternalSource {
  id: string;
  title: string;
  url: string;
  provider: string;
  retrieved_at: string;
}
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string | null;
  status: string;
  sources: Evidence[];
  external_sources?: ExternalSource[];
  context?: ObjectContext | null;
  created_at: string;
}
export interface MemoryCandidate {
  conflicts?: { id: string; text: string; revision: number; notice: string }[];
  id: string;
  text: string;
  status: "pending" | "confirmed" | "needs_review";
  origin?: { episode_id?: string; date?: string };
  revision: number;
}
export interface ArtifactItem {
  text: string;
  evidence_ids: string[];
}
export interface WorkArtifact {
  requested_hash: string;
  draft?: WorkArtifact | null;
  materials?: { id: string; revision: number; content: string; project_name: string; selected: boolean }[];
  observation_groups?: { project_name: string; items: ArtifactItem[] }[];
  processed_sources?: number;
  total_sources?: number;
  id: string;
  kind: "brief" | "review";
  status: string;
  body: {
    observations?: ArtifactItem[];
    progress: ArtifactItem[];
    decisions: ArtifactItem[];
    blockers: ArtifactItem[];
    next_steps: ArtifactItem[];
    unknowns: string[];
    coverage?: { source_count: number; total: number; truncated: boolean };
  } | null;
  user_text: string | null;
  edit_revision: number;
  needs_review: boolean;
  attempts: number;
  updated_at: string;
}
