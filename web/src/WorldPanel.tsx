import { useState } from "react";
import { api, type OperatorApproval, type OperatorEvent, type WorldChange } from "./api";
import { shortId } from "./markdown";
import "./world.css";

// The simulated world's shared pieces: plain-language reasons, the
// freshness detail that shows what hotel changes did to a pending review, and the audit
// trail. The hotel board (HotelBoard.tsx) holds the clock and the world events.

const COMPONENTS: { key: string; label: string }[] = [
  { key: "reservation", label: "Reservation" },
  { key: "room", label: "Room" },
  { key: "policy", label: "Checkout policy" },
  { key: "room_day_plan", label: "Cleaning plan" },
  { key: "operational", label: "This room's bookings and plans" },
];

export const REASON_TEXT: Record<string, string> = {
  INVALID_TIME: "the requested time has already passed on the hotel clock",
  NEXT_ARRIVAL_CONFLICT: "the room must be ready for its next arrival",
  CLEANING_WINDOW_EXCEEDED: "cleaning could not finish in today's window",
  ROOM_OUT_OF_SERVICE: "the room is out of service",
  AFTER_LATEST_EXTENSION: "it is after the latest possible extension",
  ALREADY_GRANTED: "the checkout is already at or after that time",
  NOT_CHECKED_IN: "the guest is not checked in",
  OPERATIONAL_DATA_UNAVAILABLE: "operational data is unavailable",
};

function componentLabel(key: string): string {
  return COMPONENTS.find((c) => c.key === key)?.label ?? key;
}

/** Plain-language reading of the server's `blocking_reason` / `status_reason` code. */
export function blockingText(reason: string | null | undefined): string | null {
  if (!reason) return null;
  const [kind, rest = ""] = reason.split(":", 2);
  const parts = rest.split(",").filter(Boolean);
  switch (kind) {
    case "CHANGED":
      return `hotel data changed since the request (${parts.map(componentLabel).join(", ")})`;
    case "NO_LONGER_ELIGIBLE":
      return `the policy no longer allows it: ${parts.map((p) => REASON_TEXT[p] ?? p).join("; ")}`;
    case "EXPIRED_BEFORE_DECISION":
      return "the requested time arrived on the hotel clock before a decision";
    case "PAYLOAD_INTEGRITY":
      return "the stored request failed its integrity check";
    case "SCOPE_OR_DATA_UNAVAILABLE":
      return "the reservation or its data is no longer available";
    default:
      return reason;
  }
}

export function clock(iso: string | null | undefined): string {
  const match = iso ? /T(\d{2}:\d{2})/.exec(iso) : null;
  return match ? match[1] : "—";
}

/** One simulated change in plain words: what it was, before → after, and when on the hotel clock. */
export function ChangeLine({ change }: { change: WorldChange }) {
  return (
    <li>
      <span className="world-tag">{change.kind === "clock" ? "Clock" : "Hotel"}</span>{" "}
      {change.title ?? change.key}
      {/* An event's title already says what changed; a clock move needs its from → to. */}
      {change.kind === "clock" && change.before && change.after
        ? ` — ${change.before} → ${change.after}`
        : ""}
      {change.demo_time && (
        <span className="world-when"> · hotel time {clock(change.demo_time)}</span>
      )}
    </li>
  );
}

/**
 * What an approve would do, in plain words first: the verdict, then the simulated changes
 * since the guest asked. The version comparison the server makes stays available as a
 * technical detail for readers who want the mechanism.
 */
