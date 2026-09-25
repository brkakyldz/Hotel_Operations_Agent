"""Test-only model adapters for the real SDK Runner (never product code).

``RuleModel`` implements the SDK ``Model`` interface. Each call inspects the
actual input the Runner assembled (user message, prior tool outputs, session
history) and applies a small rule function to produce the next output. It never
bypasses the real gateway, services or storage: tool calls it emits are executed
by the SDK through the registered wrappers, and their real results come back in
the next call's input.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agents.items import ModelResponse, TResponseInputItem
from agents.models.interface import Model
from agents.testing import assistant_message, function_call
from agents.testing.model import ModelStep, _stream_events_for_step
from agents.usage import Usage

from hotel_operations.services.policy import ALL_TOPICS

_ids = itertools.count(1)


@dataclass
class Turn:
    """What the model sees on one call."""

    input: list[dict[str, Any]]
    tools: list[str]
    instructions: str | None
    model_settings: Any

    @property
    def last(self) -> dict[str, Any]:
        return self.input[-1] if self.input else {}

    def _turn_start(self) -> dict[str, Any] | None:
        """The item that started this turn: a guest's message or a hotel update."""
        for item in reversed(self.input):
            if item.get("role") in ("user", "developer"):
                return item
        return None

    def last_user_text(self) -> str:
        """The guest's message this turn answers; empty in a hotel update's turn."""
        start = self._turn_start()
        if start is None or start.get("role") != "user":
            return ""
        content = start.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        return ""

    def hotel_update(self) -> list[dict[str, Any]] | None:
        """The recorded updates a hotel-initiated turn carries, or None in a guest's turn."""
        start = self._turn_start()
        if start is None or start.get("role") != "developer":
            return None
        text = str(start.get("content", ""))
        events: list[dict[str, Any]] = json.loads(text[text.index("[") :])
        return events

    def tool_outputs_since_user(self) -> list[dict[str, Any]]:
        """Parsed tool results received since this turn started."""
        outs: list[dict[str, Any]] = []
        for item in reversed(self.input):
            if item.get("role") in ("user", "developer"):
                break
            if item.get("type") == "function_call_output":
                try:
                    outs.append(json.loads(item.get("output", "")))
                except ValueError:
                    outs.append({"raw": item.get("output")})
        return list(reversed(outs))


Rule = Callable[[Turn], Any | Awaitable[Any]]


def call(name: str, args: dict[str, Any] | str | None = None) -> Any:
    n = next(_ids)
    return function_call(name, args if args is not None else {}, call_id=f"call_{n}")


def say(text: str) -> Any:
    return assistant_message(text, item_id=f"msg_{next(_ids)}")


@dataclass
class RuleModel(Model):
    rule: Rule
    usage_per_call: tuple[int, int] = (100, 20)
    model: str = "test-rule-model"
    turns: list[Turn] = field(default_factory=list)

    async def get_response(  # type: ignore[override]
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: Any,
        tools: list[Any],
        output_schema: Any,
        handoffs: Any,
        tracing: Any,
        *,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any = None,
    ) -> ModelResponse:
        assert previous_response_id is None, "provider-managed history must not be used"
        assert conversation_id is None, "provider conversation IDs must not be used"
        items = [{"role": "user", "content": input}] if isinstance(input, str) else list(input)
        turn = Turn(
            input=[dict(i) if isinstance(i, dict) else i.model_dump() for i in items],  # type: ignore[union-attr]
            tools=[t.name for t in tools],
            instructions=system_instructions,
            model_settings=model_settings,
        )
        self.turns.append(turn)
        result = self.rule(turn)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, BaseException):
            raise result
        output = result if isinstance(result, list) else [result]
        inp, out = self.usage_per_call
        return ModelResponse(
            output=output,
            usage=Usage(requests=1, input_tokens=inp, output_tokens=out, total_tokens=inp + out),
            response_id=None,
            request_id=f"req_{next(_ids)}",
        )

    async def stream_response(  # type: ignore[override]
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: Any,
        tools: list[Any],
        output_schema: Any,
        handoffs: Any,
        tracing: Any,
        *,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any = None,
    ) -> AsyncIterator[Any]:
        """The run service streams: the same rule, emitted as SDK stream events
        (created, output-text deltas, completed with usage) by the SDK's own converter."""
        response = await self.get_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )
        step = ModelStep(
            output=list(response.output),
            usage=response.usage,
            response_id=f"resp_{next(_ids)}",
            request_id=response.request_id,
        )
        for event in _stream_events_for_step(step, preserve_raw_usage=False):
            yield event


