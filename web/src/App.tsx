import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import {
  api,
  type Artifact,
  type GuestSummary,
  type RequestItem,
  type RunView,
  type ToolActivity,
} from "./api";
import {
  AgentMark,
  BellIcon,
  ClockIcon,
  DoorIcon,
  RefreshIcon,
  SendIcon,
  TowelsIcon,
  WrenchIcon,
} from "./icons";
import HotelBoard, { RoomCard, useBoardChanges } from "./HotelBoard";
import ManagerPanel, { DecidedList, itemText, ReviewCard } from "./ManagerPanel";
import PolicyPanel from "./PolicyPanel";
import { Markdown, shortId } from "./markdown";
import ScenarioGuide, { ScenarioPicker } from "./ScenarioGuide";
import type { Scenario } from "./scenarios";
import { forgetStoredSessions, useDemoSession, type Turn } from "./useDemoSession";
import { useHotelDesk } from "./useHotelDesk";
import { clock } from "./WorldPanel";

const SIMULATION_LABEL =
  "Simulation: fictional hotel, guests and operations. No real staff are contacted.";

/** Per-guest backdrop (generated, fictional scenery). No guest, or an unknown one, shows the reception desk. */
const GUEST_SCENES: Record<string, string> = {
  "G-001": "emma",
  "G-002": "daniel",
  "G-003": "sofia",
};

/**
 * Example requests that fit the stay: in-room services need a checked-in guest. One of
 * each kind the agent handles: a hotel question, a service request, a report, a checkout.
 */
const STAY_SUGGESTIONS = [
  "When does the pool open?",
  "Could I get two extra towels?",
  "Please clean my room.",
  "The air conditioning isn't cooling.",
  "Can I check out at 14:00 instead?",
];
const ARRIVAL_SUGGESTIONS = [
  "What time is check-in?",
  "How early could I check in today?",
  "Can I check in at 13:00?",
  "Can I leave my bags before check-in?",
  "Is there parking at the hotel?",
];

export function formatDemoTime(iso: string | undefined): string {
  if (!iso) return "—";
  // Show the hotel-local wall clock exactly as the backend reports it (e.g. 2026-09-22 10:00).
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(iso);
  return match ? `${match[1]} ${match[2]}` : iso;
}

/** Greeting from the hotel-local demo clock, never the browser clock. */
function greeting(demoIso: string | undefined): string {
  const hour = Number(/T(\d{2}):/.exec(demoIso ?? "")?.[1] ?? NaN);
  if (Number.isNaN(hour)) return "Welcome";
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");
}

function statusLabel(turn: Turn): string {
  if (turn.phase === "submitting") return "Sending…";
  if (turn.phase === "rejected") return "Not accepted";
  const status = turn.run?.status ?? "queued";
  return {
    queued: "Queued…",
    running: "Working…",
    completed: "Completed",
    failed: "Failed",
    interrupted: "Interrupted",
  }[status];
}

const ISO_DATE_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

function summaryValue(value: unknown): string {
  if (Array.isArray(value)) return value.map(String).join(", ");
  const text = String(value);
  return ISO_DATE_TIME.test(text) ? `${formatDemoTime(text)} (hotel time)` : text;
}

/** Plain-language step names. The tool name itself is always shown next to it. */
const TOOL_LABELS: Record<string, string> = {
  get_my_reservation: "Read the reservation",
  get_hotel_policy: "Looked up hotel policy",
  get_my_requests: "Read the guest's requests",
  create_housekeeping_task: "Queued a housekeeping task",
  create_maintenance_task: "Reported a maintenance issue",
  evaluate_late_checkout: "Evaluated a late checkout",
  request_late_checkout: "Requested a late checkout",
  evaluate_early_check_in: "Evaluated an early check-in",
  request_early_check_in: "Requested an early check-in",
};

/** A housekeeping request the rules refused created nothing; its step must not say "queued". */
const HOUSEKEEPING_OUTCOME_LABELS: Record<string, string> = {
  denied: "Housekeeping request refused by the rules",
  already_requested: "Room cleaning already requested",
};

function toolLabel(name: string | null, outcome?: unknown): string {
  if (!name) return "Tool call";
  if (name === "create_housekeeping_task" && typeof outcome === "string") {
    const label = HOUSEKEEPING_OUTCOME_LABELS[outcome];
    if (label) return label;
  }
  return TOOL_LABELS[name] ?? name.replace(/_/g, " ");
}

