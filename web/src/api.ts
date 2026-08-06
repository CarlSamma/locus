export type PropertyState = "unknown" | "running" | "replied" | "confirmed" | "denied";

export interface Property {
  key: string;
  weight: number;
  prior_entropy: number;
  state: PropertyState;
  votes: number;
  value: string | null;
  notes: string;
  remaining_entropy: number;
}

export interface Frame {
  alias: string;
  persona: string;
  prompt_template: string;
  status: string;
  created_at: string;
}

export interface Probe {
  id: number;
  property_key: string;
  frame: string;
  text: string;
  tweet_id: string | null;
  posted_at: string | null;
  reply_id: string | null;
  reply_text: string | null;
  classification: string | null;
  score: number;
  status: string;
  session_id: string | null;
  created_at: string;
}

export interface ReviewItem {
  id: number;
  property_key: string;
  frame: string;
  text: string;
  reply_text: string | null;
  classification: string | null;
  score: number;
  status: string;
}

export interface LedgerEntry {
  property_key: string;
  outcome: string;
  ts: string;
  probe_id: number | null;
}

export interface IntelEntry {
  id: number;
  session_id: string | null;
  kind: string;
  text: string;
  entropy_before: number;
  entropy_after: number;
  note: string;
  ts: string;
}

export interface SessionRecord {
  id: string;
  started_at: string;
  ended_at: string | null;
  probes_total: number;
  status: string;
  target: string;
}

export interface StatusData {
  properties: Property[];
  total_remaining_entropy: number;
  in_phase5: boolean;
  phase5_threshold: number;
  target: string;
  counts: Record<string, number>;
}

export interface AppConfig {
  target_handle: string;
  our_bot_handle: string;
  llm_model_primary: string;
  llm_model_hard: string;
  llm_api_base: string;
  poll_interval_seconds: number;
  poll_timeout_seconds: number;
  max_probes_per_session: number;
  phase5_entropy_threshold: number;
  similarity_threshold: number;
  dedup_top_k: number;
}

export interface RunInfo {
  session_id: string;
  dry_run: boolean;
  status: "running" | "paused" | "done";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  status: () => request<StatusData>("/status"),
  properties: () => request<Property[]>("/properties"),
  frames: () => request<Frame[]>("/frames"),
  probes: (params?: { status?: string; property_key?: string; limit?: number; offset?: number }) => {
    const q = new URLSearchParams();
    if (params?.status) q.set("status", params.status);
    if (params?.property_key) q.set("property_key", params.property_key);
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    const qs = q.toString();
    return request<{ total: number; items: Probe[] }>(`/probes${qs ? `?${qs}` : ""}`);
  },
  review: () => request<ReviewItem[]>("/review"),
  ledger: () => request<LedgerEntry[]>("/ledger"),
  intel: (params?: { kind?: string; limit?: number; offset?: number }) => {
    const q = new URLSearchParams();
    if (params?.kind) q.set("kind", params.kind);
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    const qs = q.toString();
    return request<{ total: number; items: IntelEntry[] }>(`/intel${qs ? `?${qs}` : ""}`);
  },
  sessions: () => request<SessionRecord[]>("/sessions"),
  createSession: () => request<{ session_id: string; status: string }>("/sessions", { method: "POST" }),
  run: (body: { dry_run: boolean; max_probes?: number; session_id?: string }) =>
    request<RunInfo>("/run", { method: "POST", body: JSON.stringify(body) }),
  runStatus: (sessionId: string) =>
    request<RunInfo>(`/run/${sessionId}`).catch(() => ({ session_id: sessionId, dry_run: true, status: "done" })),
  stopRun: (sessionId: string) =>
    request<{ status: string }>(`/run/${sessionId}/stop`, { method: "POST" }),
  generateProbe: (body: { property_key: string; frame_alias?: string }) =>
    request<Probe>("/probes/generate", { method: "POST", body: JSON.stringify(body) }),
  postProbe: (body: { text: string; property_key?: string; frame_alias?: string; session_id?: string }) =>
    request<{ probe_id: number; tweet_id: string; url: string }>("/probes/post", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  poll: () => request<{ replies: unknown[] }>("/probes/poll", { method: "POST" }),
  config: () => request<AppConfig>("/config"),
};
