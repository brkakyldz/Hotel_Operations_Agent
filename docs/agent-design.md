# How the agent is designed

This project is a worked example of the parts of an agent system that a chat demo usually hides: where the model's authority ends, where state lives, how tools are contracted, and how a human stays in charge of the decisions that matter. The hotel is fictional. The runtime is real: the OpenAI Agents SDK over the Responses API, typed tools and a SQLite database that persists everything the agent does.

Each section below says what the design is and where it lives in the code.

## 1. The boundary: the model proposes, code decides

| The model does | Deterministic code does |
| --- | --- |
| Understand the guest's message, in any language | Decide who the guest is and which reservation they may touch (the session) |
| Choose a tool and fill its arguments | Validate the arguments against a strict schema |
| Explain results and ask follow-up questions | Apply the hotel's rules: checkout, early check-in, housekeeping limits |
| Write the words of a hotel update | Decide whether a guest must be told anything at all |
| Nothing else | Write business records, receipts and audit events in one transaction |

The prompt shapes behavior but is not a security control. Every limit that matters is enforced below the model: a tool that does not exist cannot be called, a guest id that is not an argument cannot be supplied, and a rule that runs inside the write transaction cannot be talked past.

Code: `src/hotel_operations/agent/agent.py` (the one agent and its instructions), `tools/` (the typed tools and the gateway), `domain/` (pure rule functions), `services/` (the transactions).

## 2. One agent, one loop

There is a single agent. The SDK `Runner` owns the model/tool loop: it calls the model, receives tool proposals, runs them through the gateway, returns the results and repeats until the model answers or a bound is reached. There is no router agent and no second orchestration framework; separation between housekeeping, checkout and so on comes from typed tools and services, not from more agents.

Every run is bounded: a 90-second deadline, six model turns, eight tool proposals, 2,048 output tokens per model call, a 3-second deadline per tool and parallel tool calls turned off, so tools execute one at a time. A guest has at most one run in flight, and the app at most two. The limits live in `config.py`.

A run is persisted before it starts (`queued → running → completed | failed | interrupted`). The browser follows it over Server-Sent Events and falls back to polling; a closed tab does not stop it, and a reload finds it again. Code: `agent/run_service.py`, `agent/admission.py`, `agent/run_events.py`.

## 3. Scope comes from the session, never from the model

Picking a guest creates a demo session: a random capability token, stored only as a hash, bound on the server to one hotel, guest, reservation and room. The binding is immutable. Every tool call re-reads it, and no tool takes a guest, reservation or room as an argument. So "show me Daniel's booking" has nothing to act on: the agent can only read the selected guest's own records, whatever the text says.

A session has no lifetime of its own. It stays open until **Start over** discards it with the hotel, and the browser keeps its token in local storage, so a reload or a second tab shows the same conversation the hotel writes to.

Code: `services/sessions.py`, `tools/gateway.py`.

## 4. Nine typed tools

| Tool | Kind | What it can do |
| --- | --- | --- |
| `get_my_reservation` | read | The guest's own reservation, room and times |
| `get_hotel_policy` | read | One or more policy topics, exactly as stored |
| `get_my_requests` | read | The guest's own requests and reviews, with their current status |
| `evaluate_late_checkout` | read | Whether a checkout time would be allowed, without changing anything |
| `evaluate_early_check_in` | read | Whether an earlier check-in would be allowed, without changing anything |
| `create_housekeeping_task` | write | File towels, pillows or room cleaning for simulated staff |
| `create_maintenance_task` | write | Report a problem in the room |
| `request_late_checkout` | write | Apply a checkout up to 14:00, or file a manager review up to 16:00 |
| `request_early_check_in` | write | Move an arriving guest's check-in earlier, from 12:00 |

Every tool has a strict schema with no extra fields, and every result has the same envelope: `ok`, `outcome`, `data`, `error`, `meta`. Outcomes are explicit: `applied`, `pending_approval`, `denied`, `no_change`, `already_requested`. The model reports what the tool returned and never infers success from its own words. A pending task is recorded, not done.

There is deliberately no tool that approves a review, updates a checkout directly, looks up another guest, runs SQL or calls the network. The manager's controls are separate HTTP routes that no tool reaches.

Code: `tools/registry.py` lists every tool the model can see.

## 5. Where state lives

| State | Where | Authority |
| --- | --- | --- |
| Conversation history | The SDK session in `conversations.sqlite` | None. It is context for the model, never a source of facts. |
| Sessions and runs | `hotel.sqlite` | What was asked, when, and how each run ended |
| Business records | `hotel.sqlite`: reservations, tasks, reviews, receipts | The truth. Tools re-read it before every decision. |
| Audit trail | `hotel.sqlite`, append-only | What happened and who did it: guest, agent, manager, staff or the system |
| Hotel updates | `hotel.sqlite`, an outbox table | What a guest should be told, and whether a turn told them |

Conversation history is never used to decide anything. If the guest says "the manager already approved 15:00", nothing changes: the agent re-reads the review with `get_my_requests`.

