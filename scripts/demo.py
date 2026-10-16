"""Narrated live demo against a running API (see scripts/demo.ps1).

    python scripts/demo.py --seat 77 [--event-id 1] [--base http://127.0.0.1:8000]

Uses seats N, N+1, N+2 of the event. Makes only normal API calls.
"""

import argparse
import sys
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx


def step(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--event-id", type=int, default=1)
    ap.add_argument("--seat", type=int, default=77)
    ap.add_argument("--students", type=int, default=100)
    ap.add_argument("--late", action="store_true", help="also demo a late callback (short TTL)")
    a = ap.parse_args()
    ev, s1, s2, s3 = a.event_id, a.seat, a.seat + 1, a.seat + 2
    c = httpx.Client(base_url=a.base, timeout=60)

    def hold(seat, who, key=None):
        h = {"Idempotency-Key": key} if key else {}
        return c.post(
            "/api/v1/reservations",
            headers=h,
            json={"event_id": ev, "seat_number": seat, "customer_ref": who},
        )

    for s in (s1, s2, s3):
        st = c.get(f"/api/v1/events/{ev}/seats/{s}").json()
        if st.get("status") != "AVAILABLE":
            print(f"Seat {s} is {st.get('status')}; rerun with a free --seat.")
            return 1

    step(f"1. {a.students} students click seat {s1} at the same instant")
    barrier = threading.Barrier(a.students)

    def student(i):
        with httpx.Client(base_url=a.base, timeout=60) as cc:
            barrier.wait()
            return cc.post(
                "/api/v1/reservations",
                json={"event_id": ev, "seat_number": s1, "customer_ref": f"STU{i:03d}"},
            )

    with ThreadPoolExecutor(a.students) as pool:
        rs = list(pool.map(student, range(a.students)))
    print("status codes:", dict(Counter(r.status_code for r in rs)))
    win = next(r for r in rs if r.status_code == 201).json()["reservation"]
    print(f"winner: {win['customer_ref']}  reservation={win['id']}  expires_at={win['expires_at']}")
    print("seat now:", c.get(f"/api/v1/events/{ev}/seats/{s1}").json()["status"])

    step("2. Winner's payment FAILS, hold survives, retry SUCCEEDS")
    r = c.post(f"/api/v1/reservations/{win['id']}/confirm", json={"scenario": "FAILURE"})
    print(
        r.status_code,
        r.json()["error"]["code"],
        "-> reservation still",
        r.json()["error"]["details"]["reservation"]["status"],
    )
    r = c.post(f"/api/v1/reservations/{win['id']}/confirm", json={"scenario": "SUCCESS"})
    print(r.status_code, r.json()["result"], "attempt", r.json()["payment"]["attempt_number"])

    step(f"3. DELAYED payment on seat {s2}, callback arrives in time")
    rid = hold(s2, "DEMO_DELAYED").json()["reservation"]["id"]
    r = c.post(f"/api/v1/reservations/{rid}/confirm", json={"scenario": "DELAYED"})
    pid = r.json()["payment"]["id"]
    print(r.status_code, r.json()["result"], "payment", r.json()["payment"]["status"])
    r = c.post(f"/api/v1/payments/{pid}/simulate-callback", json={"outcome": "SUCCEEDED"})
    print("callback ->", r.status_code, r.json()["result"])

    step(f"4. Cancel the booking on seat {s2}: refund recorded, seat reallocated")
    r = c.post(f"/api/v1/reservations/{rid}/cancel")
    print(r.status_code, r.json()["result"], "refund:", r.json()["payment"]["status"])
    r = hold(s2, "NEXT_STUDENT")
    print("next student holds it:", r.status_code, r.json()["result"])

    step(f"5. Idempotent retry on seat {s3}: same key twice = one reservation")
    key = f"demo-{uuid.uuid4()}"
    r1, r2 = hold(s3, "DOUBLE_CLICK", key), hold(s3, "DOUBLE_CLICK", key)
    print(
        r1.status_code,
        r2.status_code,
        "same id:",
        r1.json()["reservation"]["id"] == r2.json()["reservation"]["id"],
        "replayed header:",
        r2.headers.get("Idempotent-Replayed"),
    )
    r3 = hold(s3 + 1, "DOUBLE_CLICK", key)
    print("same key, different seat ->", r3.status_code, r3.json()["error"]["code"])

    step(f"6. Late callback: DELAYED payment on seat {s3 + 2}, bank answers after expiry")
    if not a.late:
        print("skipped (run the API with HOLD_TTL_SECONDS=20 and pass --late to watch it live)")
    else:
        r = hold(s3 + 2, "SLOW_BANK").json()["reservation"]
        if r["seconds_remaining"] > 90:
            print(
                f"server TTL is {r['seconds_remaining']:.0f}s; restart the API with "
                "HOLD_TTL_SECONDS=20 to keep this step short"
            )
        else:
            pid = c.post(
                f"/api/v1/reservations/{r['id']}/confirm", json={"scenario": "DELAYED"}
            ).json()["payment"]["id"]
            wait = r["seconds_remaining"] + 1
            print(f"payment PENDING; waiting {wait:.0f}s for the hold to expire...")
            time.sleep(wait)
            cb = c.post(f"/api/v1/payments/{pid}/simulate-callback", json={"outcome": "SUCCEEDED"})
            err = cb.json()["error"]
            print(
                "late callback ->",
                cb.status_code,
                err["code"],
                "| payment:",
                err["details"]["payment"]["status"],
                "| reservation:",
                err["details"]["reservation"]["status"],
            )
    print("\nDemo finished.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
