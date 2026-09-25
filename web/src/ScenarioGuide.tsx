import { useCallback, useEffect, useRef, useState } from "react";
import { TERMINAL, type OperatorApproval, type RunView } from "./api";
import { NOT_TOLD } from "./HotelBoard";
import {
  SCENARIOS,
  changeNarrative,
  clockPlan,
  decisionNarrative,
  expectedText,
  hhmm,
  hotelNarrative,
  matches,
  sayNarrative,
  sayResult,
  unchangedNarrative,
  type Scenario,
  type Step,
} from "./scenarios";
import type { Turn, useDemoSession } from "./useDemoSession";
import type { HotelDesk } from "./useHotelDesk";
import { clock } from "./WorldPanel";
import "./scenarios.css";

// The guided tour's story column. The tour has its own layout:
// this column, the guest's chat, and the story's room with its reviews, read only. The hotel
// changes only through these steps; each step is ticked when the server's own result shows it
// happened.

type Demo = ReturnType<typeof useDemoSession>;
type Tone = "ok" | "blocked" | "info";

interface Entry {
  at: string | null;
  text: string;
  tone: Tone;
  /** False for something that happened outside the script (for example, an extra decision). */
  step: boolean;
  /** The hotel's turn a hotel step was ticked from, so a later step never counts it again. */
  runId?: string;
}

function firstName(name: string): string {
  return name.split(" ")[0] ?? name;
}

/** Tool errors behind a checkout step, so a mismatch can say what the server refused. */
function toolErrors(run: RunView | null): string[] {
  return (run?.activity ?? [])
    .filter((a) => a.error_code && a.tool_name?.includes("late_checkout"))
    .map((a) => a.error_code as string);
}

/** A decided review, read back from the queue, in the shape the narratives take. */
function asDecision(a: OperatorApproval) {
  return {
    approval: a,
    decision: (a.human_decision === "reject" ? "reject" : "approve") as "approve" | "reject",
    outcome: a.status,
    applied: a.status === "executed",
    receipt_id: null,
    replayed: false,
  };
}

export function ScenarioPicker(props: {
  busy: boolean;
  onStart: (scenario: Scenario) => void;
}) {
  return (
    <section className="panel picker" aria-labelledby="picker-title" data-testid="scenario-picker">
      <div className="guide-head">
        <div>
          <h2 id="picker-title">Guided tour</h2>
          <p className="hint">
            Short stories that show what a chat demo usually hides: what happens when the hotel
            changes while a guest is waiting. The guest&apos;s lines go to the real agent, and you
            are the manager. Each one starts from a clean hotel, so the conversations and changes
            made so far are cleared. During a story the manager&apos;s desk, the staff, the
            hotel&apos;s changes and the clock move only through its steps; what you type as the
            guest still goes to the agent.
          </p>
        </div>
      </div>
      <ol className="scenario-grid">
        {SCENARIOS.map((s, i) => (
          <li key={s.id} className="scenario-card" data-testid={`scenario-${s.id}`}>
            <span className="scenario-number">Scenario {i + 1}</span>
            <h3>{s.title}</h3>
            <p className="hint">{s.story}</p>
            <span className="scenario-guest">
              {s.guestName} · room {s.room} · {s.steps.length} steps
            </span>
            <button
              type="button"
              className="btn primary"
              disabled={props.busy}
              aria-label={`Start: ${s.title}`}
              onClick={() => props.onStart(s)}
            >
              Start
            </button>
          </li>
        ))}
      </ol>
    </section>
  );
}

