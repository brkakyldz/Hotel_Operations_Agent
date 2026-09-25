// Typed client for the local backend. The browser never calls OpenAI directly.

export interface GuestSummary {
  guest_id: string;
  display_name: string;
  room_number: string;
  reservation_reference: string;
  display_status: string;
}

export interface SessionContext {
  guest_id: string;
  guest_name: string;
  room_number: string;
  reservation_reference: string;
  display_status: string;
  hotel_name: string;
  timezone: string;
  demo_time: string;
}

export interface CreatedSession {
  session_id: string;
  token: string;
  context: SessionContext;
  demo_time: string;
  timezone: string;
  simulation_label: string;
}

export interface SessionInfo {
  session_id: string;
  accepted_turn_count: number;
  context_restarted_at: string | null;
  context: SessionContext;
  demo_time: string;
  timezone: string;
  simulation_label: string;
  run_ids: string[];
}

export type RunStatus = "queued" | "running" | "completed" | "failed" | "interrupted";

export interface ToolActivity {
  tool_call_id: string | null;
  tool_name: string | null;
  status: "proposed" | "completed" | "rejected" | "failed";
  proposed_at: string | null;
  finished_at: string | null;
  outcome: string | null;
  error_code: string | null;
  entity_type: string | null;
  entity_id: string | null;
  summary: Record<string, unknown>;
  duration_ms: number | null;
}

export interface Artifact {
  kind: string | null;
  id: string | null;
  receipt_id: string;
  tool_name: string;
  committed_at: string | null;
  /** Immutable creation-time result recorded in the receipt. */
  result: Record<string, unknown>;
  /** The artifact's status now (may have moved on since the receipt). */
  current_status: string | null;
}

export interface RequestItem {
  kind: string;
  id: string;
  status: string;
  status_meaning: string;
  created_at: string | null;
  room_number: string | null;
  receipt_id: string | null;
  details: Record<string, unknown>;
}

/** Authoritative reservation read (GET /api/me/reservation); hotel-local ISO times. */
export interface ReservationView {
  reservation_reference: string;
  room_number: string;
  status: string;
  display_status: string;
  scheduled_check_in: string | null;
  scheduled_checkout: string | null;
  reservation_version: number;
}

/** Operator queue item (GET /api/operator/approvals). `created_at` is real UTC; every
 * other time, `expires_at` included, is hotel-local. */
export interface OperatorApproval {
  approval_id: string;
  status: string;
  version: number;
  guest_name: string | null;
  room_number: string | null;
  reservation_reference: string | null;
  requested_checkout_local: string | null;
  current_checkout_local: string | null;
  guest_reason: string | null;
  created_at: string | null;
  expires_at: string | null;
  fresh: boolean | null;
  human_decision: string | null;
  decided_by: string | null;
  decided_at: string | null;
  status_reason: string | null;
  // Freshness detail and the simulated world changes since the request.
  observed_versions?: Record<string, number | null>;
  current_versions?: Record<string, number | null> | null;
  changed_components?: string[] | null;
  blocking_reason?: string | null;
  world_changes?: WorldChange[];
}

/** One recorded simulated world change (an event or a demo-clock move). */
export interface WorldChange {
  kind: "event" | "clock";
  key: string;
  title: string | null;
  components: string[];
  before: string | null;
  after: string | null;
  wall_time: string | null;
  demo_time: string | null;
}

export interface WorldEventItem {
  key: string;
  title: string;
  effect: string;
  components: string[];
  /** Where the hotel board offers it; `fact: null` keeps it off the board. */
  room: string | null;
  fact: "next_arrival" | "in_service" | null;
  action: string | null;
  applied: boolean;
  applied_at: string | null;
  available: boolean;
  unavailable_reason: string | null;
}

export interface BoardTask {
  kind: "housekeeping" | "maintenance";
  task_id: string;
  /** "2 towels", "room_cleaning", or a maintenance category such as "hvac". */
  what: string;
  status: "pending" | "in_progress";
}