function ActivityItem({ item }: { item: ToolActivity }) {
  const summary = Object.entries(item.summary ?? {})
    .filter(([k, v]) => !(k === "replayed" && v === false)) // only a replay is worth showing
    .map(([k, v]) => `${k.replace(/_/g, " ")}: ${summaryValue(v)}`)
    .join(" · ");
  return (
    <li className={`activity activity-${item.status}`} data-testid="activity-item">
      <span className="activity-dot" aria-hidden="true" />
      <div className="activity-body">
        <div className="activity-head">
          <span className="activity-title">{toolLabel(item.tool_name, item.summary?.outcome)}</span>
          {item.duration_ms != null && <span className="activity-time">{item.duration_ms} ms</span>}
        </div>
        <div className="activity-tags">
          <code className="chip">{item.tool_name}</code>
          <span className={`badge badge-${item.status}`}>{item.status}</span>
        </div>
        {item.entity_id && (
          <div className="activity-meta">
            {`${item.entity_type ?? "entity"} `}
            <code title={item.entity_id}>{shortId(item.entity_id)}</code>
          </div>
        )}
        {summary && <div className="activity-summary">{summary}</div>}
        {item.error_code && <div className="activity-error">Error: {item.error_code}</div>}
      </div>
    </li>
  );
}

/**
 * The tool steps behind one reply, attached to that reply (not a separate list). Open while
 * the request runs, collapsed to a one-line summary afterwards.
 */
function StepStrip({ activity, live }: { activity: ToolActivity[]; live: boolean }) {
  if (activity.length === 0) return null;
  const stopped = activity.filter((a) => a.status === "rejected" || a.status === "failed").length;
  const names = [...new Set(activity.map((a) => toolLabel(a.tool_name, a.summary?.outcome)))].join(" · ");
  return (
    <details className="steps" open={live || undefined} data-testid="steps">
      <summary>
        <span className="steps-count">
          {activity.length} tool step{activity.length === 1 ? "" : "s"}
        </span>
        <span className="steps-names">{names}</span>
        {stopped > 0 && <span className="pill pill-stopped">{stopped} not completed</span>}
      </summary>
      <ol className="activity-list">
        {activity.map((a, i) => (
          <ActivityItem key={a.tool_call_id ?? i} item={a} />
        ))}
      </ol>
    </details>
  );
}

const KIND_LABELS: Record<string, string> = {
  housekeeping: "Housekeeping request",
  housekeeping_task: "Housekeeping request",
  maintenance: "Maintenance report",
  maintenance_task: "Maintenance report",
  checkout_change: "Checkout changed",
  check_in_change: "Check-in moved earlier",
  approval: "Late checkout review",
  late_checkout_review: "Late checkout review",
};

const REVIEW_STATUS: Record<string, string> = {
  pending: "Pending manager review — checkout not changed yet",
  executed: "Approved by a manager — checkout changed",
  rejected: "Declined by a manager — checkout not changed",
  expired: "Expired before a decision — checkout not changed",
  stale: "Hotel data changed before a decision — checkout not changed",
};

function isReview(kind: string | null | undefined): boolean {
  return kind === "approval" || kind === "late_checkout_review";
}

const CATEGORY_LABELS: Record<string, string> = {
  hvac: "Air conditioning / heating",
  plumbing: "Plumbing",
  electrical: "Electrical",
  other: "Other issue",
};

function isMaintenance(kind: string | null | undefined): boolean {
  return kind === "maintenance" || kind === "maintenance_task";
}

function isHousekeeping(kind: string | null | undefined): boolean {
  return kind === "housekeeping" || kind === "housekeeping_task";
}

function KindIcon({ kind }: { kind: string | null | undefined }) {
  let icon: ReactNode = <ClockIcon />;
  if (isMaintenance(kind)) icon = <WrenchIcon />;
  else if (isHousekeeping(kind)) icon = <TowelsIcon />;
  else if (kind === "checkout_change" || kind === "check_in_change") icon = <DoorIcon />;
  return (
    <span className="kind-icon" aria-hidden="true">
      {icon}
    </span>
  );
}

/** Truthful status wording: pending is never presented as delivered or done. */
export function statusText(status: string | null | undefined, kind?: string | null): string {
  if (isReview(kind)) return REVIEW_STATUS[status ?? ""] ?? status ?? "Unknown";
  switch (status) {
    case "pending":
      return isMaintenance(kind)
        ? "Pending — reported to simulated staff, not yet repaired"
        : "Pending — recorded for simulated staff, not yet done";
    case "in_progress":
      return "In progress (simulated staff)";
    case "completed":
      return "Completed (in the simulator)";
    case "cancelled":
      return "Cancelled";
    default:
      return status ?? "Unknown";
  }
}

function describe(details: Record<string, unknown>): string {
  if (details.item) return itemText(details);
  if (details.requested_checkout_local) {
    return `until ${formatDemoTime(String(details.requested_checkout_local))} (hotel time)`;
  }
  if (details.category) {
    const category = String(details.category);
    return CATEGORY_LABELS[category] ?? category;
  }
  return "";
}

