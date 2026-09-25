"""Demo selector sessions: capability handling, expiry, tampering and isolation."""

from __future__ import annotations

from sqlalchemy import select

from hotel_operations.clock import utcnow
from hotel_operations.storage.models import DemoSession
from tests.conftest import Harness


def test_health_has_label_and_no_secrets(harness: Harness) -> None:
    body = harness.client.get("/api/health").json()
    assert body["status"] == "ok"
    assert "Simulation" in body["simulation_label"]
    assert "key" not in str(body).lower().replace("provider_configured", "")


def test_requests_for_a_foreign_host_are_refused(harness: Harness) -> None:
    """A page that rebinds its own domain to 127.0.0.1 cannot drive the local API."""
    evil = {"Host": "attacker.example:8000"}
    assert harness.client.get("/api/health", headers=evil).status_code == 400
    assert (
        harness.client.post(
            "/api/demo/sessions", json={"guest_id": "G-001"}, headers=evil
        ).status_code
        == 400
    )
    assert harness.client.get("/api/health", headers={"Host": "localhost:8000"}).status_code == 200
    assert harness.client.get("/api/health").status_code == 200


def test_guest_selector_lists_safe_summaries(harness: Harness) -> None:
    guests = harness.client.get("/api/demo/guests").json()["guests"]
    assert [g["guest_id"] for g in guests] == ["G-001", "G-002", "G-003"]
    sofia = guests[2]
    assert sofia == {
        "guest_id": "G-003",
        "display_name": "Sofia Rossi",
        "room_number": "218",
        "reservation_reference": "R-1101",
        "display_status": "Arriving today",
    }
    assert "scheduled_checkout" not in str(guests)


def test_session_binding_is_server_resolved_and_token_hashed(harness: Harness) -> None:
    created = harness.session("G-002")
    assert created["context"]["reservation_reference"] == "R-1088"
    token = created["token"]
    assert len(token) >= 43  # 256-bit urlsafe
    with harness.services.db.read() as s:
        row = s.scalar(select(DemoSession).where(DemoSession.id == created["session_id"]))
        assert row is not None
        assert row.capability_hash != token and token not in row.capability_hash
        assert (row.guest_id, row.reservation_id, row.room_id) == ("G-002", "rsv_1088", "room_412")
    me = harness.client.get("/api/session", headers=harness.auth(token)).json()
    assert me["context"]["guest_name"] == "Daniel Kim"
    assert "capability_hash" not in me


def test_session_body_cannot_carry_scope_fields(harness: Harness) -> None:
    r = harness.client.post(
        "/api/demo/sessions", json={"guest_id": "G-001", "reservation_id": "rsv_1088"}
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_unknown_or_unselectable_guest(harness: Harness) -> None:
    assert harness.client.post("/api/demo/sessions", json={"guest_id": "G-999"}).status_code == 404


def test_invalid_tampered_and_missing_tokens_are_401(harness: Harness) -> None:
    token = harness.session()["token"]
    for headers in (
        {},
        {"Authorization": "Bearer "},
        {"Authorization": f"Bearer {token}x"},
        {"Authorization": f"Basic {token}"},
        {"Authorization": "Bearer " + "a" * 300},
    ):
        r = harness.client.get("/api/session", headers=headers)
        assert r.status_code == 401, headers
        assert r.json()["error"]["code"] == "INVALID_SESSION"


def test_closed_session_is_401(harness: Harness) -> None:
    created = harness.session()
    with harness.services.db.write() as s:
        row = s.get(DemoSession, created["session_id"])
        assert row is not None
        row.closed_at = utcnow()
    r = harness.client.get("/api/session", headers=harness.auth(created["token"]))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_SESSION"
    r = harness.chat(created["token"], "hello")
    assert r.status_code == 401


def test_cross_session_run_lookup_is_404(harness: Harness) -> None:
    emma = harness.session("G-001")["token"]
    daniel = harness.session("G-002")["token"]
    run = harness.ask(emma, "When do I check out?")
    r = harness.client.get(f"/api/runs/{run['run_id']}", headers=harness.auth(daniel))
    assert r.status_code == 404
    unknown = harness.client.get("/api/runs/run_doesnotexist", headers=harness.auth(daniel))
    assert unknown.status_code == 404
    assert r.json() == unknown.json()  # indistinguishable


def test_new_session_for_same_guest_cannot_read_old_transcript(harness: Harness) -> None:
    first = harness.session("G-001")["token"]
    run = harness.ask(first, "Where am I staying?")
    second = harness.session("G-001")["token"]
    r = harness.client.get(f"/api/runs/{run['run_id']}", headers=harness.auth(second))
    assert r.status_code == 404


def test_session_scope_drift_is_409(harness: Harness) -> None:
    from hotel_operations.storage.models import Reservation, Room

    token = harness.session("G-001")["token"]
    with harness.services.db.write() as s:
        s.add(
            Room(
                id="room_998",
                hotel_id="hotel_demo",
                number="998",
                housekeeping_state="clean",
                out_of_service=False,
                version=1,
            )
        )
        s.flush()
        s.get(Reservation, "rsv_1042").room_id = "room_998"  # type: ignore[union-attr]
    r = harness.client.get("/api/session", headers=harness.auth(token))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SESSION_SCOPE_STALE"
    r = harness.chat(token, "hi")
    assert r.status_code == 409


def test_cors_allows_only_local_ui_origin(harness: Harness) -> None:
    ok = harness.client.options(
        "/api/health",
        headers={"Origin": "http://127.0.0.1:5173", "Access-Control-Request-Method": "GET"},
    )
    assert ok.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"
    bad = harness.client.options(
        "/api/health",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert bad.headers.get("access-control-allow-origin") is None
