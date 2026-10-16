from app.db.session import new_session
from app.jobs.reservation_expiry import expire_batch
from tests.helpers import assert_all_invariants, backdate_expiry, confirm, count, hold


def test_worker_expires_only_past_due_holds(client, db):
    stale = [hold(client, n).json()["reservation"]["id"] for n in (40, 41, 42)]
    fresh = hold(client, 43).json()["reservation"]["id"]
    confirmed = hold(client, 44).json()["reservation"]["id"]
    assert confirm(client, confirmed, "SUCCESS").status_code == 200
    pending = hold(client, 45).json()["reservation"]["id"]
    pid = confirm(client, pending, "DELAYED").json()["payment"]["id"]
    for rid in [*stale, pending]:
        backdate_expiry(db, rid)

    with new_session() as s:
        assert expire_batch(s, batch_size=100) == 4
    with new_session() as s:
        assert expire_batch(s, batch_size=100) == 0  # nothing left to do

    def status(rid):
        return client.get(f"/api/v1/reservations/{rid}").json()["status"]

    assert [status(r) for r in stale] == ["EXPIRED"] * 3
    assert status(fresh) == "HELD"
    assert status(confirmed) == "CONFIRMED"
    assert status(pending) == "EXPIRED"
    assert client.get(f"/api/v1/payments/{pid}").json()["payment"]["status"] == "VOIDED"
    assert_all_invariants(db)


def test_worker_respects_batch_size(client, db):
    ids = [hold(client, n).json()["reservation"]["id"] for n in range(50, 60)]
    for rid in ids:
        backdate_expiry(db, rid)
    with new_session() as s:
        assert expire_batch(s, batch_size=4) == 4
    assert count(db, "SELECT COUNT(*) FROM reservations WHERE status='HELD'") == 6
    with new_session() as s:
        assert expire_batch(s, batch_size=100) == 6


def test_worker_skips_seat_locked_by_an_api_transaction(client, db):
    from sqlalchemy import text

    rid = hold(client, 61).json()["reservation"]["id"]
    backdate_expiry(db, rid)
    with new_session() as locker:
        locker.execute(text("SELECT id FROM seats WHERE event_id=1 AND seat_number=61 FOR UPDATE"))
        with new_session() as s:
            assert expire_batch(s) == 0  # SKIP LOCKED: does not wait, does not deadlock
        locker.rollback()
    with new_session() as s:
        assert expire_batch(s) == 1