function receiptStatus(artifact: Artifact): string {
  if (artifact.kind === "checkout_change") return "Applied — checkout updated in the simulator";
  if (artifact.kind === "check_in_change") {
    return "Applied — check-in time updated in the simulator (arrival at the desk still to come)";
  }
  return statusText(artifact.current_status, artifact.kind);
}

/**
 * The status the receipt recorded, when the request has moved on since (a task completed, a
 * review decided). The reply above was written at that moment; the receipt says what is true
 * now, and this keeps both readable side by side.
 */
export function recordedStatus(artifact: Artifact): string | null {
  const r = artifact.result;
  const then = typeof r.status === "string" ? r.status : typeof r.approval_status === "string" ? r.approval_status : null;
  if (!then || !artifact.current_status || then === artifact.current_status) return null;
  return then.replace(/_/g, " ");
}

/** Colour family for a status dot: waiting (amber), done (green), stopped (grey). */
function tone(status: string | null | undefined, kind?: string | null): string {
  if (kind === "checkout_change" || kind === "check_in_change") return "done";
  if (status === "executed" || status === "completed") return "done";
  if (status === "pending" || status === "in_progress") return "waiting";
  return "stopped";
}

function Receipt({ artifact }: { artifact: Artifact }) {
  const label = KIND_LABELS[artifact.kind ?? ""] ?? artifact.kind ?? "Record";
  const r = artifact.result;
  const summary =
    artifact.kind === "checkout_change"
      ? `${formatDemoTime(String(r.previous_checkout_local))} → ${formatDemoTime(
          String(r.current_checkout_local),
        )}`
      : artifact.kind === "check_in_change"
        ? `${formatDemoTime(String(r.previous_check_in_local))} → ${formatDemoTime(
            String(r.current_check_in_local),
          )}`
        : describe(r);
  return (
    <div className="receipt card-row" data-testid="receipt" role="status">
      <KindIcon kind={artifact.kind} />
      <div className="card-row-body">
        <div className="receipt-title">
          <span className="receipt-label">{label}</span>
          <span className="receipt-kicker">Receipt</span>
        </div>
        {summary && <div className="receipt-summary">{summary}</div>}
        <div className="receipt-id">
          ID <code title={artifact.id ?? undefined}>{shortId(artifact.id ?? "")}</code>
        </div>
        <div
          className={`status status-${tone(artifact.current_status, artifact.kind)}`}
          data-testid="receipt-status"
        >
          <span className="status-dot" aria-hidden="true" />
          {recordedStatus(artifact) ? "Now: " : ""}
          {receiptStatus(artifact)}
        </div>
        {recordedStatus(artifact) && (
          <div className="receipt-then" data-testid="receipt-recorded">
            Recorded as {recordedStatus(artifact)} when the agent replied
          </div>
        )}
      </div>
    </div>
  );
}

function RequestCard({ r }: { r: RequestItem }) {
  return (
    <li className="request card-row" data-testid="request-item">
      <KindIcon kind={r.kind} />
      <div className="card-row-body">
        <div className="request-head">
          <span className="request-title">{KIND_LABELS[r.kind] ?? r.kind}</span>
        </div>
        <div className="activity-summary">
          {describe(r.details)}
          {r.details.notes ? ` · “${String(r.details.notes)}”` : ""}
          {r.details.guest_description ? (
            <div className="guest-text">
              Your description: “{String(r.details.guest_description)}”
            </div>
          ) : null}
        </div>
        <div className={`status status-${tone(r.status, r.kind)}`}>
          <span className="status-dot" aria-hidden="true" />
          {statusText(r.status, r.kind)}
        </div>
        <div className="activity-meta">
          <code title={r.id}>{shortId(r.id)}</code>
        </div>
      </div>
    </li>
  );
}

function RequestsPanel({
  requests,
  hasOlder,
  loadingOlder,
  onLoadOlder,
}: {
  requests: RequestItem[];
  hasOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => void;
}) {
  const tasks = requests.filter((r) => !isReview(r.kind));
  const reviews = requests.filter((r) => isReview(r.kind));
  return (
    <section className="panel" aria-labelledby="requests-title">
      <h2 id="requests-title" className="panel-subhead">
        Requests
      </h2>
      {tasks.length === 0 ? (
        <p className="empty">No requests for this reservation yet.</p>
      ) : (
        <ol className="request-list">
          {tasks.map((r) => (
            <RequestCard key={r.id} r={r} />
          ))}
        </ol>
      )}
      <h2 id="reviews-title" className="panel-subhead">
        Manager reviews
      </h2>
      {reviews.length === 0 ? (
        <p className="empty">No late checkout reviews. Requests after 14:00 wait for a manager.</p>
      ) : (
        <ol className="request-list">
          {reviews.map((r) => (
            <RequestCard key={r.id} r={r} />
          ))}
        </ol>
      )}
      {hasOlder && (
        <button
          type="button"
          className="link-button show-older"
          onClick={onLoadOlder}
          disabled={loadingOlder}
          data-testid="show-older"
        >
          {loadingOlder ? "Loading…" : "Show older requests"}
        </button>
      )}
    </section>
  );
}