function StepAction(props: {
  step: Step;
  guest: string;
  busy: boolean;
  chatBusy: boolean;
  /** The story's conversation is open; a guest line has nowhere to go without it. */
  canSay: boolean;
  pendingReview: OperatorApproval | undefined;
  onSay: (message: string) => void;
  onEvent: (event: string) => void;
  onClock: (to: string) => void;
  onDecide: (item: OperatorApproval, decision: "approve" | "reject") => void;
}) {
  const { step, guest, busy, chatBusy, pendingReview } = props;
  switch (step.kind) {
    case "say":
      return (
        <button
          type="button"
          className="btn primary"
          disabled={busy || chatBusy || !props.canSay}
          onClick={() => props.onSay(step.message)}
          data-testid="step-action"
        >
          {chatBusy ? "The agent is working…" : `Send as ${guest}`}
        </button>
      );
    case "event":
      return (
        <button
          type="button"
          className="btn primary"
          disabled={busy}
          onClick={() => props.onEvent(step.event)}
          data-testid="step-action"
        >
          Make it happen
        </button>
      );
    case "clock":
      return (
        <button
          type="button"
          className="btn primary"
          disabled={busy}
          onClick={() => props.onClock(step.to)}
          data-testid="step-action"
        >
          Move the hotel clock to {step.to}
        </button>
      );
    case "decide": {
      const decision = step.decision ?? "approve";
      return (
        <>
          <button
            type="button"
            className="btn primary"
            disabled={busy || !pendingReview}
            onClick={() => pendingReview && props.onDecide(pendingReview, decision)}
            data-testid="step-action"
          >
            {decision === "reject" ? "Reject" : "Approve"} {guest}&apos;s review
          </button>
          {!pendingReview && <span className="hint">No review is waiting yet.</span>}
        </>
      );
    }
    case "hotel":
      // Nothing to press: the step before it made the hotel start this turn.
      return (
        <span className="hint" data-testid="step-waiting">
          {chatBusy
            ? `The agent is writing to ${guest}…`
            : `Waiting for the hotel's message to ${guest}…`}
        </span>
      );
  }
}

