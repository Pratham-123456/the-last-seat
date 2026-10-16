from tests.helpers import (
    active_count,
    assert_all_invariants,
    backdate_expiry,
    callback,
    confirm,
    count,
    hold,
)


def _hold_id(client, seat=1, customer="22BCE0001"):
    r = hold(client, seat, customer)
    assert r.status_code == 201, r.text
    return r.json()["reservation"]["id"]


def test_happy_path_view_hold_confirm_history(client, db):
    assert client.get("/api/v1/events/1/seats/3").json()["status"] == "AVAILABLE"
    rid = _hold_id(client, 3, "22BCE1111")

    r = confirm(client, rid, "SUCCESS")
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "CONFIRMED"
    assert body["reservation"]["status"] == "CONFIRMED"
    assert body["payment"]["status"] == "SUCCEEDED" and body["payment"]["payment_type"] == "CHARGE"

    assert client.get("/api/v1/events/1/seats/3").json()["status"] == "CONFIRMED"
    history = client.get("/api/v1/customers/22BCE1111/bookings").json()
    assert history["count"] == 1
    assert history["reservations"][0]["status"] == "CONFIRMED"
    assert len(history["reservations"][0]["payments"]) == 1
    assert_all_invariants(db)


def test_failed_payment_is_recorded_hold_kept_and_retry_succeeds(client, db):
    rid = _hold_id(client)
    r = confirm(client, rid, "FAILURE")
    assert r.status_code == 402
    err = r.json()["error"]
    assert err["code"] == "PAYMENT_FAILED" and err["details"]["retry_allowed"] is True
    assert err["details"]["reservation"]["status"] == "HELD"
    # The failed attempt was COMMITTED (not lost to a rollback).
    assert count(db, "SELECT COUNT(*) FROM payment_attempts WHERE status='FAILED'") == 1

    r = confirm(client, rid, "SUCCESS")
    assert r.status_code == 200
    assert r.json()["payment"]["attempt_number"] == 2
    detail = client.get(f"/api/v1/reservations/{rid}").json()
    assert [p["status"] for p in detail["payments"]] == ["FAILED", "SUCCEEDED"]
    assert_all_invariants(db)


def test_delayed_payment_then_successful_callback(client, db):
    rid = _hold_id(client)
    r = confirm(client, rid, "DELAYED")
    assert r.status_code == 202
    payment = r.json()["payment"]
    assert payment["status"] == "PENDING" and payment["completed_at"] is None
    assert r.json()["reservation"]["status"] == "HELD"

    # While pending, a second payment attempt is refused.
    again = confirm(client, rid, "SUCCESS")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "PAYMENT_ALREADY_PENDING"

    assert client.get(f"/api/v1/payments/{payment['id']}").json()["payment"]["status"] == "PENDING"
    cb = callback(client, payment["id"], "SUCCEEDED")
    assert cb.status_code == 200
    assert cb.json()["result"] == "CONFIRMED"
    assert cb.json()["reservation"]["status"] == "CONFIRMED"

    # A duplicate webhook delivery changes nothing.
    dup = callback(client, payment["id"], "SUCCEEDED")
    assert dup.status_code == 200 and dup.json()["result"] == "CALLBACK_ALREADY_PROCESSED"
    assert count(db, "SELECT COUNT(*) FROM payment_attempts WHERE status='SUCCEEDED'") == 1
    assert_all_invariants(db)


def test_delayed_payment_failed_callback_keeps_hold_and_allows_retry(client, db):
    rid = _hold_id(client)
    pid = confirm(client, rid, "DELAYED").json()["payment"]["id"]
    cb = callback(client, pid, "FAILED")
    assert cb.status_code == 200 and cb.json()["result"] == "PAYMENT_FAILED"
    assert cb.json()["reservation"]["status"] == "HELD"
    assert confirm(client, rid, "SUCCESS").status_code == 200
    assert_all_invariants(db)


def test_late_callback_after_expiry_is_voided_and_never_confirms(client, db):
    rid = _hold_id(client, 11, "A")
    pid = confirm(client, rid, "DELAYED").json()["payment"]["id"]
    backdate_expiry(db, rid)

    cb = callback(client, pid, "SUCCEEDED")
    assert cb.status_code == 409
    err = cb.json()["error"]
    assert err["code"] == "LATE_PAYMENT_VOIDED"
    assert err["details"]["payment"]["status"] == "VOIDED"
    assert err["details"]["reservation"]["status"] == "EXPIRED"
    assert client.get(f"/api/v1/payments/{pid}").json()["payment"]["status"] == "VOIDED"

    # The seat is free for someone else; a repeated late callback stays voided.
    assert hold(client, 11, "B").status_code == 201
    assert callback(client, pid, "SUCCEEDED").status_code == 409
    assert active_count(db, 11) == 1
    assert_all_invariants(db)


def test_late_callback_after_seat_reallocated_cannot_double_book(client, db):
    rid = _hold_id(client, 12, "A")
    pid = confirm(client, rid, "DELAYED").json()["payment"]["id"]
    backdate_expiry(db, rid)
    b = hold(client, 12, "B").json()["reservation"]["id"]  # lazy-expires A, voids A's payment
    assert confirm(client, b, "SUCCESS").status_code == 200

    cb = callback(client, pid, "SUCCEEDED")
    assert cb.status_code == 409 and cb.json()["error"]["code"] == "LATE_PAYMENT_VOIDED"
    assert client.get(f"/api/v1/reservations/{rid}").json()["status"] == "EXPIRED"
    assert active_count(db, 12) == 1
    assert_all_invariants(db)


def test_confirm_after_expiry_returns_409_and_frees_seat(client, db):
    rid = _hold_id(client, 13)
    backdate_expiry(db, rid)
    r = confirm(client, rid, "SUCCESS")
    assert r.status_code == 409 and r.json()["error"]["code"] == "RESERVATION_EXPIRED"
    assert count(db, "SELECT COUNT(*) FROM payment_attempts") == 0
    assert client.get(f"/api/v1/reservations/{rid}").json()["status"] == "EXPIRED"
    # EXPIRED is terminal.
    assert confirm(client, rid, "SUCCESS").json()["error"]["code"] == "RESERVATION_EXPIRED"
    assert_all_invariants(db)


def test_real_short_ttl_expiry(client, db, short_ttl):
    import time

    short_ttl(1)
    rid = _hold_id(client, 14)
    time.sleep(1.3)
    r = confirm(client, rid, "SUCCESS")
    assert r.status_code == 409 and r.json()["error"]["code"] == "RESERVATION_EXPIRED"
    assert_all_invariants(db)


def test_confirm_twice_never_double_charges(client, db):
    rid = _hold_id(client)
    assert confirm(client, rid, "SUCCESS").status_code == 200
    r = confirm(client, rid, "SUCCESS")
    assert r.status_code == 200 and r.json()["result"] == "ALREADY_CONFIRMED"
    assert count(db, "SELECT COUNT(*) FROM payment_attempts") == 1


def test_unknown_ids_return_404(client):
    missing = "00000000-0000-0000-0000-000000000000"
    assert confirm(client, missing).json()["error"]["code"] == "RESERVATION_NOT_FOUND"
    assert client.get(f"/api/v1/payments/{missing}").status_code == 404
    assert callback(client, missing).json()["error"]["code"] == "PAYMENT_NOT_FOUND"
    assert client.get(f"/api/v1/reservations/{missing}").status_code == 404
    assert (
        client.post(
            f"/api/v1/reservations/{missing}/confirm", json={"scenario": "MAYBE"}
        ).status_code
        == 422
    )
