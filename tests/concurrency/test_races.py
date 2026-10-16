"""Tests 3-5: idempotency, confirmation/callback and expiry-worker races."""

import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.db.session import new_session
from app.jobs.reservation_expiry import expire_batch
from tests.helpers import active_count, assert_all_invariants, backdate_expiry, count

pytestmark = pytest.mark.concurrency


def _fire_all(base_url: str, calls: list[tuple[str, str, dict | None, dict]]):
    """calls = [(method, path, json, headers)] all released by one barrier."""
    barrier = threading.Barrier(len(calls))

    def run(call):
        method, path, body, headers = call
        with httpx.Client(base_url=base_url, timeout=60) as c:
            barrier.wait()
            return c.request(method, path, json=body, headers=headers)

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def _hold(base_url, seat, customer="OWNER"):
    with httpx.Client(base_url=base_url) as c:
        r = c.post(
            "/api/v1/reservations",
            json={"event_id": 1, "seat_number": seat, "customer_ref": customer},
        )
        assert r.status_code == 201, r.text
        return r.json()["reservation"]["id"]


def test_10_identical_requests_same_idempotency_key(live_server, db):
    body = {"event_id": 1, "seat_number": 200, "customer_ref": "22BCE0200"}
    responses = _fire_all(
        live_server,
        [("POST", "/api/v1/reservations", body, {"Idempotency-Key": "double-click-key"})] * 10,
    )
    assert [r.status_code for r in responses] == [201] * 10
    ids = {r.json()["reservation"]["id"] for r in responses}
    assert len(ids) == 1
    assert sum(r.headers.get("Idempotent-Replayed") == "true" for r in responses) == 9
    assert count(db, "SELECT COUNT(*) FROM reservations") == 1
    assert count(db, "SELECT COUNT(*) FROM idempotency_records") == 1


def test_10_concurrent_success_confirmations_charge_once(live_server, db):
    rid = _hold(live_server, 201)
    calls = [
        (
            "POST",
            f"/api/v1/reservations/{rid}/confirm",
            {"scenario": "SUCCESS"},
            {"Idempotency-Key": str(uuid.uuid4())},
        )
        for _ in range(10)
    ]
    responses = _fire_all(live_server, calls)
    results = Counter(r.json()["result"] for r in responses)
    assert all(r.status_code == 200 for r in responses)
    assert results == {"CONFIRMED": 1, "ALREADY_CONFIRMED": 9}
    assert (
        count(
            db,
            "SELECT COUNT(*) FROM payment_attempts WHERE payment_type='CHARGE' "
            "AND status='SUCCEEDED'",
        )
        == 1
    )
    assert count(db, "SELECT COUNT(*) FROM payment_attempts") == 1
    assert_all_invariants(db)


def test_10_concurrent_mixed_scenarios_at_most_one_success(live_server, db):
    rid = _hold(live_server, 202)
    scenarios = ["SUCCESS", "FAILURE", "DELAYED"] * 3 + ["SUCCESS"]
    calls = [
        ("POST", f"/api/v1/reservations/{rid}/confirm", {"scenario": s}, {}) for s in scenarios
    ]
    responses = _fire_all(live_server, calls)
    assert not [r for r in responses if r.status_code >= 500]
    assert (
        count(
            db,
            "SELECT COUNT(*) FROM payment_attempts WHERE payment_type='CHARGE' "
            "AND status='SUCCEEDED'",
        )
        <= 1
    )
    assert count(db, "SELECT COUNT(*) FROM payment_attempts WHERE status='PENDING'") <= 1
    assert_all_invariants(db)


def test_10_concurrent_callbacks_for_one_delayed_payment(live_server, db):
    rid = _hold(live_server, 203)
    with httpx.Client(base_url=live_server) as c:
        pid = c.post(f"/api/v1/reservations/{rid}/confirm", json={"scenario": "DELAYED"}).json()[
            "payment"
        ]["id"]
    calls = [
        ("POST", f"/api/v1/payments/{pid}/simulate-callback", {"outcome": "SUCCEEDED"}, {})
        for _ in range(10)
    ]
    responses = _fire_all(live_server, calls)
    results = Counter(r.json()["result"] for r in responses)
    assert results == {"CONFIRMED": 1, "CALLBACK_ALREADY_PROCESSED": 9}
    assert count(db, "SELECT COUNT(*) FROM payment_attempts WHERE status='SUCCEEDED'") == 1
    assert_all_invariants(db)


