from tests.helpers import active_count, assert_all_invariants, backdate_expiry, count, hold


def test_hold_creates_held_reservation_with_server_side_expiry(client, db):
    r = hold(client, 5)
    assert r.status_code == 201
    body = r.json()
    res = body["reservation"]
    assert body["result"] == "HELD"
    assert res["status"] == "HELD" and res["effective_status"] == "HELD"
    assert res["seat_number"] == 5 and res["customer_ref"] == "22BCE0001"
    assert 295 <= res["seconds_remaining"] <= 300
    assert active_count(db, 5) == 1

    seat = client.get("/api/v1/events/1/seats/5").json()
    assert seat["status"] == "HELD" and seat["is_available"] is False
    assert seat["held_until"] is not None


def test_second_student_gets_409_for_held_seat(client):
    assert hold(client, 7, "A").status_code == 201
    r = hold(client, 7, "B")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "SEAT_UNAVAILABLE"
    assert err["details"]["seat_status"] == "HELD"


def test_unknown_seat_and_event_return_404(client):
    assert hold(client, 501).json()["error"]["code"] == "SEAT_NOT_FOUND"
    r = hold(client, 1, event_id=999)
    assert r.status_code == 404 and r.json()["error"]["code"] == "EVENT_NOT_FOUND"


def test_validation_errors_use_envelope(client):
    r = client.post("/api/v1/reservations", json={"event_id": 1, "seat_number": 1})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = client.post(
        "/api/v1/reservations", json={"event_id": 1, "seat_number": 1, "customer_ref": "bad ref!"}
    )
    assert r.status_code == 422


def test_lazy_expiry_lets_new_student_take_stale_seat_without_worker(client, db):
    first = hold(client, 9, "A").json()["reservation"]["id"]
    backdate_expiry(db, first)

    # Derived availability already reports it free, even though the row still says HELD.
    assert client.get("/api/v1/events/1/seats/9").json()["status"] == "AVAILABLE"
    assert count(db, "SELECT COUNT(*) FROM reservations WHERE status='HELD'") == 1

    r = hold(client, 9, "B")
    assert r.status_code == 201
    assert client.get(f"/api/v1/reservations/{first}").json()["status"] == "EXPIRED"
    assert active_count(db, 9) == 1
    assert_all_invariants(db)


def test_seat_listing_and_event_summary(client):
    hold(client, 1)
    hold(client, 2)
    seats = client.get("/api/v1/events/1/seats").json()
    assert seats["count"] == 500
    available = client.get("/api/v1/events/1/seats", params={"available_only": True}).json()
    assert available["count"] == 498
    assert all(s["is_available"] for s in available["seats"])
    summary = client.get("/api/v1/events/1").json()["seat_summary"]
    assert summary == {"total": 500, "available": 498, "held": 2, "confirmed": 0}
    assert client.get("/api/v1/events/999").status_code == 404
