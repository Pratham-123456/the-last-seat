from tests.helpers import (
    active_count,
    assert_all_invariants,
    backdate_expiry,
    callback,
    cancel,
    confirm,
    count,
    hold,
)


def test_cancel_held_frees_seat_for_immediate_reallocation(client, db):
    rid = hold(client, 20, "A").json()["reservation"]["id"]
    r = cancel(client, rid)
    assert r.status_code == 200 and r.json()["result"] == "CANCELLED"
    assert r.json()["reservation"]["cancelled_at"] is not None
    assert client.get("/api/v1/events/1/seats/20").json()["status"] == "AVAILABLE"

    assert hold(client, 20, "B").status_code == 201  # reallocation
    assert active_count(db, 20) == 1
    # History preserved: both rows remain.
    assert (
        count(
            db,
            "SELECT COUNT(*) FROM reservations r JOIN seats s ON s.id=r.seat_id "
            "WHERE s.seat_number = 20",
        )
        == 2
    )
    assert_all_invariants(db)


def test_cancel_confirmed_records_refund_and_reallocates(client, db):
    rid = hold(client, 21, "A").json()["reservation"]["id"]
    charge = confirm(client, rid, "SUCCESS").json()["payment"]
    r = cancel(client, rid)
    assert r.status_code == 200
    refund = r.json()["payment"]
    assert refund["payment_type"] == "REFUND" and refund["status"] == "SUCCEEDED"
    assert refund["related_payment_id"] == charge["id"]

    b = hold(client, 21, "B")
    assert b.status_code == 201
    assert confirm(client, b.json()["reservation"]["id"], "SUCCESS").status_code == 200
    assert active_count(db, 21) == 1
    assert_all_invariants(db)


def test_cancel_is_idempotent_and_cancelled_is_terminal(client, db):
    rid = hold(client, 22).json()["reservation"]["id"]
    assert cancel(client, rid).status_code == 200
    again = cancel(client, rid)
    assert again.status_code == 200 and again.json()["result"] == "ALREADY_CANCELLED"
    r = confirm(client, rid, "SUCCESS")
    assert r.status_code == 409 and r.json()["error"]["code"] == "RESERVATION_NOT_ACTIVE"
    assert count(db, "SELECT COUNT(*) FROM payment_attempts") == 0


def test_cancel_expired_hold_returns_409(client, db):
    rid = hold(client, 23).json()["reservation"]["id"]
    backdate_expiry(db, rid)
    r = cancel(client, rid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "RESERVATION_ALREADY_EXPIRED"
    assert client.get(f"/api/v1/reservations/{rid}").json()["status"] == "EXPIRED"


def test_cancel_with_pending_payment_voids_it_and_late_callback_is_rejected(client, db):
    rid = hold(client, 24).json()["reservation"]["id"]
    pid = confirm(client, rid, "DELAYED").json()["payment"]["id"]
    assert cancel(client, rid).status_code == 200
    assert client.get(f"/api/v1/payments/{pid}").json()["payment"]["status"] == "VOIDED"
    cb = callback(client, pid, "SUCCEEDED")
    assert cb.status_code == 409 and cb.json()["error"]["code"] == "LATE_PAYMENT_VOIDED"
    assert client.get(f"/api/v1/reservations/{rid}").json()["status"] == "CANCELLED"
    assert_all_invariants(db)