def reservation_rule(turn: Turn) -> Any:
    """Read the reservation once, then answer from the actual tool result."""
    outputs = turn.tool_outputs_since_user()
    if not outputs:
        return call("get_my_reservation")
    result = outputs[-1]
    if result.get("ok"):
        data = result["data"]
        return say(
            f"Your reservation {data['reservation_reference']} is for room {data['room_number']}; "
            f"status {data['status']}; checkout {data['scheduled_checkout']}."
        )
    return say(f"I could not read your reservation ({result['error']['code']}).")


_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "bir": 1, "iki": 2, "üç": 3, "dört": 4, "beş": 5, "altı": 6}  # fmt: skip


def _quantity(text: str, default: int = 1) -> int:
    for token in text.replace(",", " ").split():
        if token.isdigit():
            return int(token)
        if token in _NUMBERS:
            return _NUMBERS[token]
    return default


_MAINTENANCE_KEYWORDS = {
    "hvac": ("air condition", "klima", "heating", "ısıtma"),
    "plumbing": ("leak", "sız", "shower", "duş", "toilet", "tuvalet", "lavabo"),
    "electrical": ("light", "lamba", "ışık", "socket", "priz"),
    "other": ("broken", "bozuk"),
}


_TIME = re.compile(r"\b(\d{1,2})[:.](\d{2})\b")


def _checkout_time(text: str) -> str | None:
    """A checkout message with an explicit HH:MM becomes a demo-day ISO time (test double)."""
    if not any(w in text for w in ("checkout", "check out", "check-out", "çıkış")):
        return None
    found = _TIME.search(text)
    if found is None:
        return None
    return f"2026-09-22T{int(found.group(1)):02d}:{found.group(2)}:00+03:00"


_CHECK_IN_WORDS = ("check in", "check-in", "checkin", "giriş")
_HYPOTHETICAL = ("possible", "would", "how early", "mümkün")
_GENERAL_CHECK_IN = ("what time is check-in", "check-in time", "giriş saati")


def _check_in(text: str) -> tuple[bool, str | None]:
    """Is this about check-in, and with which explicit demo-day time (test double)?"""
    if not any(w in text for w in _CHECK_IN_WORDS):
        return False, None
    found = _TIME.search(text)
    if found is None:
        return True, None
    return True, f"2026-09-22T{int(found.group(1)):02d}:{found.group(2)}:00+03:00"


# Guest information topics, checked after the request, maintenance and breakfast
# keywords so "towel" or "leak" never lands here.
_INFO_KEYWORDS = {
    "facilities": ("pool", "gym", "spa", "havuz", "spor salonu", "fitness"),
    "dining": ("restaurant", "bar ", "dinner", "room service", "restoran", "akşam yemeği"),
    "internet": ("wifi", "wi-fi", "internet", "password", "şifre"),
    "house_rules": ("quiet", "smok", "pet", "sigara", "evcil", "sessiz"),
    "parking": ("parking", "park my", "garage", "otopark", "charger", "şarj"),
    "luggage": ("luggage", "bags", "bagaj", "valiz"),
    "room_amenities": ("safe", "minibar", "kettle", "hair dryer", "kasa", "saç kurutma"),
}


# "All the hotel's policies" reads every topic in one call.
_ALL_POLICIES = (
    "all the hotel",
    "all hotel polic",
    "all polic",
    "every polic",
    "all the polic",
    "tüm otel",
    "tüm poli",
    "bütün otel",
    "bütün poli",
)


def _info_topic(text: str) -> str | None:
    for topic, words in _INFO_KEYWORDS.items():
        if any(w in text for w in words):
            return topic
    return None


def _maintenance_category(text: str) -> str | None:
    for category, words in _MAINTENANCE_KEYWORDS.items():
        if any(w in text for w in words):
            return category
    return None


# --- hotel updates --------------------------------------------------------------------

