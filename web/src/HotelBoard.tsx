import { useEffect, useRef, useState, type ReactNode } from "react";
import type {
  BoardReview,
  BoardRoom,
  BoardTask,
  ClockSideEffects,
  GuestSummary,
  WorldEventItem,
  WorldResult,
  WorldState,
} from "./api";
import type { HotelDesk } from "./useHotelDesk";
import { ActivityLog, blockingText, clock, REASON_TEXT } from "./WorldPanel";
import "./world.css";

// The hotel board: the fictional hotel as the checkout rules see it right now. Every value is
// read from the server (the same facts and the same pure rule a request would be decided on),
// and every simulated change sits next to the fact it changes, so its effect shows at once.
// None of these controls is a model tool.

/** Why no later checkout can be had now, in the hotel's words. */
const BLOCKED_TEXT: Record<string, string> = {
  ROOM_OUT_OF_SERVICE: "the room is out of service",
  NO_LATER_TIME: "nothing later fits today",
  TIME_PASSED: "that time has passed on the hotel clock",
  NOT_DEPARTING_TODAY: "not departing today",
};

function latestValue(room: BoardRoom): string {
  if (room.status !== "checked_in") return "—";
  return room.latest_checkout ? clock(room.latest_checkout) : "None";
}

function latestNote(room: BoardRoom): string {
  if (room.status !== "checked_in") return "after check-in";
  // One review waits per stay: a new time can be asked for once this one is decided.
  if (room.pending_review) return "once the waiting review is decided";
  if (room.latest_checkout) return room.latest_needs_review ? "with a manager" : "automatic";
  const code = room.latest_blocked_by ?? "";
  return BLOCKED_TEXT[code] ?? REASON_TEXT[code] ?? code;
}

function serviceValue(room: BoardRoom): string {
  return room.in_service ? "In service" : "Out of service";
}

const CLEANING_TEXT: Record<string, string> = {
  dirty: "Needs cleaning",
  clean: "Clean",
  inspected: "Clean and inspected",
};

function cleaningValue(room: BoardRoom): string {
  return CLEANING_TEXT[room.housekeeping_state] ?? room.housekeeping_state;
}

/**
 * The cleaning state matters only where a rule reads it: an arriving guest's early check-in
 * needs a ready room. A room someone is staying in is not re-inspected in this demo, so
 * showing its state would show a value nothing ever changes.
 */
function showsCleaning(room: BoardRoom): boolean {
  return room.status !== "checked_in";
}

const WHAT_TEXT: Record<string, string> = {
  room_cleaning: "Room cleaning",
  hvac: "Air conditioning or heating",
  plumbing: "Plumbing",
  electrical: "Electrical",
  other: "Other issue",
};

