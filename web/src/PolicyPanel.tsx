import { useEffect, useState } from "react";
import { api, type Handbook, type HandbookTopic } from "./api";

// The Hotel policy tab: every topic exactly as the agent's `get_hotel_policy` tool
// returns it, so a viewer can check an answer against its source. A topic the agent read in
// its latest reply is marked. Nothing here is a model tool and nothing here changes the hotel.

const TITLES: Record<string, string> = {
  check_in: "Check-in",
  checkout: "Checkout",
  luggage: "Luggage",
  housekeeping: "Housekeeping",
  maintenance: "Maintenance",
  room_amenities: "In the room",
  breakfast: "Breakfast",
  dining: "Restaurant, bar and room service",
  facilities: "Pool, gym and spa",
  internet: "Internet",
  parking: "Parking",
  house_rules: "House rules",
};
/** Stay first, then services in the room, food, and the rest of the hotel. */
const ORDER = Object.keys(TITLES);

type Data = Record<string, unknown>;

function text(value: unknown): string {
  return value === null || value === undefined ? "" : String(value);
}

function list(value: unknown): string {
  return Array.isArray(value) ? value.map((v) => text(v).replace(/_/g, " ")).join(", ") : "";
}

interface Venue {
  name: string;
  opens_local: string;
  closes_local: string;
  notes?: string | null;
}

function venues(data: Data): string[] {
  const items = Array.isArray(data.venues) ? (data.venues as Venue[]) : [];
  return items.map((v) => `${v.name}: ${v.opens_local}–${v.closes_local}${v.notes ? `. ${v.notes}` : ""}`);
}

/** The lines a person reads for one topic; every value comes from the stored policy. */
export function topicLines(topic: string, d: Data): string[] {
  switch (topic) {
    case "check_in":
      return [
        `Standard check-in ${text(d.standard_check_in_local)}.`,
        `Early check-in from ${text(d.early_check_in_from_local)} when the room is ready; no manager review.`,
      ];
    case "checkout":
      return [
        `Standard checkout ${text(d.standard_checkout_local)}.`,
        `Later checkout is automatic until ${text(d.automatic_extension_until_local)} and needs a manager until ${text(d.reviewed_extension_until_local)}, if the room's next arrival allows it.`,
      ];
    case "breakfast":
      return [`Served ${text(d.served)} ${text(d.breakfast_start_local)}–${text(d.breakfast_end_local)}.`];
    case "housekeeping": {
      const lines = [
        `On request: ${list(d.supported_items)}; up to ${text(d.max_quantity_per_request)} per request.`,
      ];
      if (d.room_cleaning_from_local && d.room_cleaning_until_local) {
        lines.push(
          `Room cleaning from ${text(d.room_cleaning_from_local)} until ${text(d.room_cleaning_until_local)}; one open cleaning request per stay.`,
        );
      }
      if (d.towels_per_stay != null && d.pillows_per_stay != null) {
        lines.push(`Per stay: up to ${text(d.towels_per_stay)} towels and ${text(d.pillows_per_stay)} pillows.`);
      }
      lines.push("Recorded as a request for simulated staff; no delivery time is promised.");
      return lines;
    }
    case "maintenance":
      return [
        `Report problems with: ${list(d.supported_categories)}.`,
        "Recorded as a report for simulated staff; no repair time is promised. Not an emergency service.",
      ];
    case "facilities":
      return [...venues(d), text(d.booking)];
    case "dining":
      return [...venues(d), text(d.ordering)];
    case "internet":
      return [
        `Wi-Fi “${text(d.wifi_network)}”. ${text(d.price)}`,
        text(d.coverage),
        `Password: ${text(d.password_location)}`,
      ];
    case "house_rules":
      return [
        `Quiet hours ${text(d.quiet_hours_start_local)}–${text(d.quiet_hours_end_local)}.`,
        `Smoking: ${text(d.smoking)}`,
        `Pets: ${text(d.pets)}`,
      ];
    case "parking":
      return [
        text(d.location),
        `${text(d.hours)} ${text(d.ev_chargers)} EV chargers.`,
        text(d.price),
      ];
    case "luggage":
      return [
        `${text(d.location)} ${text(d.before_check_in)}`,
        `After checkout until ${text(d.after_checkout_until_local)}. ${text(d.price)}`,
      ];
    case "room_amenities":
      return [list(d.items), text(d.notes)];
    default:
      return [];
  }
}

function TopicCard({ item, read }: { item: HandbookTopic; read: boolean }) {
  const lines = item.data ? topicLines(item.topic, item.data).filter(Boolean) : [];
  return (
    <li
      className={read ? "policy-card policy-read" : "policy-card"}
      data-testid={`policy-${item.topic}`}
    >
      <div className="policy-head">
        <span className="policy-title">{TITLES[item.topic] ?? item.topic}</span>
        <code className="chip">{item.topic}</code>
      </div>
      {item.available ? (
        <ul className="policy-lines">
          {lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      ) : (
        <p className="empty">No stored policy. The agent says it does not know.</p>
      )}
      {read && (
        <span className="policy-read-mark" data-testid={`policy-read-${item.topic}`}>
          Read by the agent in its last reply
        </span>
      )}
    </li>
  );
}

export default function PolicyPanel({ readTopics }: { readTopics: string[] }) {
  const [handbook, setHandbook] = useState<Handbook | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .handbook()
      .then((h) => live && setHandbook(h))
      .catch(() => live && setError("Could not read the hotel policy. Is the backend running?"));
    return () => {
      live = false;
    };
  }, []);

  const topics = [...(handbook?.topics ?? [])].sort(
    (a, b) => ORDER.indexOf(a.topic) - ORDER.indexOf(b.topic),
  );
  return (
    <section className="panel policy-panel" aria-labelledby="policy-title" data-testid="policy-panel">
      <h2 id="policy-title" className="panel-subhead">
        Hotel policy
      </h2>
      <p className="world-note">
        Everything the agent can know about the hotel: <code>get_hotel_policy</code> returns
        exactly these stored values. The values are fictional.
      </p>
      {error && (
        <p className="notice" role="alert">
          {error}
        </p>
      )}
      {!handbook && !error && <p className="empty">Reading the policy…</p>}
      <ol className="policy-list">
        {topics.map((item) => (
          <TopicCard key={item.topic} item={item} read={readTopics.includes(item.topic)} />
        ))}
      </ol>
    </section>
  );
}
