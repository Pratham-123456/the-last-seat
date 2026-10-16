"""Test 1 + Test 2: many students, real HTTP, real PostgreSQL, fired at the same instant."""

import threading
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tests.helpers import active_count, assert_all_invariants, count

pytestmark = pytest.mark.concurrency


def _fire(base_url: str, payloads: list[dict]) -> list[httpx.Response]:
    barrier = threading.Barrier(len(payloads))

    def worker(payload):
        with httpx.Client(base_url=base_url, timeout=60) as c:
            barrier.wait()  # every thread releases at the same moment
            return c.post(
                "/api/v1/reservations", json=payload, headers={"Idempotency-Key": str(uuid.uuid4())}
            )

    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        return list(pool.map(worker, payloads))


def test_100_students_one_seat_exactly_one_wins(live_server, db):
    payloads = [
        {"event_id": 1, "seat_number": 1, "customer_ref": f"STUDENT{i:03d}"} for i in range(100)
    ]
    responses = _fire(live_server, payloads)

    statuses = Counter(r.status_code for r in responses)
    print("\nstampede status codes:", dict(statuses))
    assert statuses[201] == 1
    assert statuses[409] == 99
    assert not [r for r in responses if r.status_code >= 500]
    assert all(
        r.json()["error"]["code"] == "SEAT_UNAVAILABLE" for r in responses if r.status_code == 409
    )

    # Ask PostgreSQL directly, not the API.
    assert active_count(db, 1) == 1
    winner = next(r for r in responses if r.status_code == 201).json()
    assert count(db, "SELECT COUNT(*) FROM reservations") == 1
    assert winner["reservation"]["seat_number"] == 1
    assert_all_invariants(db)


def test_multi_seat_concurrent_booking(live_server, db):
    seats = list(range(10, 30))  # 20 distinct seats, 10 students each = 200 requests
    payloads = [
        {"event_id": 1, "seat_number": s, "customer_ref": f"S{s}-{i}"}
        for s in seats
        for i in range(10)
    ]
    responses = _fire(live_server, payloads)

    statuses = Counter(r.status_code for r in responses)
    print("\nmulti-seat status codes:", dict(statuses))
    assert statuses[201] == len(seats)
    assert statuses[409] == len(payloads) - len(seats)
    assert sum(statuses.values()) == len(payloads)

    won = Counter(r.json()["reservation"]["seat_number"] for r in responses if r.status_code == 201)
    assert set(won) == set(seats) and all(v == 1 for v in won.values())
    for s in seats:
        assert active_count(db, s) == 1
    assert count(db, "SELECT COUNT(*) FROM reservations WHERE status = 'HELD'") == len(seats)
    assert_all_invariants(db)
