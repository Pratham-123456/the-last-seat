"""Locust load test for The Last Seat.

Two behaviours:
* HotspotContentionUser - hammers a handful of "hot" seats to stress SELECT ... FOR UPDATE.
  201 (won) and 409 SEAT_UNAVAILABLE (lost the race) are BOTH correct outcomes and are
  counted as successes; only 5xx / unexpected codes count as failures.
* ConcertLifecycleUser  - a realistic student: browse -> hold -> pay (SUCCESS / FAILURE+retry /
  DELAYED+callback) -> sometimes cancel or abandon -> check booking history.

Run (headless, from the project root):
    locust -f locust/locustfile.py --headless -u 150 -r 25 -t 60s --host http://127.0.0.1:8000

Environment knobs: LOCUST_EVENT_ID (default 2), LOCUST_HOT_SEATS (default 5).
"""

import json
import os
import random
import threading
import uuid
from collections import Counter
from pathlib import Path

from locust import HttpUser, between, events, task

EVENT_ID = int(os.environ.get("LOCUST_EVENT_ID", "2"))
HOT_SEATS = int(os.environ.get("LOCUST_HOT_SEATS", "5"))
OUT_DIR = Path(__file__).resolve().parents[1] / "reports" / "raw"

_lock = threading.Lock()
status_codes: Counter = Counter()  # every HTTP response, by status code
outcomes: Counter = Counter()  # business outcomes, e.g. "hot:won", "life:CONFIRMED"
server_errors: list[str] = []


def record(name: str) -> None:
    with _lock:
        outcomes[name] += 1


@events.request.add_listener
def _count_status(request_type, name, response, exception, **_kw):
    with _lock:
        status_codes[getattr(response, "status_code", 0) or 0] += 1
        code = getattr(response, "status_code", 0) or 0
        if code >= 500 or code == 0:
            server_errors.append(f"{request_type} {name} -> {code} {exception or ''}"[:300])


@events.test_stop.add_listener
def _dump(environment, **_kw):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "event_id": EVENT_ID,
        "hot_seats": HOT_SEATS,
        "status_codes": {str(k): v for k, v in sorted(status_codes.items())},
        "outcomes": dict(sorted(outcomes.items())),
        "server_errors_sample": server_errors[:20],
        "server_error_count": len(server_errors),
    }
    (OUT_DIR / "locust_outcomes.json").write_text(json.dumps(data, indent=2))
    print("\n=== The Last Seat: outcome counters ===")
    print(json.dumps(data, indent=2))


def _key() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _expect(response, ok_codes: set[int], label: str) -> bool:
    if response.status_code in ok_codes:
        response.success()
        return True
    response.failure(f"{label}: unexpected {response.status_code} {response.text[:200]}")
    return False


class HotspotContentionUser(HttpUser):
    weight = 1
    wait_time = between(0.05, 0.25)

    @task
    def grab_hot_seat(self):
        seat = random.randint(1, HOT_SEATS)
        body = {
            "event_id": EVENT_ID,
            "seat_number": seat,
            "customer_ref": f"HOT{uuid.uuid4().hex[:8]}",
        }
        with self.client.post(
            "/api/v1/reservations",
            json=body,
            headers=_key(),
            name="POST /reservations [hotspot]",
            catch_response=True,
        ) as r:
            if not _expect(r, {201, 409}, "hotspot hold"):
                return
            if r.status_code == 409:
                record("hot:lost_race_409")
                return
            record("hot:won_201")
            rid = r.json()["reservation"]["id"]
        # Release it straight away so the seat is fought over again (reallocation under load).
        with self.client.post(
            f"/api/v1/reservations/{rid}/cancel",
            headers=_key(),
            name="POST /reservations/{id}/cancel [hotspot]",
            catch_response=True,
        ) as c:
            if _expect(c, {200, 409}, "hotspot cancel"):
                record("hot:cancelled")