/** One open request in a person's words: what it is and where it stands. */
export function taskText(task: BoardTask): string {
  const what = WHAT_TEXT[task.what] ?? task.what;
  if (task.status === "in_progress") return `${what}: staff working on it`;
  return task.kind === "maintenance" ? `${what}: reported` : `${what}: waiting for staff`;
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** What the clock move itself changed, in a person's words; empty when nothing did. */
export function sideEffectText(effects: ClockSideEffects | undefined): string {
  if (!effects) return "";
  const parts: string[] = [];
  if (effects.expired_reviews.length) {
    const n = effects.expired_reviews.length;
    parts.push(`${plural(n, "waiting review", "waiting reviews")} expired: the requested time came.`);
  }
  if (effects.cancelled_tasks.length) {
    const n = effects.cancelled_tasks.length;
    parts.push(`Cleaning hours ended: ${plural(n, "room cleaning", "room cleanings")} nobody started cancelled.`);
  }
  return parts.join(" ");
}

export const NOT_TOLD: Record<string, string> = {
  NO_CONVERSATION: "no open conversation",
  NO_PROVIDER: "no language model configured",
  SERVER_RESTARTED: "the server restarted",
};

/** What the hotel last had to tell a guest, and whether the agent did. */
export function updateText(room: BoardRoom): string | null {
  const update = room.latest_update;
  if (!update) return null;
  const who = room.guest_name.split(" ")[0] ?? room.guest_name;
  const at = update.demo_time ? ` (${clock(update.demo_time)})` : "";
  if (update.status === "sent") return `Agent told ${who}: ${update.title}${at}`;
  if (update.status === "pending") return `Agent will tell ${who} when their turn ends: ${update.title}`;
  const why = NOT_TOLD[update.skip_reason ?? ""] ?? "not delivered";
  return `Not told (${why}): ${update.title}${at}`;
}

function reviewText(review: BoardReview): string {
  const until = review.requested_checkout ? clock(review.requested_checkout) : "a later time";
  return `Late checkout until ${until}: waiting for a manager`;
}

/** Every value the board shows, keyed by where it shows, so a change can be marked. */
export function boardValues(world: WorldState | null): Record<string, string> {
  const values: Record<string, string> = {};
  for (const room of world?.rooms ?? []) {
    const n = room.room_number;
    values[`${n}:checkout`] = clock(room.checkout);
    values[`${n}:check_in`] = clock(room.check_in);
    values[`${n}:next_arrival`] = room.next_arrival ? clock(room.next_arrival) : "None";
    values[`${n}:in_service`] = serviceValue(room);
    if (showsCleaning(room)) values[`${n}:cleaning`] = cleaningValue(room);
    values[`${n}:latest`] = latestValue(room);
  }
  return values;
}

/**
 * The board's last change: each value that moved, with what it was. It stays marked until the
 * next change, so a viewer can see cause and effect side by side. A reset clears it: a new
 * `resetKey` (a start over from this screen) or a hotel read that went back in time.
 */
export function useBoardChanges(world: WorldState | null, resetKey = 0): Map<string, string> {
  const last = useRef<{ time: string; applied: number; values: Record<string, string> } | null>(
    null,
  );
  const [changes, setChanges] = useState<Map<string, string>>(new Map());
  const seenReset = useRef(resetKey);
  useEffect(() => {
    if (!world) return;
    if (resetKey !== seenReset.current) {
      // The hotel was started over: the first read of the new one is the new baseline.
      seenReset.current = resetKey;
      last.current = null;
    }
    const values = boardValues(world);
    const applied = world.events.filter((e) => e.applied).length;
    const before = last.current;
    last.current = { time: world.demo_time, applied, values };
    if (!before || world.demo_time < before.time || applied < before.applied) {
      setChanges(new Map()); // first read, or a start over
      return;
    }
    const moved = Object.entries(before.values).filter(([k, v]) => k in values && values[k] !== v);
    if (moved.length > 0) setChanges(new Map(moved));
  }, [world, resetKey]);
  return changes;
}

/** A board value; when it just changed, the old value is struck through beside the new one. */
function Value({ at, value, changes }: { at: string; value: string; changes: Map<string, string> }) {
  const was = changes.get(at);
  if (was === undefined) return <span className="board-value">{value}</span>;
  return (
    <span className="board-value board-changed" data-testid={`changed-${at}`}>
      <s aria-label={`was ${was}`}>{was}</s> {value}
    </span>
  );
}

function EventAction(props: {
  event: WorldEventItem | undefined;
  busy: boolean;
  onSimulate: (event: WorldEventItem) => void;
}) {
  const { event } = props;
  if (!event || event.applied) return null;
  if (!event.available) {
    return (
      <span className="board-action board-action-off">
        {event.action}: not possible now
        {event.unavailable_reason ? ` (${event.unavailable_reason.replace(/\.$/, "")})` : ""}
      </span>
    );
  }
  return (
    <button
      type="button"
      className="board-action"
      disabled={props.busy}
      title={event.effect}
      onClick={() => props.onSimulate(event)}
      data-testid={`world-event-${event.key}`}
    >
      <BoltIcon />
      {event.action}
    </button>
  );
}

/** Marks a simulated hotel change, so it never reads as something the agent did. */
function BoltIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false">
      <path d="M13 2 4 14h7l-1 8 9-12h-7z" />
    </svg>
  );
}