/**
 * Where a turn the hotel started comes from: what changed and when, on the hotel
 * clock. It stands where the guest's own words would, so cause and reply read together.
 */
function HotelUpdateMark({ run }: { run: RunView }) {
  const updates = run.hotel_updates ?? [];
  const at = updates[0]?.demo_time;
  return (
    <div className="hotel-update" data-testid="hotel-update">
      <span className="hotel-update-icon" aria-hidden="true">
        <BellIcon />
      </span>
      <div>
        <div className="hotel-update-label">
          <span className="sr-only">The hotel sent an update, not you. </span>
          Hotel update{at ? ` · ${clock(at)}` : ""}
        </div>
        {updates.length > 0 ? (
          <ul className="hotel-update-list">
            {updates.map((u, i) => (
              <li key={`${u.kind}-${i}`}>{u.title}</li>
            ))}
          </ul>
        ) : (
          <div>{run.user_message}</div>
        )}
      </div>
    </div>
  );
}

export function TurnView({ turn }: { turn: Turn }) {
  const run: RunView | null = turn.run;
  const pending = turn.phase === "submitting" || turn.phase === "accepted";
  const fromHotel = run?.initiated_by === "hotel";
  return (
    <li className={fromHotel ? "turn turn-hotel" : "turn"} data-testid="turn" aria-busy={pending}>
      {fromHotel && run ? (
        <HotelUpdateMark run={run} />
      ) : (
        <div className="bubble user">
          <span className="sr-only">You said: </span>
          {turn.message}
        </div>
      )}
      <div className="turn-status" data-testid="turn-status">
        {pending && <span className="typing" aria-hidden="true" />}
        {statusLabel(turn)}
      </div>
      {(run?.final_message ||
        run?.artifacts.length ||
        run?.error ||
        run?.activity.length ||
        (pending && turn.liveText)) && (
        <div className="agent-block">
          <span className="agent-avatar" aria-hidden="true">
            <AgentMark />
          </span>
          <div className="agent-stack">
            <StepStrip activity={run?.activity ?? []} live={pending} />
            {pending && turn.liveText && !run?.final_message && (
              // Provisional: streamed while the agent works; the stored reply
              // replaces it when the request finishes.
              <div className="bubble agent md streaming" data-testid="agent-streaming">
                <span className="sr-only">Agent is replying…</span>
                <div aria-hidden="true">
                  <Markdown text={turn.liveText} />
                </div>
                <span className="caret" aria-hidden="true" />
              </div>
            )}
            {run?.final_message && (
              <div className="bubble agent md" data-testid="agent-reply">
                <span className="sr-only">Agent replied: </span>
                <Markdown text={run.final_message} />
              </div>
            )}
            {run?.artifacts.map((a) => <Receipt key={a.receipt_id} artifact={a} />)}
            {run?.error && (
              <div className="bubble error" role="alert" data-testid="run-error">
                {run.error.message ?? "The request failed."} <code>{run.error.code}</code>
                {run.error.retryable && " You can try again with a new message."}
              </div>
            )}
          </div>
        </div>
      )}
      {run?.outcome_detail === "action_committed_response_failed" && (
        <div className="bubble warning" role="status" data-testid="committed-warning">
          An action was recorded before the reply failed. Check the receipts before asking again.
        </div>
      )}
      {run?.turn_forgotten && (
        <div className="notice" role="status" data-testid="turn-forgotten">
          The agent will not remember this request; the earlier conversation is kept. Hotel
          records are intact.
        </div>
      )}
      {run?.conversation_restarted && (
        <div className="notice" role="status" data-testid="context-restarted">
          The agent&apos;s memory of this conversation was restarted after this request (the
          transcript above stays visible). Hotel records are intact.
        </div>
      )}
      {turn.error && !run?.error && (
        <div className="bubble error" role="alert" data-testid="turn-error">
          {turn.error.message} <code>{turn.error.code}</code>
        </div>
      )}
    </li>
  );
}

type SideTab = "requests" | "manager" | "policy";
type Mode = "explore" | "guided";

const TAB_LABELS: Record<SideTab, string> = {
  requests: "Requests",
  manager: "Manager",
  policy: "Hotel policy",
};

/** The policy topics the agent read in its latest reply, from the run's recorded tool steps.
 * One `get_hotel_policy` step can read several topics. */
/**
 * Whether the transcript scrolls inside its own pane (wide screens) rather than with the page.
 * The overflow decides it, not the heights alone: on a narrow screen the pane's overflow is
 * visible, and while a reply grows its content can measure a few pixels taller than its box.
 */