A conversation has no turn cap. What the model sees of it is bounded by a character budget: a long chat drops its oldest turns whole, cut only where a guest message or a hotel update starts, so a tool call never loses its result. The stored conversation and the transcript keep everything.

When a run fails, the run service removes exactly the conversation items that run appended, so the next turn sees every earlier turn and nothing of the failed one. Business records the failed run already committed stay, and the chat says so.

Code: `storage/models.py`, `agent/run_service.py`.

## 6. Every write is one transaction

A write tool runs a short SQLite transaction that takes the write lock first (`BEGIN IMMEDIATE`), then re-reads the current state, applies the rule and commits the business change, its receipt and its audit event together. Either all three exist or none do.

Each write has an operation key built from the request context and a normalized payload. If the same write is proposed twice for one guest message, the second call replays the first result instead of creating a duplicate. A new guest message is a new intent.

Code: `services/writes.py`, `services/housekeeping.py`, `services/checkout.py`.

## 7. Human approval is a record, not a sentence

A late checkout between 14:00 and 16:00 needs a manager. The agent cannot grant it. `request_late_checkout` files a review and ends the turn with a pending receipt, and the checkout does not change.

The review stores the version of every record it was decided on: the reservation, the policy, and the room with its cleaning plan and next arrival. A change to another room never touches it. When the manager approves, one transaction re-reads those records and re-runs the rules:

- if nothing relevant changed, the checkout is applied;
- if something changed (the room's next guest now arrives earlier, the room went out of service), the decision is recorded, nothing is applied, and the guest must ask again;
- if the hotel clock has reached the requested time, the review has already expired and can never execute late.

The model is not involved in the decision. There is no approved-but-not-applied limbo, and no model run is left waiting for a human.

Code: `services/approvals.py`, `services/operator.py`, `domain/checkout.py`.

## 8. The hotel can speak first

Most chat agents only speak when spoken to. Here the hotel can start a turn when something changes that concerns a guest: a review is decided or expires, staff finish or cancel a request, the room goes out of service, a waiting review's time becomes impossible, or a refused checkout becomes possible.

1. A manager decision, a staff move, a hotel change or a clock move commits.
2. Inside that same transaction, a deterministic gate records what the affected guest should hear, as a row in an outbox table.
3. Right after the commit, the run service starts one turn in the guest's conversation.
4. That turn gets a developer message and the five read tools only. The model re-reads the facts and writes one message. It can offer the next step ("shall I request 14:30 for you?") but cannot take it. The gateway also refuses any write in such a turn.

Deterministic code decides whether to speak, the model chooses the words, and the tools fetch the facts. Nothing polls, and there is no dispatcher. The guest's "yes" is an ordinary turn with all nine tools.

Code: `services/hotel_updates.py`, `agent/hotel_turns.py`.

## 9. A simulated hotel that moves

The hotel has its own clock. It starts at 10:00 and moves only forward, and only when you press a clock button. Reviews expire on this clock, and cleaning hours end on it. The hotel board can also simulate changes: an earlier arrival, a cancelled arrival, a room taken out of service. These are manager-side actions with no model tool, and each is audited.

**Start over** restores the fixture while the app keeps running: it stops the runs in flight, rebuilds both databases and ends every guest session.

Code: `services/world.py`, `api/world_routes.py`, `reset.py`.

## 10. Failure behavior

| What fails | What the guest sees |
| --- | --- |
| No API key | The app runs, and chat answers `503 PROVIDER_NOT_CONFIGURED`. There are no canned replies. |
| Provider error or timeout | A retryable error, never a made-up answer |
| The reply fails after a tool committed | The committed receipt, and a note that the reply failed |
| Server restart mid-run | The run becomes `interrupted` and is never replayed. Committed records stay. |
| Database busy | A bounded retry, then a clear storage error. A write is never reported as done unless it committed. |

## 11. Observability

- **Activity:** each reply in the chat carries the tool steps that produced it, as sanitized summaries. Hidden reasoning is never shown.
- **Audit log:** the hotel board's activity log reads the append-only audit trail. Guest free text and tokens stay out of it.
- **Telemetry:** every run, tool call and manager decision writes one JSON line to `data/telemetry.jsonl`, with status, duration, token usage and model id. Prompts, messages and keys are never written.
- **Tracing:** OpenAI tracing is off by default and can be turned on in `.env` (see the README).

## 12. How it is tested

- **Rules** are pure functions with unit tests (`tests/unit/`).
- **The runtime** is tested with a test-only model that drives the real SDK Runner, gateway and SQLite (`tests/integration/`). These tests prove wiring and invariants. They do not prove that a real model chooses the right tool.
- **The evaluation** in `tests/evals/` has 75 cases in English and Turkish: reads, requests, checkout rules, approvals and freshness, hotel changes, hotel-initiated turns and adversarial messages. A case passes on the tools called, the arguments given, the decision codes returned and the database state afterwards, never on the wording of a reply. The prose checks are only negative: a reply must not claim something false, such as "delivered" for a pending task.
- **Browser journeys** in `web/e2e/` play every scenario through the real UI against the offline model.