export interface BoardUpdate {
  kind: HotelUpdateKind;
  title: string;
  demo_time: string | null;
  /** sent: the agent wrote to the guest; pending: it waits for the guest's turn to end. */
  status: "pending" | "sent" | "skipped";
  skip_reason: string | null;
}

export interface BoardReview {
  approval_id: string;
  requested_checkout: string | null;
  blocking_reason: string | null;
}

/** One demo guest's room as the checkout rules see it now (hotel-local ISO times). */
export interface BoardRoom {
  room_number: string;
  guest_id: string;
  guest_name: string;
  status: "checked_in" | "confirmed";
  check_in: string | null;
  checkout: string | null;
  in_service: boolean;
  /** The room's cleaning state: a towel request does not make a room clean. */
  housekeeping_state: "dirty" | "clean" | "inspected";
  /** The stay's open housekeeping and maintenance requests (typed fields only). */
  open_tasks: BoardTask[];
  /** The stay's waiting review, with what an approve would find now (a preview only). */
  pending_review: BoardReview | null;
  /** What the hotel last had to tell this guest, and whether the agent did. */
  latest_update?: BoardUpdate | null;
  next_arrival: string | null;
  latest_checkout: string | null;
  latest_needs_review: boolean;
  latest_blocked_by: string | null;
}

export interface BoardPolicy {
  version: number;
  standard_checkout: string;
  automatic_until: string;
  manager_until: string;
}

/** GET /api/operator/world — the demo clock, the closed catalog and the hotel board. */
export interface WorldState {
  source: string;
  demo_time: string;
  /** The hotel's timezone: the clock on screen is this hotel's, whoever is selected. */
  timezone: string;
  clock_limit: string;
  clock_steps: number[];
  events: WorldEventItem[];
  rooms: BoardRoom[];
  policy: BoardPolicy | null;
}

export interface WorldResult {
  kind: "event" | "clock";
  key: string;
  title: string;
  components: string[];
  before: string;
  after: string;
  demo_time: string;
  /** A clock move's recorded consequences; absent on events. */
  side_effects?: ClockSideEffects;
  replayed: boolean;
}

export interface ClockSideEffects {
  /** Pending reviews whose requested time the clock reached. */
  expired_reviews: string[];
  /** Room cleanings nobody had started when cleaning hours ended. */
  cancelled_tasks: string[];
}

/** Operator task board item (GET /api/operator/tasks). */
export interface OperatorTask {
  kind: "housekeeping" | "maintenance";
  task_id: string;
  status: string;
  version: number;
  guest_name: string | null;
  room_number: string | null;
  reservation_reference: string | null;
  details: Record<string, unknown>;
  created_at: string | null;
  /** Moves allowed from the current status. */
  actions: ("start" | "complete" | "cancel")[];
}

/** Sanitized audit event (GET /api/operator/events). */
export interface OperatorEvent {
  id: number;
  event_type: string;
  actor_type: string;
  run_id: string | null;
  entity_type: string | null;
  entity_id: string | null;
  tool_name: string | null;
  outcome: string | null;
  reason_codes: string[];
  wall_time: string | null;
  demo_time: string | null;
  payload: Record<string, unknown>;
}

export interface OperatorDecision {
  approval: OperatorApproval;
  decision: "approve" | "reject";
  outcome: string;
  applied: boolean;
  receipt_id: string | null;
  replayed: boolean;
}

export type HotelUpdateKind =
  | "review_closed"
  | "task_closed"
  | "room_out_of_service"
  | "review_at_risk"
  | "checkout_now_possible";

/** One change a hotel-initiated turn told the guest about. */
export interface HotelUpdateView {
  kind: HotelUpdateKind;
  title: string;
  /** Hotel-local time the change happened. */
  demo_time: string;
}

