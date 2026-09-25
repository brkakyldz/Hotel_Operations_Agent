import { useEffect, useState } from "react";
import type { OperatorApproval, OperatorDecision, OperatorTask } from "./api";
import type { HotelDesk } from "./useHotelDesk";
import { blockingText, clock, FreshnessDetail } from "./WorldPanel";

// The Manager tab: the human's side of a review and the simulated staff. A
// decision is its own HTTP command; approve re-reads the hotel and re-runs the rules in one
// transaction, so the result can differ from what the button says.

/** Human intent and applied state are shown separately. */
export function outcomeText(result: OperatorDecision): string {
  const intent = result.decision === "approve" ? "Approve" : "Reject";
  const who = result.approval.guest_name?.split(" ")[0] ?? "The guest";
  switch (result.outcome) {
    case "executed":
      return `Approved: ${who}'s checkout is now ${clock(
        result.approval.requested_checkout_local,
      )} (hotel time).`;
    case "rejected":
      return `Rejected: ${who}'s checkout was not changed.`;
    case "expired":
      return `${intent} recorded, but ${clock(
        result.approval.requested_checkout_local,
      )} had already arrived on the hotel clock. Nothing was changed.`;
    case "stale":
      return `${intent} recorded, but ${
        blockingText(result.approval.status_reason) ?? "hotel data changed since the request"
      }. Nothing was changed; ${who} must ask again.`;
    default:
      return `${intent}: ${result.outcome}.`;
  }
}

const STATUS_WORDS: Record<string, string> = {
  executed: "approved and applied",
  rejected: "rejected",
  expired: "expired on the hotel clock",
  stale: "not applied: hotel data changed",
};

const ACTION_LABELS: Record<OperatorTask["actions"][number], string> = {
  start: "Start",
  complete: "Mark completed",
  cancel: "Cancel",
};

const TASK_STATUS: Record<string, string> = {
  pending: "Pending: not started by simulated staff",
  in_progress: "In progress (simulated staff)",
  completed: "Completed in the simulator",
  cancelled: "Cancelled",
};

/** What a housekeeping request asks for: "towels × 2", or "Room cleaning" (no count). */
export function itemText(details: Record<string, unknown>): string {
  if (details.item === "room_cleaning") return "Room cleaning";
  const item = String(details.item ?? "").replace(/_/g, " ");
  return `${item} × ${String(details.quantity ?? 1)}`;
}

/** A task's state in a word or two, for its badge. */
const TASK_BADGE: Record<string, string> = {
  pending: "Not started",
  in_progress: "In progress",
};

function taskSummary(task: OperatorTask): string {
  if (task.kind === "housekeeping") return itemText(task.details);
  return `Maintenance: ${String(task.details.category ?? "other")}`;
}

/**
 * One review on the manager's desk. `locked` is the guided tour's reading: the same card and
 * freshness verdict, decided from the story's step instead of these buttons.
 */
export function ReviewCard({
  item,
  busy,
  locked = false,
  onDecide,
}: {
  item: OperatorApproval;
  busy: boolean;
  locked?: boolean;
  onDecide?: (item: OperatorApproval, decision: "approve" | "reject") => void;
}) {
  return (
    <li className="desk-item" data-testid="operator-item" data-id={item.approval_id}>
      <div className="desk-item-head">
        <span>
          <strong>{item.guest_name}</strong> · room {item.room_number}
        </span>
        {item.status !== "pending" && (
          <span className={`badge badge-${item.status}`}>{STATUS_WORDS[item.status] ?? item.status}</span>
        )}
      </div>
      <div className="desk-item-what">
        {/* A review lapses when the hotel clock reaches the time it asks for. */}
        Late checkout {clock(item.current_checkout_local)} →{" "}
        <strong>{clock(item.requested_checkout_local)}</strong>
        <span className="world-when"> · hotel time</span>
      </div>
      {item.guest_reason && <div className="guest-text">Guest&apos;s reason: “{item.guest_reason}”</div>}
      <FreshnessDetail item={item} />
      {!locked && onDecide && (
        <div className="step-actions">
          <button
            type="button"
            className="btn primary"
            disabled={busy}
            onClick={() => onDecide(item, "approve")}
          >
            Approve
          </button>
          <button type="button" className="btn" disabled={busy} onClick={() => onDecide(item, "reject")}>
            Reject
          </button>
        </div>
      )}
    </li>
  );
}