function stepLabel(minutes: number): string {
  return minutes % 60 === 0 ? `+${minutes / 60} h` : `+${minutes} min`;
}

function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");
}

function asGuest(room: BoardRoom): GuestSummary {
  return {
    guest_id: room.guest_id,
    display_name: room.guest_name,
    room_number: room.room_number,
    reservation_reference: "",
    display_status: "",
  };
}

/** One room's facts: the cells after the guest's name. */
function RoomCells(props: {
  room: BoardRoom;
  changes: Map<string, string>;
  eventFor: (room: string | null, fact: WorldEventItem["fact"]) => WorldEventItem | undefined;
  busy: boolean;
  onSimulate: (event: WorldEventItem) => void;
}) {
  const { room, changes, eventFor, busy, onSimulate } = props;
  const n = room.room_number;
  const review = room.pending_review;
  return (
    <>
      <td data-label="Stay">
        {room.status === "checked_in" ? (
          <>
            <span className="board-value">
              Checkout <Value at={`${n}:checkout`} value={clock(room.checkout)} changes={changes} />
            </span>
            <span className="board-sub">
              Next arrival{" "}
              <Value
                at={`${n}:next_arrival`}
                value={room.next_arrival ? clock(room.next_arrival) : "None"}
                changes={changes}
              />
            </span>
          </>
        ) : (
          <span className="board-value">
            Arrives <Value at={`${n}:check_in`} value={clock(room.check_in)} changes={changes} />
          </span>
        )}
        <EventAction event={eventFor(n, "next_arrival")} busy={busy} onSimulate={onSimulate} />
      </td>
      <td data-label="Room">
        <Value at={`${n}:in_service`} value={serviceValue(room)} changes={changes} />
        {showsCleaning(room) && (
          <span className="board-sub">
            <Value at={`${n}:cleaning`} value={cleaningValue(room)} changes={changes} />
          </span>
        )}
        <EventAction event={eventFor(n, "in_service")} busy={busy} onSimulate={onSimulate} />
      </td>
      <td data-label="Open requests" data-testid={`board-requests-${n}`}>
        {room.open_tasks.length === 0 && !review ? (
          <span className="board-note">None</span>
        ) : (
          <ul className="board-requests">
            {review && (
              <li className="board-request board-request-review">
                {reviewText(review)}
                {review.blocking_reason && (
                  <span className="board-warn">
                    {" "}
                    · would not apply now: {blockingText(review.blocking_reason)}
                  </span>
                )}
              </li>
            )}
            {room.open_tasks.map((task) => (
              <li key={task.task_id} className="board-request">
                {taskText(task)}
              </li>
            ))}
          </ul>
        )}
        {room.latest_update && (
          <p
            className={`board-update board-update-${room.latest_update.status}`}
            data-testid={`board-update-${n}`}
          >
            {updateText(room)}
          </p>
        )}
      </td>
      <td data-label="If they asked now" className="board-result">
        <Value at={`${n}:latest`} value={latestValue(room)} changes={changes} />
        <span className="board-note">{latestNote(room)}</span>
      </td>
    </>
  );
}

/**
 * Whether the board can be drawn: still reading, read, or read from an API that does not send
 * the board at all (a server started before the board existed; Vite reloads the UI by itself,
 * the API does not), which must not look like an endless load.
 */
export function boardState(world: WorldState | null): "reading" | "ready" | "outdated" {
  if (!world) return "reading";
  return Array.isArray(world.rooms) ? "ready" : "outdated";
}

function OutdatedApi() {
  return (
    <p className="notice" role="alert" data-testid="board-outdated">
      The API did not send the hotel board. It is probably running code from before the board
      existed: restart the API, then reload this page.
    </p>
  );
}