export interface RunView {
  run_id: string;
  client_request_id: string;
  status: RunStatus;
  /** "hotel": the hotel started this turn; user_message is then the update's title. */
  initiated_by?: "guest" | "hotel";
  hotel_updates?: HotelUpdateView[];
  user_message: string;
  final_message: string | null;
  error: { code: string; message: string | null; retryable: boolean } | null;
  outcome_detail: string | null;
  activity: ToolActivity[];
  artifacts: Artifact[];
  /** The whole conversational context was restarted (fallback, or a process death). */
  conversation_restarted: boolean;
  /** Only this failed request was removed from the agent's memory; earlier turns remain. */
  turn_forgotten: boolean;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  model_id: string | null;
  latency_ms: number | null;
  usage: { input_tokens: number; output_tokens: number; total_tokens: number } | null;
}

export interface ChatAccepted {
  run_id: string;
  status: RunStatus;
  poll: string;
  replayed: boolean;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly retryable: boolean,
    readonly retryAfter: number | null = null,
  ) {
    super(message);
  }
}

/** A network failure where the request may or may not have reached the server. */
export class NetworkError extends Error {}

export const TERMINAL: ReadonlySet<RunStatus> = new Set(["completed", "failed", "interrupted"]);

async function request<T>(path: string, init: RequestInit & { token?: string } = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (init.body) headers["Content-Type"] = "application/json";
  if (init.token) headers.Authorization = `Bearer ${init.token}`;
  let response: Response;
  try {
    response = await fetch(path, { ...init, headers });
  } catch (err) {
    throw new NetworkError(err instanceof Error ? err.message : "Network error");
  }
  if (!response.ok) {
    let code = "HTTP_ERROR";
    let message = `Request failed (${response.status})`;
    let retryable = response.status >= 500;
    try {
      const body = (await response.json()) as {
        error?: { code: string; message: string; retryable: boolean };
      };
      if (body.error) {
        code = body.error.code;
        message = body.error.message;
        retryable = body.error.retryable;
      }
    } catch {
      // Non-JSON error body; keep the generic message.
    }
    const retryAfter = Number(response.headers.get("Retry-After")) || null;
    throw new ApiError(response.status, code, message, retryable, retryAfter);
  }
  return (await response.json()) as T;
}

/** One policy topic exactly as `get_hotel_policy` returns it, or unavailable. */
export interface HandbookTopic {
  topic: string;
  available: boolean;
  data: Record<string, unknown> | null;
}

/** GET /api/hotel/policy — every topic the agent can read, for the on-screen handbook. */
export interface Handbook {
  source: string;
  hotel_name: string;
  hotel_timezone: string;
  demo_time: string;
  topics: HandbookTopic[];
}

/** A live progress hint for a running request; the persisted run stays authoritative. */
export interface RunSnapshot {
  /** Provisional reply text streamed so far (replaced by the final message). */
  text: string;
  /** Moves whenever tool activity changes: re-read the run to see it. */
  activity: number;
  done: boolean;
}

/**
 * Follow a run's Server-Sent Events. `fetch` rather than EventSource, so the capability
 * travels in the Authorization header and never in a URL. Resolves `true` when the server
 * said the run is done, `false` when streaming is unavailable or the stream broke; either
 * way the caller then reads the persisted run.
 */
async function streamRun(
  token: string,
  runId: string,
  onSnapshot: (snapshot: RunSnapshot) => void,
  signal: AbortSignal,
): Promise<boolean> {
  let response: Response;
  try {
    response = await fetch(`/api/runs/${encodeURIComponent(runId)}/events`, {
      headers: { Accept: "text/event-stream", Authorization: `Bearer ${token}` },
      signal,
    });
  } catch {
    return false;
  }
  const type = response.headers.get("Content-Type") ?? "";
  if (!response.ok || !response.body || !type.startsWith("text/event-stream")) return false;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return false;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let end = buffer.indexOf("\n\n");
      while (end >= 0) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        end = buffer.indexOf("\n\n");
        let event = "message";
        let data = "";
        for (const line of block.split("\n")) {
          if (line.startsWith("event: ")) event = line.slice(7);
          else if (line.startsWith("data: ")) data += line.slice(6);
        }
        if (event === "done") return true;
        if (event === "snapshot" && data) onSnapshot(JSON.parse(data) as RunSnapshot);
      }
    }
  } catch {
    return false;
  } finally {
    reader.cancel().catch(() => {});
  }
}