export function FreshnessDetail({ item }: { item: OperatorApproval }) {
  const observed = item.observed_versions ?? {};
  const current = item.current_versions ?? null;
  const changed = new Set(item.changed_components ?? []);
  const blocking = blockingText(item.blocking_reason);
  const changes = item.world_changes ?? [];
  const who = item.guest_name?.split(" ")[0] ?? "the guest";
  if (item.status !== "pending" && changes.length === 0) return null;
  return (
    <div className="freshness" data-testid="freshness">
      {item.status === "pending" && (
        <p className={blocking ? "freshness-verdict blocked" : "freshness-verdict"}>
          {blocking
            ? `If you approve now, nothing will change: ${blocking}. ${who} would have to ask again, so the rules are checked against today's data.`
            : `If you approve now, the checkout changes to ${clock(item.requested_checkout_local)} (hotel time).`}
        </p>
      )}
      {changes.length > 0 && (
        <div>
          <p className="freshness-heading">What changed in the hotel since {who} asked</p>
          <ul className="world-changes">
            {changes.map((change, i) => (
              <ChangeLine key={`${change.key}-${i}`} change={change} />
            ))}
          </ul>
        </div>
      )}
      {item.status === "pending" && current && (
        <details className="freshness-tech">
          <summary>How this is checked (version comparison)</summary>
          <p className="world-note">
            The request stored the version of every record it was decided on. An approve
            re-reads them inside the same transaction; any difference means the decision was
            made on data that no longer exists.
          </p>
          <table className="freshness-table">
            <caption>Data the request saw → data now</caption>
            <tbody>
              {COMPONENTS.map(({ key, label }) => (
                <tr key={key} className={changed.has(key) ? "changed" : undefined}>
                  <th scope="row">{label}</th>
                  <td>v{observed[key] ?? "—"}</td>
                  <td>v{current[key] ?? "—"}</td>
                  <td>{changed.has(key) ? "changed" : "same"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}

/** Who acted, in the screen's own words: the operator side is you, the manager. */
const ACTORS: Record<string, string> = {
  guest: "Guest",
  agent: "Agent",
  operator: "You (hotel)",
  system: "System",
};

function eventSummary(e: OperatorEvent): string {
  const entity = e.entity_type && e.entity_id ? `${e.entity_type.replace(/_/g, " ")} ${shortId(e.entity_id)}` : null;
  const parts = [e.tool_name, entity];
  if (e.outcome) parts.push(`→ ${e.outcome}`);
  if (e.reason_codes.length) parts.push(`(${e.reason_codes.join(", ")})`);
  return parts.filter(Boolean).join(" ");
}

/** The sanitized, append-only audit trail, read only when opened. Times are hotel time. */
export function ActivityLog() {
  const [events, setEvents] = useState<OperatorEvent[] | null>(null);
  const [before, setBefore] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = async (older: number | null) => {
    if (loading) return;
    setLoading(true);
    try {
      const page = await api.operatorEvents(older);
      setEvents((shown) => (older && shown ? [...shown, ...page.events] : page.events));
      setBefore(page.next_before_id);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the activity log.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <details
      className="log"
      data-testid="activity-log"
      onToggle={(e) => {
        if ((e.currentTarget as HTMLDetailsElement).open) void load(null);
      }}
    >
      <summary>Activity log (audit trail)</summary>
      <p className="world-note">
        Every tool call, decision and hotel change, append-only and sanitized: no guest free text,
        tokens or prompts.
      </p>
      {error && (
        <p className="notice" role="alert">
          {error}
        </p>
      )}
      {events && events.length === 0 && <p className="empty">No events yet.</p>}
      <ol className="event-list">
        {(events ?? []).map((e, i, all) => (
          <li key={e.id} className="event" data-testid="operator-event">
            {/* The hotel clock moves only when you move it, so many events share a minute:
                the time is written once, where it changes. */}
            <span className="event-at">
              {i > 0 && clock(all[i - 1].demo_time) === clock(e.demo_time) ? "" : clock(e.demo_time)}
            </span>
            <span className="event-type">{e.event_type.replace(/_/g, " ")}</span>
            <span className="event-actor">{ACTORS[e.actor_type] ?? e.actor_type}</span>
            <span className="event-summary">{eventSummary(e)}</span>
          </li>
        ))}
      </ol>
      {before !== null && (
        <button
          type="button"
          className="link-button"
          disabled={loading}
          onClick={() => void load(before)}
        >
          {loading ? "Loading…" : "Show older events"}
        </button>
      )}
    </details>
  );
}