/** The decided reviews, one line each. */
export function DecidedList({ items }: { items: OperatorApproval[] }) {
  return (
    <ol className="decided-list">
      {items.map((item) => (
        <li key={item.approval_id} data-testid="decided-item" data-id={item.approval_id}>
          {item.guest_name?.split(" ")[0]} · {clock(item.requested_checkout_local)} ·{" "}
          {STATUS_WORDS[item.status] ?? item.status}
        </li>
      ))}
    </ol>
  );
}

export default function ManagerPanel({
  desk,
  onChanged,
}: {
  desk: HotelDesk;
  onChanged: () => void;
}) {
  const [message, setMessage] = useState<string | null>(null);
  const decided = desk.approvals.filter((a) => a.status !== "pending").slice(0, 5);
  // The last action's result describes the desk as it was; a new review arriving replaces it.
  const waitingIds = desk.pending.map((a) => a.approval_id).join(",");
  const [seenWaiting, setSeenWaiting] = useState(waitingIds);
  useEffect(() => {
    if (waitingIds === seenWaiting) return;
    const arrived = waitingIds.split(",").some((id) => id && !seenWaiting.split(",").includes(id));
    setSeenWaiting(waitingIds);
    if (arrived) setMessage(null);
  }, [waitingIds, seenWaiting]);

  const decide = async (item: OperatorApproval, decision: "approve" | "reject") => {
    try {
      setMessage(outcomeText(await desk.decide(item, decision)));
      onChanged();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "The decision failed.");
    }
  };

  const move = async (task: OperatorTask, action: OperatorTask["actions"][number]) => {
    try {
      const moved = await desk.moveTask(task, action);
      setMessage(
        `${taskSummary(moved)} (room ${moved.room_number}): ${TASK_STATUS[moved.status] ?? moved.status}.`,
      );
      onChanged();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "The update failed.");
    }
  };

  return (
    <section className="panel manager-panel" aria-labelledby="manager-title">
      <h2 id="manager-title" className="panel-subhead">
        Waiting for you
      </h2>
      <p className="world-note">
        You are the manager. The agent can file a review but never decide one.
      </p>
      {message && (
        <p className="notice" role="status" data-testid="operator-result">
          {message}
        </p>
      )}
      {desk.pending.length === 0 ? (
        <p className="empty">No review is waiting. Late checkouts after 14:00 land here.</p>
      ) : (
        <ol className="request-list">
          {desk.pending.map((item) => (
            <ReviewCard
              key={item.approval_id}
              item={item}
              busy={desk.busy}
              onDecide={(i, d) => void decide(i, d)}
            />
          ))}
        </ol>
      )}
      {decided.length > 0 && (
        <>
          <h3 className="panel-subhead">Decided</h3>
          <DecidedList items={decided} />
        </>
      )}

      <h3 className="panel-subhead">Simulated staff tasks</h3>
      <p className="world-note">
        Moving a task is a simulator action; no real staff are contacted. The guest sees the new
        status in their requests.
      </p>
      {desk.tasks.length === 0 ? (
        <p className="empty">No open tasks.</p>
      ) : (
        <ol className="request-list">
          {desk.tasks.map((task) => (
            <li key={task.task_id} className="desk-item" data-testid="operator-task" data-id={task.task_id}>
              <div className="desk-item-head">
                <span>
                  <strong>{task.guest_name}</strong> · room {task.room_number}
                </span>
                <span className={`badge badge-${task.status}`}>
                  {TASK_BADGE[task.status] ?? task.status.replace("_", " ")}
                </span>
              </div>
              <div className="desk-item-what">
                {taskSummary(task)}
                {task.details.notes ? ` · “${String(task.details.notes)}”` : ""}
              </div>
              {task.details.guest_description ? (
                <div className="guest-text">Guest: “{String(task.details.guest_description)}”</div>
              ) : null}
              {task.actions.length > 0 && (
                <div className="step-actions">
                  {task.actions.map((action) => (
                    <button
                      key={action}
                      type="button"
                      className={action === "cancel" ? "btn" : "btn primary"}
                      disabled={desk.busy}
                      aria-label={`${ACTION_LABELS[action]}: ${taskSummary(task)}, room ${task.room_number}`}
                      onClick={() => void move(task, action)}
                    >
                      {ACTION_LABELS[action]}
                    </button>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