export const api = {
  streamRun,
  health: () =>
    request<{ status: string; simulation_label: string; provider_configured: boolean }>(
      "/api/health",
    ),
  guests: () => request<{ guests: GuestSummary[] }>("/api/demo/guests"),
  handbook: () => request<Handbook>("/api/hotel/policy"),
  createSession: (guestId: string) =>
    request<CreatedSession>("/api/demo/sessions", {
      method: "POST",
      body: JSON.stringify({ guest_id: guestId }),
    }),
  session: (token: string) => request<SessionInfo>("/api/session", { token }),
  chat: (token: string, clientRequestId: string, message: string) =>
    request<ChatAccepted>("/api/chat", {
      method: "POST",
      token,
      body: JSON.stringify({ client_request_id: clientRequestId, message }),
    }),
  run: (token: string, runId: string) =>
    request<RunView>(`/api/runs/${encodeURIComponent(runId)}`, { token }),
  reservation: (token: string) =>
    request<{ reservation: ReservationView }>("/api/me/reservation", { token }),
  // The operator side: separate routes, no credential, never a model tool.
  operatorApprovals: (status = "pending") =>
    request<{ approvals: OperatorApproval[]; next_cursor: string | null }>(
      `/api/operator/approvals?status=${encodeURIComponent(status)}`,
    ),
  operatorDecide: (
    approvalId: string,
    decision: "approve" | "reject",
    expectedVersion: number,
    clientRequestId: string,
  ) =>
    request<OperatorDecision>(
      `/api/operator/approvals/${encodeURIComponent(approvalId)}/decision`,
      {
        method: "POST",
        body: JSON.stringify({
          decision,
          expected_version: expectedVersion,
          client_request_id: clientRequestId,
        }),
      },
    ),
  world: () => request<WorldState>("/api/operator/world"),
  worldEvent: (event: string, clientRequestId: string) =>
    request<WorldResult>("/api/operator/world/events", {
      method: "POST",
      body: JSON.stringify({ event, client_request_id: clientRequestId }),
    }),
  worldReset: () =>
    request<WorldState>("/api/operator/world/reset", {
      method: "POST",
      body: JSON.stringify({ confirm: true }),
    }),
  worldClock: (minutes: number, clientRequestId: string) =>
    request<WorldResult>("/api/operator/world/clock", {
      method: "POST",
      body: JSON.stringify({ advance_minutes: minutes, client_request_id: clientRequestId }),
    }),
  operatorTasks: (status: "open" | "all" = "open") =>
    request<{ tasks: OperatorTask[]; truncated: boolean }>(
      `/api/operator/tasks?status=${encodeURIComponent(status)}`,
    ),
  operatorMoveTask: (
    task: Pick<OperatorTask, "kind" | "task_id" | "version">,
    action: "start" | "complete" | "cancel",
  ) =>
    request<{ task: OperatorTask }>(
      `/api/operator/tasks/${task.kind}/${encodeURIComponent(task.task_id)}/transition`,
      {
        method: "POST",
        body: JSON.stringify({ action, expected_version: task.version }),
      },
    ),
  operatorEvents: (beforeId: number | null = null) =>
    request<{ events: OperatorEvent[]; next_before_id: number | null }>(
      beforeId ? `/api/operator/events?before_id=${beforeId}` : "/api/operator/events",
    ),
  requests: (token: string, cursor: string | null = null) =>
    request<{ requests: RequestItem[]; next_cursor: string | null }>(
      cursor ? `/api/me/requests?cursor=${encodeURIComponent(cursor)}` : "/api/me/requests",
      { token },
    ),
};