/**
 * Explore mode's board: one row per demo guest's room. Choosing a row opens that guest's
 * conversation; the buttons in a cell simulate a change to that fact.
 */
export default function HotelBoard(props: {
  desk: HotelDesk;
  changes: Map<string, string>;
  guests: GuestSummary[] | null;
  selected: string | null;
  selectDisabled: boolean;
  loadError: string | null;
  onSelect: (guestId: string) => void;
  onChanged: () => void;
  /** Opens the Hotel policy tab, where every topic the agent can read is listed. */
  onShowPolicy?: () => void;
}) {
  const { desk, changes } = props;
  const [message, setMessage] = useState<string | null>(null);
  const world = desk.world;
  const events = world?.events ?? [];
  const rooms = world?.rooms ?? [];
  const eventFor = (room: string | null, fact: WorldEventItem["fact"]) =>
    events.find((e) => e.room === room && e.fact === fact);
  // Rows follow the guest list, so a guest can be chosen even before the board is read.
  const rows = (props.guests ?? rooms.map(asGuest))
    .map((guest) => ({ guest, room: rooms.find((r) => r.guest_id === guest.guest_id) }))
    .sort((a, b) => a.guest.room_number.localeCompare(b.guest.room_number));

  const act = async (run: () => Promise<WorldResult>, effect?: string) => {
    try {
      const result = await run();
      const then = [effect, sideEffectText(result.side_effects)].filter(Boolean).join(" ");
      setMessage(`Simulated: ${result.title}.${then ? ` ${then}` : ""}`);
      props.onChanged();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "The hotel change failed.");
    }
  };
  const simulate = (event: WorldEventItem) => void act(() => desk.simulate(event.key), event.effect);

  return (
    <section className="panel board" aria-labelledby="board-title" data-testid="hotel-board">
      <div className="board-head">
        <div>
          <h2 id="board-title">The hotel now</h2>
          <p className="world-note">Pick a guest to talk as them.</p>
        </div>
        <div className="board-clock" data-testid="board-clock">
          <span className="world-clock-label">
            Hotel clock{world ? ` ${clock(world.demo_time)}` : ""}
          </span>
          <span className="world-clock-actions">
            {(world?.clock_steps ?? []).map((minutes) => (
              <button
                key={minutes}
                type="button"
                className="board-step"
                disabled={desk.busy}
                onClick={() => void act(() => desk.advanceClock(minutes))}
              >
                {stepLabel(minutes)}
              </button>
            ))}
            {world && world.clock_steps.length === 0 && (
              <span className="board-note">The demo day ends at {clock(world.clock_limit)}.</span>
            )}
          </span>
        </div>
      </div>
      {props.loadError && (
        <p role="alert" className="error">
          {props.loadError}
        </p>
      )}
      {boardState(world) === "outdated" && <OutdatedApi />}
      <table className="board-table">
        <colgroup>
          <col className="board-col-guest" />
          <col className="board-col-stay" />
          <col className="board-col-room" />
          <col />
          <col className="board-col-result" />
        </colgroup>
        <thead>
          <tr>
            <th scope="col">Guest</th>
            <th scope="col">Stay</th>
            <th scope="col">Room</th>
            <th scope="col">Open requests</th>
            <th
              scope="col"
              className="board-result"
              title="The latest checkout the rules would grant if this guest asked right now"
            >
              If they asked now
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ guest, room }) => {
            const n = room?.room_number ?? guest.room_number;
            const chosen = props.selected === guest.guest_id;
            return (
              <tr key={guest.guest_id} className={chosen ? "selected" : undefined} data-testid={`board-room-${n}`}>
                <th scope="row">
                  <button
                    type="button"
                    className="board-guest"
                    aria-pressed={chosen}
                    disabled={props.selectDisabled}
                    onClick={() => props.onSelect(guest.guest_id)}
                    data-testid={`guest-${guest.guest_id}`}
                  >
                    <span className="avatar avatar-sm" aria-hidden="true">
                      {initials(guest.display_name)}
                    </span>
                    <span className="guest-text-block">
                      <span className="guest-name">{guest.display_name}</span>
                      <span className="guest-meta">
                        Room {n}
                        {guest.reservation_reference ? ` · ${guest.reservation_reference}` : ""}
                        {guest.display_status ? ` · ${guest.display_status}` : ""}
                      </span>
                    </span>
                  </button>
                </th>
                {room ? (
                  <RoomCells room={room} changes={changes} eventFor={eventFor} busy={desk.busy} onSimulate={simulate} />
                ) : (
                  <td colSpan={4} className="board-note">
                    {boardState(world) === "reading" ? "Reading the hotel…" : "—"}
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="board-foot">
        {world?.policy && (
          <div className="board-policy" data-testid="board-policy">
            Checkout policy: automatic until {world.policy.automatic_until}, with a manager until{" "}
            {world.policy.manager_until}
            {props.onShowPolicy && (
              <button
                type="button"
                className="link-button"
                onClick={props.onShowPolicy}
                data-testid="show-policy"
              >
                Full hotel policy
              </button>
            )}
          </div>
        )}
        <span className="board-legend">
          <BoltIcon /> simulates a change to the hotel; the agent has no tool for it
        </span>
      </div>
      {message && (
        <p className="notice" role="status" data-testid="world-result">
          {message}
        </p>
      )}
      <ActivityLog />
    </section>
  );
}

function Fact({ label, testId, children }: { label: string; testId?: string; children: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd data-testid={testId}>{children}</dd>
    </div>
  );
}

/**
 * The guided tour's view of the story's room: the same values as the board, read only. In a
 * tour the hotel changes only through the story's own steps.
 */
export function RoomCard({
  world,
  room: number,
  changes,
}: {
  world: WorldState | null;
  room: string;
  changes: Map<string, string>;
}) {
  const room = (world?.rooms ?? []).find((r) => r.room_number === number);
  if (!room) {
    const state = boardState(world);
    if (state === "outdated") return <OutdatedApi />;
    return (
      <p className="panel empty">
        {state === "reading" ? `Reading room ${number}…` : `Room ${number} is not in the hotel's records.`}
      </p>
    );
  }
  const n = room.room_number;
  return (
    <section className="panel room-card" aria-labelledby="room-card-title" data-testid="room-card">
      <h2 id="room-card-title" className="panel-subhead">
        Room {n} now · {room.guest_name}
      </h2>
      <dl className="room-facts">
        <Fact label="Checkout" testId="room-checkout">
          <Value at={`${n}:checkout`} value={clock(room.checkout)} changes={changes} />
        </Fact>
        <Fact label="Next arrival">
          <Value
            at={`${n}:next_arrival`}
            value={room.next_arrival ? clock(room.next_arrival) : "None"}
            changes={changes}
          />
        </Fact>
        <Fact label="Room">
          <Value at={`${n}:in_service`} value={serviceValue(room)} changes={changes} />
          {showsCleaning(room) && (
            <>
              {" · "}
              <Value at={`${n}:cleaning`} value={cleaningValue(room)} changes={changes} />
            </>
          )}
        </Fact>
        {world?.policy && (
          <Fact label="Checkout policy">
            Automatic until {world.policy.automatic_until}, manager until{" "}
            {world.policy.manager_until}
          </Fact>
        )}
        <div className="room-facts-wide">
          <dt>Latest checkout if {room.guest_name.split(" ")[0]} asked now</dt>
          <dd>
            <Value at={`${n}:latest`} value={latestValue(room)} changes={changes} />{" "}
            <span className="board-note">{latestNote(room)}</span>
          </dd>
        </div>
      </dl>
      <p className="board-lock">
        In a guided tour the manager&apos;s desk, the staff, the hotel&apos;s changes and the
        clock move only through the story&apos;s steps. What you type as the guest still goes to
        the agent and can change the stay, as a guest&apos;s words would.
      </p>
    </section>
  );
}