export default function ScenarioGuide(props: {
  /** A story is (re)starting: the hotel is being reset, so no second start may begin. */
  starting?: boolean;
  scenario: Scenario;
  demo: Demo;
  desk: HotelDesk;
  onGuestChanged: () => void;
  onRestart: (scenario: Scenario) => void;
  onExit: () => void;
}) {
  const { scenario, demo, desk, onGuestChanged } = props;
  const [entries, setEntries] = useState<Entry[]>([]);
  const [note, setNote] = useState<string | null>(null);
  const [mark, setMark] = useState(0);
  const [busy, setBusy] = useState(false);
  const seen = useRef<Map<string, string>>(new Map());

  const done = entries.filter((e) => e.step).length;
  const current = scenario.steps[done];
  const finished = done >= scenario.steps.length;
  const guest = firstName(scenario.guestName);
  const index = SCENARIOS.findIndex((s) => s.id === scenario.id);
  const next = SCENARIOS[(index + 1) % SCENARIOS.length];
  const reviews = desk.approvals.filter((a) => a.room_number === scenario.room);
  const pendingReview = reviews.find((a) => a.status === "pending");

  const record = useCallback(
    (text: string, tone: Tone, step: boolean, at: string | null = null, runId?: string) => {
      setEntries((prev) => [
        ...prev,
        { at: at ?? hhmm(desk.world?.demo_time), text, tone, step, runId },
      ]);
      if (step) {
        setNote(null);
        setMark(demo.turns.length);
      }
    },
    [demo.turns.length, desk.world?.demo_time],
  );

  // A guest step is done when a finished turn's recorded tool outcome matches it. A turn the
  // hotel started to tell the guest something is not the guest's step.
  const guestTurns = (turns: Turn[]) => turns.filter((t) => t.run?.initiated_by !== "hotel");
  const finishedTurns = guestTurns(demo.turns).filter(
    (t) => t.phase === "rejected" || (t.phase === "done" && t.run && TERMINAL.has(t.run.status)),
  ).length;
  useEffect(() => {
    if (current?.kind !== "say") return;
    const turns = guestTurns(demo.turns.slice(mark)).filter(
      (t) => t.phase === "rejected" || (t.phase === "done" && t.run),
    );
    if (turns.length === 0) return;
    const results = turns.map((t) => sayResult(t.run));
    const hit = results.find((r) => matches(current.expect, r));
    if (hit) {
      const text =
        current.expect.outcome === "unchanged"
          ? unchangedNarrative(guest, current.expect)
          : sayNarrative(guest, hit);
      record(text, hit.outcome === "denied" ? "blocked" : "ok", true);
      return;
    }
    const last = turns[turns.length - 1];
    const refused = toolErrors(last.run);
    const unfinished = last.run && last.run.status !== "completed";
    const what = last.error
      ? `the message was not accepted: ${last.error.message}`
      : unfinished
        ? `the agent's turn did not finish (${last.run?.error?.code ?? last.run?.status}).`
        : refused.length
          ? `the server refused the request (${refused.join(", ")})`
          : sayNarrative(guest, results[results.length - 1]);
    setNote(
      `This step expects ${expectedText(current.expect)}. Instead: ${what} Read the agent's ` +
        "reply; you can answer it in the chat, send the line again, or restart the scenario.",
    );
    // Re-evaluate only when a turn finishes or the step changes.
  }, [finishedTurns, mark, done]);

  // A review of this room leaving "pending" is reported however it happened: the step's
  // button, the Manager tab's own buttons, or the hotel clock overtaking it.
  useEffect(() => {
    for (const a of reviews) {
      const before = seen.current.get(a.approval_id);
      seen.current.set(a.approval_id, a.status);
      if (before !== "pending" || a.status === "pending") continue;
      const lapsed = a.status === "expired" && !a.human_decision;
      const text = lapsed
        ? `${guest}'s review for ${hhmm(a.requested_checkout_local) ?? "that time"} expired: ` +
          "the time it asked for has arrived on the hotel clock with no decision. Nothing was changed."
        : decisionNarrative(asDecision(a), a.world_changes ?? []);
      const tone: Tone = a.status === "executed" ? "ok" : "blocked";
      const isStep = current?.kind === "decide" && a.status === current.expect;
      record(text, tone, isStep);
      if (current?.kind === "decide" && !isStep && !lapsed) {
        setNote(`This step expected the review to be ${current.expect}. ${text}`);
      }
    }
  }, [desk.approvals]);

  // A hotel step is done when a turn the hotel started carried its update and
  // finished. It is matched by the update, not by position: the hotel's turn can reach the
  // screen before the step that caused it is ticked. A turn is counted for one step only.
  const hotelTurns = demo.turns.filter((t) => t.run?.initiated_by === "hotel");
  const finishedHotelTurns = hotelTurns.filter((t) => t.phase === "done").length;
  const storyRoom = desk.world?.rooms.find((r) => r.room_number === scenario.room);
  const skipped =
    current?.kind === "hotel" &&
    storyRoom?.latest_update?.kind === current.update &&
    storyRoom.latest_update.status === "skipped"
      ? (storyRoom.latest_update.skip_reason ?? "")
      : null;
  useEffect(() => {
    if (current?.kind !== "hotel") return;
    const counted = new Set(entries.map((e) => e.runId).filter(Boolean));
    const turn = hotelTurns.find(
      (t) =>
        t.run &&
        !counted.has(t.run.run_id) &&
        (t.run.hotel_updates ?? []).some((u) => u.kind === current.update),
    );
    if (!turn?.run || turn.phase !== "done") {
      if (skipped !== null) {
        setNote(
          `The hotel did not send this update to ${guest}: ${NOT_TOLD[skipped] ?? "not delivered"}. ` +
            "Restart the scenario to try again.",
        );
      }
      return;
    }
    const update = turn.run.hotel_updates?.find((u) => u.kind === current.update);
    if (turn.run.status === "completed") {
      record(hotelNarrative(guest, turn.run), "ok", true, hhmm(update?.demo_time), turn.run.run_id);
    } else {
      setNote(
        `The agent's message about "${update?.title ?? "the hotel's update"}" did not finish ` +
          `(${turn.run.error?.code ?? turn.run.status}). A hotel update is not retried; restart ` +
          "the scenario to try again.",
      );
    }
    // Re-evaluate when a hotel turn arrives or finishes, when the step changes, or when the
    // board says the update was not sent.
  }, [hotelTurns.length, finishedHotelTurns, done, skipped]);

  const act = async (run: () => Promise<void>) => {
    setBusy(true);
    try {
      await run();
    } catch (err) {
      setNote(err instanceof Error ? err.message : "That did not work.");
    } finally {
      setBusy(false);
    }
  };

  const onEvent = (event: string) =>
    act(async () => {
      const result = await desk.simulate(event);
      record(changeNarrative(result), "info", true, hhmm(result.demo_time));
      onGuestChanged();
    });

  const onClock = (to: string) =>
    act(async () => {
      const from = clock(desk.world?.demo_time);
      let after = from;
      for (const minutes of clockPlan(from, to, desk.world?.clock_steps)) {
        after = (await desk.advanceClock(minutes)).after;
      }
      record(`The hotel clock moved from ${from} to ${after}.`, "info", true, after);
      onGuestChanged();
    });

  const onDecide = (item: OperatorApproval, decision: "approve" | "reject") =>
    act(async () => {
      await desk.decide(item, decision); // reported by the review watcher above
      onGuestChanged();
    });

  return (
    <section className="panel guide" aria-labelledby="guide-title" data-testid="scenario-guide">
      <div className="guide-head">
        <div>
          <span className="scenario-number">
            Story {index + 1} of {SCENARIOS.length} · {scenario.guestName}, room {scenario.room}
          </span>
          <h2 id="guide-title">{scenario.title}</h2>
          <p className="hint">{scenario.story}</p>
        </div>
        <div className="guide-head-actions">
          <button
            type="button"
            className="btn"
            disabled={busy || demo.active || props.starting}
            onClick={() => props.onRestart(scenario)}
          >
            Restart
          </button>
          <button type="button" className="btn" disabled={props.starting} onClick={props.onExit}>
            Exit tour
          </button>
        </div>
      </div>

      <div className="guide-body">
        <ol className="tour-steps scenario-steps">
          {scenario.steps.map((step, i) => {
            const state = i < done ? "done" : i === done ? "current" : "todo";
            return (
              <li
                key={i}
                className={`tour-step ${state}`}
                aria-current={state === "current" ? "step" : undefined}
                data-testid="scenario-step"
                data-state={state}
              >
                <span className="tour-mark" aria-hidden="true">
                  {state === "done" ? "✓" : i + 1}
                </span>
                <div className="step-body">
                  <span>{step.title}</span>
                  {step.kind === "say" && state !== "done" && (
                    <q className="step-quote">{step.message}</q>
                  )}
                  {state === "current" && (
                    <div className="step-actions">
                      <StepAction
                        step={step}
                        guest={guest}
                        busy={busy || desk.busy || demo.switching}
                        chatBusy={demo.active}
                        canSay={Boolean(demo.token)}
                        pendingReview={pendingReview}
                        onSay={(m) =>
                          void demo.send(m).then((result) => {
                            if (result === "busy") {
                              setNote(
                                "The hotel is telling the guest something first. Send the line " +
                                  "again when that reply has finished.",
                              );
                            }
                          })
                        }
                        onEvent={(e) => void onEvent(e)}
                        onClock={(t) => void onClock(t)}
                        onDecide={(item, decision) => void onDecide(item, decision)}
                      />
                    </div>
                  )}
                </div>
              </li>
            );
          })}
        </ol>

        <div className="guide-timeline">
          <h3 className="timeline-title">What happened</h3>
          {entries.length === 0 ? (
            <p className="hint">Nothing yet. Each result appears here in plain words.</p>
          ) : (
            <ol className="timeline" data-testid="scenario-timeline">
              {entries.map((e, i) => (
                <li key={i} className={`timeline-${e.tone}`}>
                  {e.at && <span className="at">{e.at}</span>}
                  {e.text}
                </li>
              ))}
            </ol>
          )}
          {note && (
            <p className="notice" role="status" data-testid="scenario-note">
              {note}
            </p>
          )}
        </div>
      </div>

      {finished ? (
        <div className="scenario-done" role="status" data-testid="scenario-done">
          <div className="why" data-testid="scenario-why">
            <div className="why-box naive">
              <h3>Without these checks</h3>
              <p>{scenario.naive}</p>
            </div>
            <div className="why-box here">
              <h3>In this system</h3>
              <p>{scenario.here}</p>
            </div>
          </div>
          <div className="step-actions">
            {next.id !== scenario.id && (
              <button
                type="button"
                className="btn primary"
                disabled={props.starting}
                onClick={() => props.onRestart(next)}
              >
                Next: {next.title}
              </button>
            )}
            <button type="button" className="btn" disabled={props.starting} onClick={props.onExit}>
              Keep exploring from here
            </button>
          </div>
        </div>
      ) : (
        <details className="why-details">
          <summary>Why this matters</summary>
          <div className="why" data-testid="scenario-why">
            <div className="why-box naive">
              <h3>Without these checks</h3>
              <p>{scenario.naive}</p>
            </div>
            <div className="why-box here">
              <h3>In this system</h3>
              <p>{scenario.here}</p>
            </div>
          </div>
        </details>
      )}
    </section>
  );
}