class ConcertLifecycleUser(HttpUser):
    weight = 4
    wait_time = between(0.3, 1.5)

    def on_start(self):
        self.customer = f"22BCE{random.randint(1000, 9999)}{uuid.uuid4().hex[:4]}"

    @task(1)
    def view_history(self):
        with self.client.get(
            f"/api/v1/customers/{self.customer}/bookings",
            name="GET /customers/{ref}/bookings",
            catch_response=True,
        ) as r:
            _expect(r, {200}, "history")

    @task(6)
    def book_a_seat(self):
        with self.client.get(
            f"/api/v1/events/{EVENT_ID}/seats?available_only=true",
            name="GET /events/{id}/seats?available_only",
            catch_response=True,
        ) as r:
            if not _expect(r, {200}, "browse"):
                return
            free = [s["seat_number"] for s in r.json()["seats"] if s["seat_number"] > HOT_SEATS]
        if not free:
            record("life:sold_out")
            return
        seat = random.choice(free)
        body = {"event_id": EVENT_ID, "seat_number": seat, "customer_ref": self.customer}
        with self.client.post(
            "/api/v1/reservations",
            json=body,
            headers=_key(),
            name="POST /reservations",
            catch_response=True,
        ) as r:
            if not _expect(r, {201, 409}, "hold"):
                return
            if r.status_code == 409:
                record("life:seat_taken_between_browse_and_hold_409")
                return
            rid = r.json()["reservation"]["id"]
            record("life:held")

        roll = random.random()
        if roll < 0.10:
            record("life:abandoned")  # never pays; expiry worker reclaims it
            return
        if roll < 0.70:
            confirmed = self._pay(rid, "SUCCESS")
        elif roll < 0.85:
            self._pay(rid, "FAILURE")  # 402, hold kept
            confirmed = self._pay(rid, "SUCCESS")  # retry
        else:
            confirmed = self._delayed(rid)

        if confirmed and random.random() < 0.10:
            with self.client.post(
                f"/api/v1/reservations/{rid}/cancel",
                headers=_key(),
                name="POST /reservations/{id}/cancel",
                catch_response=True,
            ) as c:
                if _expect(c, {200}, "cancel"):
                    record("life:cancelled_with_refund")

        with self.client.get(
            f"/api/v1/reservations/{rid}", name="GET /reservations/{id}", catch_response=True
        ) as g:
            _expect(g, {200}, "status")

    def _pay(self, rid: str, scenario: str) -> bool:
        with self.client.post(
            f"/api/v1/reservations/{rid}/confirm",
            json={"scenario": scenario},
            headers=_key(),
            name=f"POST /reservations/{{id}}/confirm [{scenario}]",
            catch_response=True,
        ) as r:
            if not _expect(r, {200, 402, 409}, f"confirm {scenario}"):
                return False
            if r.status_code == 200:
                record("life:CONFIRMED")
                return True
            record(f"life:confirm_{r.status_code}_{r.json()['error']['code']}")
            return False

    def _delayed(self, rid: str) -> bool:
        with self.client.post(
            f"/api/v1/reservations/{rid}/confirm",
            json={"scenario": "DELAYED"},
            headers=_key(),
            name="POST /reservations/{id}/confirm [DELAYED]",
            catch_response=True,
        ) as r:
            if not _expect(r, {202, 409}, "confirm DELAYED") or r.status_code != 202:
                return False
            pid = r.json()["payment"]["id"]
        outcome = "SUCCEEDED" if random.random() < 0.9 else "FAILED"
        with self.client.post(
            f"/api/v1/payments/{pid}/simulate-callback",
            json={"outcome": outcome},
            headers=_key(),
            name="POST /payments/{id}/simulate-callback",
            catch_response=True,
        ) as cb:
            if not _expect(cb, {200, 409}, "callback"):
                return False
            result = cb.json().get("result") or cb.json()["error"]["code"]
            record(f"life:callback_{result}")
            return result == "CONFIRMED"