def test_confirm_vs_cancel_race_leaves_consistent_state(live_server, db):
    seats = range(210, 220)
    rids = [_hold(live_server, s) for s in seats]
    calls = []
    for rid in rids:
        calls += [("POST", f"/api/v1/reservations/{rid}/confirm", {"scenario": "SUCCESS"}, {})] * 3
        calls += [("POST", f"/api/v1/reservations/{rid}/cancel", None, {})] * 3
    responses = _fire_all(live_server, calls)
    assert not [r for r in responses if r.status_code >= 500]
    # Whoever won, a cancelled-after-confirm booking must carry a refund (checked by 6b).
    assert_all_invariants(db)
    assert count(db, "SELECT COUNT(*) FROM reservations WHERE status IN ('HELD','CONFIRMED')") == 0


def test_expiry_worker_vs_reallocation_and_confirmation(live_server, db):
    """Test 5: abandoned holds are being expired by 3 worker threads while new students
    grab the same seats and the original owners try (too late) to pay."""
    seats = list(range(300, 340))
    original = {s: _hold(live_server, s, f"OLD{s}") for s in seats}
    for rid in original.values():
        backdate_expiry(db, rid, seconds_ago=0.01)

    stop = threading.Event()
    worker_errors: list[BaseException] = []

    def worker_loop():
        while not stop.is_set():
            try:
                with new_session() as s:
                    expire_batch(s, batch_size=7)
            except BaseException as exc:  # noqa: BLE001
                worker_errors.append(exc)
            time.sleep(0.005)

    workers = [threading.Thread(target=worker_loop) for _ in range(3)]
    for w in workers:
        w.start()
    try:
        calls = []
        for s in seats:
            body = {"event_id": 1, "seat_number": s, "customer_ref": f"NEW{s}"}
            calls += [("POST", "/api/v1/reservations", body, {})] * 2
            calls.append(
                ("POST", f"/api/v1/reservations/{original[s]}/confirm", {"scenario": "SUCCESS"}, {})
            )
        responses = _fire_all(live_server, calls)
    finally:
        stop.set()
        for w in workers:
            w.join()

    assert not worker_errors, worker_errors
    codes = Counter(r.status_code for r in responses)
    print("\nexpiry race status codes:", dict(codes))
    assert not [r for r in responses if r.status_code >= 500]
    confirm_codes = [
        r.json()["error"]["code"] for r in responses if r.request.url.path.endswith("/confirm")
    ]
    assert confirm_codes == ["RESERVATION_EXPIRED"] * len(seats)
    assert codes[201] == len(seats)  # exactly one new student per seat
    for s in seats:
        assert active_count(db, s) == 1
    assert count(db, "SELECT COUNT(*) FROM reservations WHERE status='CONFIRMED'") == 0
    assert_all_invariants(db)


def test_boundary_expiry_with_real_clock(live_server, db, short_ttl):
    """Holds expire *during* the burst: confirmations and re-holds race the real clock."""
    short_ttl(1)
    seats = list(range(400, 430))
    original = {s: _hold(live_server, s, f"OLD{s}") for s in seats}
    time.sleep(0.95)  # land right on the 1-second boundary
    calls = []
    for s in seats:
        calls.append(
            ("POST", f"/api/v1/reservations/{original[s]}/confirm", {"scenario": "SUCCESS"}, {})
        )
        calls.append(
            (
                "POST",
                "/api/v1/reservations",
                {"event_id": 1, "seat_number": s, "customer_ref": f"NEW{s}"},
                {},
            )
        )
    responses = _fire_all(live_server, calls)
    with new_session() as s:
        expire_batch(s)
    assert not [r for r in responses if r.status_code >= 500]
    print(
        "\nboundary outcomes:",
        dict(Counter(r.json().get("result") or r.json()["error"]["code"] for r in responses)),
    )
    for s in seats:
        assert active_count(db, s) <= 1
    assert_all_invariants(db)