function scrollsItself(pane: HTMLElement): boolean {
  return getComputedStyle(pane).overflowY !== "visible" && pane.scrollHeight > pane.clientHeight;
}

export function policyTopicsRead(turns: Turn[]): string[] {
  const last = [...turns].reverse().find((t) => t.run && t.run.activity.length > 0);
  return (last?.run?.activity ?? [])
    .filter((a) => a.tool_name === "get_hotel_policy" && a.status === "completed")
    .flatMap((a) => (Array.isArray(a.summary?.topics) ? a.summary.topics.map(String) : []))
    .filter(Boolean);
}

/**
 * The one screen, in two modes. Explore: the hotel board on top
 * (every room as the rules see it now, with the simulated changes beside the facts they
 * change), the chosen guest's chat below it, and the guest's records and the manager's desk
 * beside the chat. Guided tour: the story, the guest's chat, and the story's room with its
 * reviews, read only; the hotel changes only through the story's steps.
 */
export default function App() {
  const demo = useDemoSession();
  const {
    refresh,
    token,
    session,
    turns,
    requests,
    hasOlderRequests,
    loadingOlder,
    loadOlder,
    reservation,
    notice,
    restoreProblem,
    retryRestore,
    switching,
    active,
    selectGuest,
    newConversation,
    send,
  } = demo;
  // A finished guest turn may have filed a review or a task: the desk re-reads then. Only a
  // newly finished turn counts; clearing the transcript (a start-over) must not trigger a read.
  const finishedTurns = turns.filter((t) => t.phase === "done" || t.phase === "rejected").length;
  const [deskKey, setDeskKey] = useState(0);
  const seenFinished = useRef(0);
  useEffect(() => {
    if (finishedTurns > seenFinished.current) setDeskKey((k) => k + 1);
    seenFinished.current = finishedTurns;
  }, [finishedTurns]);
  const desk = useHotelDesk(deskKey, refresh);
  // Bumped by every start over: what the screen remembers of the old hotel (the board's
  // last change, its notices, the open audit log) goes with it.
  const [worldKey, setWorldKey] = useState(0);
  const changes = useBoardChanges(desk.world, worldKey);
  const [providerMissing, setProviderMissing] = useState(false);
  const [guests, setGuests] = useState<GuestSummary[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [tab, setTab] = useState<SideTab>("requests");
  const [mode, setMode] = useState<Mode>("explore");
  const [scenario, setScenario] = useState<Scenario | null>(null);
  const [run, setRun] = useState(0); // remounts the guide on every (re)start
  const [starting, setStarting] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const transcriptEnd = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLFormElement>(null);

  useEffect(() => {
    api
      .guests()
      .then((r) => setGuests(r.guests))
      .catch(() => setLoadError("Could not load demo guests. Is the backend running?"));
    // Say before the first message, not after it, that the agent cannot answer without a key.
    api
      .health()
      .then((h) => setProviderMissing(!h.provider_configured))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (token && !switching) inputRef.current?.focus();
  }, [token, switching]);

  // Whether the reader is at the end of the conversation, as of their last scroll. It is the
  // end of the conversation, not of the page: on a narrow screen the guest's records follow
  // the chat, so the page's end is never where the latest reply is. A reply that lengthens
  // the transcript does not scroll anything, so it does not count as the reader moving away.
  const atEnd = useRef(true);
  useEffect(() => {
    const update = () => {
      const end = transcriptEnd.current;
      const pane = scrollRef.current;
      if (!end || !pane) return;
      const bottom = scrollsItself(pane) ? pane.getBoundingClientRect().bottom : window.innerHeight;
      const top = end.getBoundingClientRect().top;
      atEnd.current = top >= 0 && top <= bottom + 120;
    };
    // Scroll events do not bubble; capturing sees the page's and the pane's.
    document.addEventListener("scroll", update, { capture: true, passive: true });
    return () => document.removeEventListener("scroll", update, { capture: true });
  }, []);

  const follow = useCallback(() => {
    const pane = scrollRef.current;
    if (pane && scrollsItself(pane)) {
      // The transcript scrolls inside its own pane: never move the page.
      pane.scrollTop = pane.scrollHeight;
      return;
    }
    // The pane grows with the page (narrow screens): put the message box at the bottom of the
    // screen, so the latest turn stands just above it and the guest can answer without
    // scrolling. Merely keeping the box visible is not enough: with the page scrolled to its
    // end (the guest's records below the chat), the box shows while the reply is above it.
    (composerRef.current ?? transcriptEnd.current)?.scrollIntoView?.({ block: "end" });
  }, []);

  // A new turn is always brought into view. After that the conversation is followed as it
  // grows (the reply streams in, the final reply and its receipts arrive) only while the
  // reader is at its end, never pulling them back from reading elsewhere.
  const turnKeys = turns.map((t) => t.clientRequestId).join("|");
  const keysSeen = useRef("");
  useEffect(() => {
    const grew = turnKeys.length > keysSeen.current.length;
    keysSeen.current = turnKeys;
    if (grew) follow();
  }, [turnKeys, follow]);
  const transcriptRef = useCallback(
    (el: HTMLOListElement | null) => {
      if (!el || typeof ResizeObserver === "undefined") return;
      const observer = new ResizeObserver(() => {
        if (atEnd.current && el.childElementCount > 0) follow();
      });
      observer.observe(el);
      return () => observer.disconnect();
    },
    [follow],
  );

  const selected = session?.context.guest_id ?? null;
  const scene = (selected && GUEST_SCENES[selected]) || "lobby";
  const canSend = Boolean(token) && !active && !switching && draft.trim().length > 0;
  const hotelTime = desk.world?.demo_time ?? session?.demo_time;
  const firstName = session?.context.guest_name.split(" ")[0] ?? "";

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    if (!canSend) return;
    const message = draft.trim();
    setDraft("");
    void send(message).then((result) => {
      // The hotel spoke first: its turn is shown now; the guest's words are kept.
      if (result === "busy") setDraft((current) => current || message);
    });
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter that confirms an input-method composition (Japanese, Chinese, …) is not a send.
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter" && !e.shiftKey) submit(e);
  };

  const suggest = (text: string) => {
    setDraft(text);
    inputRef.current?.focus();
  };

  /** Discard the whole fictional hotel and forget every guest session with it. */
  const wipe = async () => {
    demo.leave();
    await desk.reset();
    forgetStoredSessions();
    setWorldKey((n) => n + 1);
  };

  const startScenario = async (next: Scenario) => {
    setStarting(true);
    setProblem(null);
    setMode("guided");
    try {
      await wipe();
      setScenario(next);
      setRun((n) => n + 1);
      await demo.startConversation(next.guestId);
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "The scenario could not start.");
    } finally {
      setStarting(false);
    }
  };

  /** Leave the tour; the hotel stays exactly as the story left it. */
  const explore = () => {
    setScenario(null);
    setMode("explore");
  };

  const startOver = async () => {
    const sure = window.confirm(
      "Start over from a clean hotel? Every conversation, request, decision and hotel change " +
        "is discarded, and the hotel clock goes back to 10:00.",
    );
    if (!sure) return;
    setStarting(true);
    setProblem(null);
    try {
      await wipe();
      setScenario(null);
      setMode("explore");
      setTab("requests");
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "The hotel could not be reset.");
    } finally {
      setStarting(false);
    }
  };

  const arriving = session?.context.display_status.toLowerCase().includes("arriv") ?? false;
  // The authoritative reservation decides which examples make sense (not the display label).
  const suggestions = reservation?.status === "confirmed" ? ARRIVAL_SUGGESTIONS : STAY_SUGGESTIONS;
  const waiting = desk.pending.length;
  const storyReviews = scenario
    ? desk.approvals.filter((a) => a.room_number === scenario.room)
    : [];
  const storyPending = storyReviews.filter((a) => a.status === "pending");
  const storyDecided = storyReviews.filter((a) => a.status !== "pending");

  const chatSection = (
    <section className="chat glass" aria-labelledby="chat-title">
      <h2 id="chat-title" className="sr-only">
        Conversation
      </h2>
      <div className="chat-scroll" ref={scrollRef}>
        {!token && !switching && scenario && (
          <div className="welcome">
            <p className="empty" data-testid="story-no-conversation">
              This story&apos;s conversation is not open. Press Restart to open it again.
            </p>
          </div>
        )}
        {!token && !switching && !scenario && (
          <div className="welcome">
            <span className="welcome-mark" aria-hidden="true">
              <AgentMark />
            </span>
            <p className="welcome-title">Your concierge is ready.</p>
            <p className="empty">Pick a guest on the hotel board above to start a scoped demo conversation.</p>
            <p className="empty">
              First time here?{" "}
              <button
                type="button"
                className="link-button inline"
                onClick={() => setMode("guided")}
              >
                Take the guided tour
              </button>
              : short stories where the hotel changes while a guest waits for you, the
              manager.
            </p>
          </div>
        )}
        {switching && <p className="empty">Opening the conversation…</p>}
        {token && turns.length === 0 && !switching && (
          <div className="welcome">
            <span className="welcome-mark" aria-hidden="true">
              <AgentMark />
            </span>
            <p className="welcome-title">
              {greeting(hotelTime)}
              {firstName ? `, ${firstName}` : ""}.
            </p>
            <p className="empty">
              Ask about the stay or the hotel, or ask for something, in any language: “When is
              my checkout?”, “Havuz kaçta açılıyor?”
            </p>
            {!scenario && (
              <div className="suggestions" role="group" aria-label="Example requests">
                {suggestions.map((s) => (
                  <button key={s} type="button" className="suggestion" onClick={() => suggest(s)}>
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        <ol className="transcript" ref={transcriptRef} aria-live="polite" aria-relevant="additions">
          {turns.map((t) => (
            <TurnView key={t.clientRequestId} turn={t} />
          ))}
        </ol>
        <div ref={transcriptEnd} />
      </div>
      <form className="composer" onSubmit={submit} ref={composerRef}>
        <label htmlFor="message" className="sr-only">
          Message
        </label>
        <textarea
          id="message"
          ref={inputRef}
          value={draft}
          maxLength={4000}
          rows={1}
          placeholder={token ? `Type as ${firstName}…` : "Select a guest first"}
          disabled={!token || switching}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <button
          type="submit"
          className={active ? "send working" : "send"}
          disabled={!canSend}
          aria-label={active ? "Working…" : "Send"}
          title={active ? "Working…" : "Send (Enter)"}
        >
          <SendIcon />
        </button>
      </form>
    </section>
  );

  const exploreSide = (
    <aside className="side" aria-label="Guest and manager">
    <section className="panel guest-card" aria-labelledby="context-title">
      <h2 id="context-title" className="sr-only">
        Guest context
      </h2>
      {session ? (
        <div data-testid="guest-context">
          <div className="guest-card-head">
            <span className="avatar" aria-hidden="true">
              {initials(session.context.guest_name)}
            </span>
            <div className="guest-card-who">
              <div className="guest-card-name">{session.context.guest_name}</div>
              <div className={arriving ? "presence presence-away" : "presence"}>
                <span className="status-dot" aria-hidden="true" />
                <span>{session.context.display_status}</span>
              </div>
            </div>
            <button
              type="button"
              className="link-button new-conversation"
              onClick={() => void newConversation()}
              disabled={active || switching}
              title="Start a fresh conversation for this guest. Hotel records are kept."
              data-testid="new-conversation"
            >
              New conversation
            </button>
          </div>
          <dl className="context">
            {reservation?.status === "confirmed" && (
              <div className="context-wide">
                <dt>Check-in</dt>
                <dd data-testid="check-in-time">
                  {reservation.scheduled_check_in
                    ? `${formatDemoTime(reservation.scheduled_check_in)} (hotel time)`
                    : "—"}
                </dd>
              </div>
            )}
            <div className="context-wide">
              <dt>Checkout</dt>
              <dd data-testid="checkout-time">
                {reservation?.scheduled_checkout
                  ? `${formatDemoTime(reservation.scheduled_checkout)} (hotel time)`
                  : "—"}
              </dd>
            </div>
          </dl>
        </div>
      ) : (
        <p className="empty">No guest selected.</p>
      )}
    </section>

      <div className="side-tabs" role="tablist" aria-label="Side panel">
        {(Object.keys(TAB_LABELS) as SideTab[]).map((key) => (
          <button
            key={key}
            type="button"
            role="tab"
            id={`tab-${key}`}
            aria-selected={tab === key}
            aria-controls="side-panel"
            className="side-tab"
            onClick={() => {
              setTab(key);
              // Opening a tab shows the hotel as it is now.
              if (key === "requests") refresh();
              else void desk.refresh();
            }}
            data-testid={`tab-${key}`}
          >
            {TAB_LABELS[key]}
            {key === "manager" && waiting > 0 && (
              <span className="tab-count" data-testid="manager-count">
                {waiting}
                <span className="sr-only"> waiting</span>
              </span>
            )}
          </button>
        ))}
      </div>
      <div className="side-panel" role="tabpanel" id="side-panel" aria-labelledby={`tab-${tab}`}>
        {tab === "requests" &&
          (session ? (
            <RequestsPanel
              requests={requests}
              hasOlder={hasOlderRequests}
              loadingOlder={loadingOlder}
              onLoadOlder={() => void loadOlder()}
            />
          ) : (
            <p className="panel empty">Select a guest to see their requests.</p>
          ))}
        {tab === "manager" && <ManagerPanel key={worldKey} desk={desk} onChanged={refresh} />}
        {tab === "policy" && <PolicyPanel readTopics={policyTopicsRead(turns)} />}
        {desk.error && (
          <p className="notice" role="alert">
            {desk.error}
          </p>
        )}
      </div>
    </aside>
  );

  const guidedSide = scenario && (
    <aside className="side" aria-label="The story's room and reviews">
      <RoomCard world={desk.world} room={scenario.room} changes={changes} />
      <section className="panel manager-panel" aria-labelledby="story-reviews-title">
        <h2 id="story-reviews-title" className="panel-subhead">
          Manager reviews · room {scenario.room}
        </h2>
        {storyPending.length > 0 ? (
          <ol className="request-list">
            {storyPending.map((item) => (
              <ReviewCard key={item.approval_id} item={item} busy={desk.busy} locked />
            ))}
          </ol>
        ) : (
          <p className="empty">No review is waiting.</p>
        )}
        {storyDecided.length > 0 && <DecidedList items={storyDecided} />}
      </section>
      {desk.error && (
        <p className="notice" role="alert">
          {desk.error}
        </p>
      )}
    </aside>
  );

  return (
    <div className="shell" data-scene={scene}>
      <div className="scene" aria-hidden="true">
        {["lobby", ...Object.values(GUEST_SCENES)].map((name) => (
          <div key={name} className={`scene-layer scene-${name}${name === scene ? " on" : ""}`} />
        ))}
        <div className="scene-shade" />
      </div>
      <div
        key={session?.context.room_number ?? "desk"}
        className={session ? "room-plate" : "room-plate room-plate-desk"}
        aria-hidden="true"
      >
        {session ? session.context.room_number : "Reception"}
      </div>

      <div className="app">
        <header className="topbar">
          <div className="brand">
            <span className="brand-mark" aria-hidden="true">
              <AgentMark />
            </span>
            <div>
              <h1>Hotel Operations Agent</h1>
              <p className="sim-label" data-testid="simulation-label">
                {SIMULATION_LABEL}
              </p>
            </div>
          </div>
          <div className="topbar-right">
            <div
              className="clock"
              aria-label="Hotel time (simulated)"
              title="The fictional hotel's clock. Every policy decision and every waiting review runs on it; it moves only from the hotel board or a story step."
            >
              <span className="clock-label">Hotel time · simulated</span>
              <span className="clock-value" data-testid="demo-time">
                {formatDemoTime(hotelTime)}
              </span>
              {(desk.world?.timezone ?? session?.timezone) && (
                <span className="clock-tz">{desk.world?.timezone ?? session?.timezone}</span>
              )}
            </div>
            <nav className="topbar-links" aria-label="Demo">
              <div className="mode-switch" role="group" aria-label="Mode">
                <button
                  type="button"
                  className="mode"
                  aria-pressed={mode === "explore"}
                  disabled={starting}
                  onClick={explore}
                  data-testid="mode-explore"
                >
                  Explore
                </button>
                <button
                  type="button"
                  className="mode"
                  aria-pressed={mode === "guided"}
                  disabled={starting}
                  onClick={() => setMode("guided")}
                  data-testid="mode-guided"
                >
                  Guided tour
                </button>
              </div>
              <button
                type="button"
                className="nav-link"
                disabled={starting || active || switching}
                onClick={() => void startOver()}
                data-testid="start-over"
              >
                Start over
              </button>
            </nav>
          </div>
        </header>

        {providerMissing && (
          <p className="notice banner" role="status" data-testid="provider-missing">
            {/* The variable is not named here: the leak check refuses any bundle that
                mentions the provider's configuration. */}
            No OpenAI key is configured, so the agent cannot reply. Add your key to the{" "}
            <code>.env</code> file (see <code>env.example</code>) and restart the API; the hotel
            board, the manager&apos;s desk and the hotel policy work without it.
          </p>
        )}
        {problem && (
          <p className="notice banner" role="alert">
            {problem}
          </p>
        )}
        {mode === "explore" && (
          <HotelBoard
            key={worldKey}
            desk={desk}
            changes={changes}
            guests={guests}
            selected={selected}
            selectDisabled={active || switching || starting}
            loadError={loadError}
            onSelect={(id) => void selectGuest(id)}
            onChanged={refresh}
            onShowPolicy={() => setTab("policy")}
          />
        )}
        {mode === "guided" && !scenario && (
          <ScenarioPicker busy={starting} onStart={(s) => void startScenario(s)} />
        )}

        {notice && (
          <p className="notice banner" role="alert" data-testid="session-notice">
            {notice}
          </p>
        )}
        {restoreProblem && (
          <div className="notice banner" role="alert" data-testid="restore-problem">
            <span>{restoreProblem.message}</span>{" "}
            <button type="button" className="link-button" onClick={() => void retryRestore()}>
              <RefreshIcon /> Retry
            </button>
          </div>
        )}

        {mode === "explore" && (
          <main className="workspace">
            {chatSection}
            {exploreSide}
          </main>
        )}
        {mode === "guided" && scenario && (
          <main className="workspace tour">
            <ScenarioGuide
              key={run}
              starting={starting}
              scenario={scenario}
              demo={demo}
              desk={desk}
              onGuestChanged={refresh}
              onRestart={(s) => void startScenario(s)}
              onExit={explore}
            />
            {chatSection}
            {guidedSide}
          </main>
        )}
      </div>
    </div>
  );
}