# No re.IGNORECASE: under it the Turkish capital "İ" matches a plain "i" (as in "Can I").
_TURKISH = re.compile(r"[ğşıçöüİĞŞÇÖÜ]|(merhaba|lütfen|mümkün|odam|istiyorum)")


def _conversation_lang(turn: Turn) -> str:
    """The language of the guest's own messages so far (a hotel update has none)."""
    for item in turn.input:
        if item.get("role") == "user" and _TURKISH.search(str(item.get("content", "")).lower()):
            return "tr"
    return "en"


def update_reads(update: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """What the agent re-reads for each kind of update before it tells the guest."""
    reads: list[tuple[str, dict[str, Any]]] = []

    def add(name: str, args: dict[str, Any]) -> None:
        if (name, args) not in reads:
            reads.append((name, args))

    for u in update:
        kind, facts = u["kind"], u.get("facts") or {}
        requested = {"requested_checkout_local": facts.get("requested_checkout_local")}
        if kind in ("review_closed", "task_closed", "review_at_risk"):
            add("get_my_requests", {"cursor": None})
        if kind in ("review_closed", "room_out_of_service"):
            add("get_my_reservation", {})
        if kind in ("review_at_risk", "checkout_now_possible"):
            add("evaluate_late_checkout", requested)
    return reads


def _hhmm(value: Any) -> str:
    text = str(value or "")
    return text[11:16] if len(text) >= 16 else "?"


_ITEM = {
    "en": {"towels": "towels", "pillows": "pillows", "room_cleaning": "room cleaning",
           "hvac": "air-conditioning report", "plumbing": "plumbing report",
           "electrical": "electrical report", "other": "maintenance report"},
    "tr": {"towels": "havlu", "pillows": "yastık", "room_cleaning": "oda temizliği",
           "hvac": "klima arıza bildirimi", "plumbing": "tesisat arıza bildirimi",
           "electrical": "elektrik arıza bildirimi", "other": "arıza bildirimi"},
}  # fmt: skip


# One template per update and language; the values come from the re-read tool results.
_UPDATE_TEXT: dict[str, dict[str, str]] = {
    "executed": {
        "en": "Good news: a manager approved your late checkout; your checkout is now {current}.",
        "tr": "Müjde: yönetici geç çıkışınızı onayladı, çıkış saatiniz artık {current}.",
    },
    "rejected": {
        "en": "A manager declined your late checkout request for {at}; "
        "your checkout stays at {current}.",
        "tr": "Yönetici {at} geç çıkış talebinizi onaylamadı; çıkış saatiniz {current}.",
    },
    "expired": {
        "en": "Your late checkout request for {at} expired before a manager decided; "
        "your checkout stays at {current}.",
        "tr": "{at} geç çıkış talebinizin, yönetici karar vermeden süresi doldu; "
        "çıkış saatiniz {current}.",
    },
    "stale": {
        "en": "Your late checkout request for {at} could not be applied after a change at the "
        "hotel; your checkout stays at {current}.",
        "tr": "Oteldeki bir değişiklik nedeniyle {at} geç çıkış talebiniz uygulanamadı; "
        "çıkış saatiniz {current}.",
    },
    "completed": {
        "en": "Your {what} request ({task}) was marked completed in the simulator.",
        "tr": "{what} talebiniz ({task}) simülasyonda tamamlandı olarak işaretlendi.",
    },
    "window_closed": {
        "en": "Cleaning hours ended before your {what} ({task}) was started, so it was cancelled.",
        "tr": "Temizlik saatleri bittiği için başlamamış {what} talebiniz ({task}) iptal edildi.",
    },
    "cancelled": {
        "en": "Staff cancelled your {what} request ({task}).",
        "tr": "{what} talebiniz ({task}) personel tarafından iptal edildi.",
    },
    "room_out_of_service": {
        "en": "Your room {room} was taken out of service in the simulator.",
        "tr": "{room} numaralı odanız simülasyonda hizmet dışı bırakıldı.",
    },
    "review_at_risk": {
        "en": "A change at the hotel means {at} is no longer possible{latest}; "
        "your request is still waiting for a manager.",
        "tr": "Oteldeki bir değişiklik nedeniyle {at} artık mümkün değil{latest}; "
        "talebiniz hâlâ yönetici bekliyor.",
    },
    "checkout_now_possible": {
        "en": "A change at the hotel means checkout at {at} may now be possible{extra}. "
        "Shall I request it for you?",
        "tr": "Oteldeki bir değişiklik nedeniyle {at} çıkışı artık mümkün olabilir{extra}. "
        "Sizin için talep edeyim mi?",
    },
}
_NEEDS_REVIEW = {"en": " (it would need a manager's review)", "tr": " (yönetici onayı gerekir)"}
_LATEST = {"en": " (latest now {})", "tr": " (en geç {})"}  # only when a latest time exists


def _update_sentence(u: dict[str, Any], outs: list[dict[str, Any]], lang: str) -> str:
    """One grounded sentence per update: the facts come from the re-read tool results."""
    facts = u.get("facts") or {}
    kind = u["kind"]
    data = [o.get("data") or {} for o in outs]
    reservation = next((d for d in data if "scheduled_checkout" in d), {})
    evaluated = next((d for d in data if d.get("advisory")), {})
    latest = evaluated.get("latest_feasible_checkout_local")
    values = {
        "current": _hhmm(
            reservation.get("scheduled_checkout") or evaluated.get("current_checkout_local")
        ),
        "at": _hhmm(facts.get("requested_checkout_local")),
        "latest": _LATEST[lang].format(_hhmm(latest)) if latest else "",
        "what": _ITEM[lang].get(str(facts.get("item") or facts.get("category")), "request"),
        "task": facts.get("task_id"),
        "room": reservation.get("room_number") or facts.get("room_number"),
        "extra": _NEEDS_REVIEW[lang] if evaluated.get("decision") == "approval_required" else "",
    }
    if kind == "review_closed":
        key = str(facts.get("status"))
    elif kind == "task_closed":
        key = (
            "completed"
            if facts.get("status") == "completed"
            else "window_closed"
            if facts.get("reason") == "SERVICE_WINDOW_CLOSED"
            else "cancelled"
        )
    else:
        key = kind
    return _UPDATE_TEXT.get(key, _UPDATE_TEXT["stale"])[lang].format(**values)


def hotel_update_rule(turn: Turn, update: list[dict[str, Any]]) -> Any:
    """A hotel update's turn: re-read what each kind needs, then one message in the
    conversation's language. Never a write (test double of agent-prompt-n1-v2)."""
    outs = turn.tool_outputs_since_user()
    reads = update_reads(update)
    if len(outs) < len(reads):
        name, args = reads[len(outs)]
        return call(name, args)
    lang = _conversation_lang(turn)
    return say(" ".join(_update_sentence(u, outs, lang) for u in update))


def hotel_rule(turn: Turn) -> Any:
    """Keyword routing across the implemented tools (test double; not an NLU claim)."""
    update = turn.hotel_update()
    if update is not None:
        return hotel_update_rule(turn, update)
    outputs = turn.tool_outputs_since_user()
    text = turn.last_user_text().lower()
    about_check_in, check_in_at = _check_in(text)
    early = any(w in text for w in ("early", "earlier", "erken"))
    if not outputs and about_check_in:
        # Mirrors agent-prompt-r1-v1: a named early check-in time is a request; a hypothetical
        # question is evaluated; "as early as possible" needs the current time first.
        if check_in_at is not None:
            if any(w in text for w in _HYPOTHETICAL):
                return call("evaluate_early_check_in", {"requested_check_in_local": check_in_at})
            return call("request_early_check_in", {"requested_check_in_local": check_in_at})
        if not early and any(w in text for w in _GENERAL_CHECK_IN):
            return call("get_hotel_policy", {"topics": ["check_in"]})
        # "When is MY check-in?" and "how early?" both start from the own reservation.
        return call("get_my_reservation")
    if (
        about_check_in
        and early
        and check_in_at is None
        and len(outputs) == 1
        and outputs[0].get("ok")
        and "scheduled_check_in" in outputs[0].get("data", {})
    ):
        now = outputs[0]["meta"]["demo_time"]
        return call("evaluate_early_check_in", {"requested_check_in_local": now})
    if not outputs:
        if any(w in text for w in _ALL_POLICIES):
            return call("get_hotel_policy", {"topics": list(ALL_TOPICS)})
        checkout = _checkout_time(text)
        if checkout is not None:
            # Mirrors agent-prompt-r1-v1: a named own-checkout time is a request; only an
            # explicitly hypothetical question is an advisory evaluation.
            if any(w in text for w in ("possible", "would", "mümkün")):
                return call("evaluate_late_checkout", {"requested_checkout_local": checkout})
            return call(
                "request_late_checkout", {"requested_checkout_local": checkout, "reason": None}
            )
        if "request" in text or "talep" in text or "talebi" in text:
            return call("get_my_requests", {"cursor": None})
        category = _maintenance_category(text)
        if category is not None:
            return call(
                "create_maintenance_task",
                {"category": category, "description": turn.last_user_text().strip()[:500]},
            )
        if "breakfast" in text or "kahvalt" in text:
            return call("get_hotel_policy", {"topics": ["breakfast"]})
        if "towel" in text or "havlu" in text:
            return call(
                "create_housekeeping_task",
                {"item": "towels", "quantity": _quantity(text), "notes": None},
            )
        if "pillow" in text or "yastık" in text:
            return call(
                "create_housekeeping_task",
                {"item": "pillows", "quantity": _quantity(text), "notes": None},
            )
        if "clean" in text or "temizl" in text:
            return call(
                "create_housekeeping_task", {"item": "room_cleaning", "quantity": 1, "notes": None}
            )
        topic = _info_topic(text)
        if topic is not None:
            return call("get_hotel_policy", {"topics": [topic]})
        return call("get_my_reservation")
    result = outputs[-1]
    if not result.get("ok"):
        return say(f"That did not work: {result['error']['code']}.")
    data = result["data"]
    if "topics" in data:  # get_hotel_policy: one or more topics
        read = data["topics"]
        if len(read) > 1:
            missing = data.get("unavailable_topics") or []
            gap = f" No stored policy for: {', '.join(missing)}." if missing else ""
            return say(
                f"The hotel's policies ({len(read)} topics): "
                + ", ".join(t["topic"] for t in read)
                + f".{gap}"
            )
        data = read[0]
    if "requested_check_in_local" in data:
        earliest = data.get("earliest_possible_check_in_local")
        hint = f" Earliest possible: {earliest[11:16]}." if earliest else ""
        if "outcome" in data:
            return say(
                f"Early check-in request {data['outcome']} ({data['decision']}); "
                f"check-in is {data['current_check_in_local']}.{hint}"
            )
        return say(
            f"Advisory, nothing changed: {data['decision']} "
            f"({', '.join(data['reason_codes'])}).{hint}"
        )
    if data.get("topic") == "check_in":
        return say(
            f"Check-in is from {data['standard_check_in_local']}; early check-in from "
            f"{data['early_check_in_from_local']} when the room is ready."
        )
    if "outcome" in data and "decision" in data:
        return say(
            f"Checkout request {data['outcome']} ({data['decision']}); "
            f"checkout is {data['current_checkout_local']}."
        )
    if data.get("advisory"):
        return say(f"Advisory: {data['decision']} ({', '.join(data['reason_codes'])}).")
    if data.get("kind") == "housekeeping" and data.get("nothing_created"):
        return say(
            f"Housekeeping request {data['outcome']} ({', '.join(data['reason_codes'])}); "
            "nothing was created."
        )
    if data.get("kind") == "maintenance":
        return say(f"Reported issue {data['task_id']} ({data['status']}); not yet repaired.")
    if "task_id" in data:
        return say(f"Recorded request {data['task_id']} ({data['status']}).")
    if data.get("topic") in _INFO_KEYWORDS:
        venues = "; ".join(
            f"{v['name']} {v['opens_local']}-{v['closes_local']}" for v in data.get("venues", [])
        )
        return say(f"From the hotel's {data['topic']} policy: {venues or 'see the details'}.")
    if data.get("topic") == "breakfast":
        return say(
            f"Breakfast is served {data['breakfast_start_local']}-{data['breakfast_end_local']}."
        )
    if "requests" in data:
        pending = sum(1 for r in data["requests"] if r["status"] == "pending")
        reviews = [
            f" Late checkout review until {r['details']['requested_checkout_local'][11:16]}: "
            f"{r['status']}."
            for r in data["requests"]
            if r["kind"] == "late_checkout_review"
        ]
        return say(
            f"You have {len(data['requests'])} request(s), {pending} pending." + "".join(reviews)
        )
    return reservation_rule(turn)
