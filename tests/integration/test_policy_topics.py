"""Guest information topics: stored, validated, relayed exactly, never invented.

One read returns several topics, so "all the hotel's policies" is one call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import delete

from hotel_operations import errors
from hotel_operations.domain.hotel_info import INFO_TOPICS
from hotel_operations.hotel_info_v1 import HOTEL_INFO_V1
from hotel_operations.services import sessions
from hotel_operations.services.policy import ALL_TOPICS, read_policies
from hotel_operations.storage.db import Database
from hotel_operations.storage.models import HotelInfoTopic
from hotel_operations.tools.policy import GET_HOTEL_POLICY, PolicyArgs
from tests.conftest import Harness
from tests.support.fingerprint import business_fingerprint
from tests.support.models import RuleModel, hotel_rule

BASE_KEYS = {"topic", "policy_version"}


def _binding(db: Database) -> sessions.SessionBinding:
    with db.write() as s:
        _, demo = sessions.create_session(s, "G-001")
        return sessions.binding_of(demo)


@pytest.fixture
def hotel(make_harness: Any) -> Harness:
    h: Harness = make_harness(RuleModel(hotel_rule))
    return h


def test_every_info_topic_returns_exactly_the_stored_document(db: Database) -> None:
    binding = _binding(db)
    with db.read() as s:
        for topic in INFO_TOPICS:
            read = read_policies(s, binding, [topic])
            (data,) = read["data"]["topics"]
            document = {k: v for k, v in data.items() if k not in BASE_KEYS}
            assert document == HOTEL_INFO_V1[topic], topic
            assert (data["topic"], read["data"]["source"]) == (topic, "Simulated hotel policy")
            assert read["meta"]["versions"] == {f"info_topic:{topic}": 1}


def test_the_tool_offers_every_topic_and_nothing_else() -> None:
    schema = GET_HOTEL_POLICY.json_schema()
    topics = schema["properties"]["topics"]
    assert topics["type"] == "array"
    assert topics["items"]["enum"] == list(ALL_TOPICS)
    assert set(INFO_TOPICS) < set(ALL_TOPICS)


@pytest.mark.parametrize(
    "args",
    [
        {"topics": []},
        {"topics": ["breakfast"] * (len(ALL_TOPICS) + 1)},
        {"topics": ["airport_shuttle"]},
        {"topic": "breakfast"},
        {"topics": ["breakfast"], "hotel_id": "other"},
    ],
)
def test_the_arguments_are_bounded(args: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PolicyArgs.model_validate(args)


def test_one_read_returns_every_topic_and_states_the_source_once(db: Database) -> None:
    binding = _binding(db)
    with db.read() as s:
        read = read_policies(s, binding, list(ALL_TOPICS))
    data = read["data"]
    assert [t["topic"] for t in data["topics"]] == list(ALL_TOPICS)
    assert (data["source"], data["hotel_timezone"]) == ("Simulated hotel policy", "Europe/Istanbul")
    assert "unavailable_topics" not in data
    assert all("source" not in t and "hotel_timezone" not in t for t in data["topics"])
    assert read["meta"]["versions"] == {
        "policy": 1,
        **{f"info_topic:{topic}": 1 for topic in INFO_TOPICS},
    }


def test_a_repeated_topic_is_read_once(db: Database) -> None:
    binding = _binding(db)
    with db.read() as s:
        data = read_policies(s, binding, ["parking", "breakfast", "parking"])["data"]
    assert [t["topic"] for t in data["topics"]] == ["parking", "breakfast"]


def test_a_missing_topic_among_several_is_listed_not_invented(db: Database) -> None:
    binding = _binding(db)
    with db.write() as s:
        s.execute(delete(HotelInfoTopic).where(HotelInfoTopic.topic == "parking"))
    with db.read() as s:
        data = read_policies(s, binding, ["parking", "dining"])["data"]
    assert [t["topic"] for t in data["topics"]] == ["dining"]
    assert data["unavailable_topics"] == ["parking"]


@pytest.mark.parametrize("damage", ["missing", "invalid"])
def test_a_missing_or_malformed_document_is_unavailable(db: Database, damage: str) -> None:
    binding = _binding(db)
    with db.write() as s:
        if damage == "missing":
            s.execute(delete(HotelInfoTopic).where(HotelInfoTopic.topic == "internet"))
        else:
            row = s.get(HotelInfoTopic, ("hotel_demo", "internet"))
            assert row is not None
            row.content = {**row.content, "password": "hunter2"}  # not a field of the type
    with db.read() as s, pytest.raises(errors.DomainError) as caught:
        read_policies(s, binding, ["internet"])
    assert caught.value.code == "POLICY_UNAVAILABLE"


def test_an_info_question_reads_the_policy_and_writes_nothing(hotel: Harness) -> None:
    token = hotel.session("G-002")["token"]
    before = business_fingerprint(hotel.services.db)
    run = hotel.ask(token, "What time does the pool open?")
    assert [a["tool_name"] for a in run["activity"]] == ["get_hotel_policy"]
    assert run["activity"][0]["summary"] == {"topics": ["facilities"]}
    assert "Pool 08:00-20:00" in run["final_message"]
    assert run["artifacts"] == []
    assert business_fingerprint(hotel.services.db) == before


def test_all_policies_are_one_tool_call_inside_the_turn_limit(hotel: Harness) -> None:
    """A question about all the hotel's policies once hit RUN_MAX_TURNS after six single-topic
    reads. One read of every topic now answers it in two model calls."""
    token = hotel.session("G-002")["token"]
    before = business_fingerprint(hotel.services.db)
    run = hotel.ask(token, "Can you tell me all the hotel policies?")
    assert run["status"] == "completed", run.get("error")
    assert [a["tool_name"] for a in run["activity"]] == ["get_hotel_policy"]
    assert run["activity"][0]["summary"] == {"topics": list(ALL_TOPICS)}
    assert run["final_message"].startswith(f"The hotel's policies ({len(ALL_TOPICS)} topics)")
    assert hotel.services.settings.max_turns == 6  # the bound did not have to move
    assert business_fingerprint(hotel.services.db) == before


def test_the_handbook_shows_every_topic_as_the_tool_returns_it(hotel: Harness) -> None:
    r = hotel.client.get("/api/hotel/policy")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [t["topic"] for t in body["topics"]] == list(ALL_TOPICS)
    assert all(t["available"] for t in body["topics"])
    binding = _binding(hotel.services.db)
    with hotel.services.db.read() as s:
        read = read_policies(s, binding, list(ALL_TOPICS))["data"]
    assert [item["data"] for item in body["topics"]] == read["topics"]
    assert (body["source"], body["hotel_timezone"]) == (read["source"], read["hotel_timezone"])


def test_the_handbook_lists_a_missing_topic_as_unavailable(hotel: Harness) -> None:
    with hotel.services.db.write() as s:
        s.execute(delete(HotelInfoTopic).where(HotelInfoTopic.topic == "parking"))
    topics = {t["topic"]: t for t in hotel.client.get("/api/hotel/policy").json()["topics"]}
    assert topics["parking"] == {"topic": "parking", "available": False, "data": None}
    assert topics["dining"]["available"] is True


def test_upgraded_database_gets_the_info_topics(tmp_path: Path) -> None:
    from alembic import command

    from hotel_operations.storage.migrate import alembic_config, upgrade_to_head
    from tests.support.eras import seed_core_schema_era

    path = tmp_path / "old.sqlite"
    command.upgrade(alembic_config(path), "0001")
    db = Database(path)
    with db.write() as s:
        seed_core_schema_era(s)
    db.dispose()
    upgrade_to_head(path)
    db = Database(path)
    try:
        binding = _binding(db)
        with db.read() as s:
            read = read_policies(s, binding, list(INFO_TOPICS))["data"]["topics"]
            for topic, data in zip(INFO_TOPICS, read, strict=True):
                assert {k: v for k, v in data.items() if k not in BASE_KEYS} == HOTEL_INFO_V1[topic]
    finally:
        db.dispose()
