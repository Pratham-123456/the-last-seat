# Load Test Report — The Last Seat

**Run date:** 2026-10-17 (local time) · **Status:** measured, not extrapolated.

## Environment

| Item | Value |
|---|---|
| Machine | Intel Core i7-14650HX (24 logical CPUs), 15.7 GB RAM, Windows 11 Home |
| Database | Local PostgreSQL 18.4, database `last_seat_db`, role `last_seat_app` |
| API | `uvicorn app.main:app --workers 4` (each worker: pool 10 + overflow 5, 15 request threads) |
| Expiry worker | `python -m app.cli worker` (2 s interval, batch 100) |
| Hold TTL | `HOLD_TTL_SECONDS=20` (short, so abandoned holds expire during the run) |
| Target event | **event 2** (500 seats), seeded specially for this run; the demo concert (event 1) was untouched |
| Load generator | Locust 2.32.5 on the **same laptop** (it competes with the API for CPU, so these numbers are conservative for the API) |

## Exact commands

```powershell
.\.venv\Scripts\python.exe -m app.cli seed --event-id 2 --name "Load test run 2026-10-17" --seats 500

$env:HOLD_TTL_SECONDS="20"; $env:DB_POOL_SIZE="10"; $env:DB_MAX_OVERFLOW="5"; $env:API_THREAD_LIMIT="15"; $env:EXPIRY_WORKER_INTERVAL_SECONDS="2"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 4 --log-level warning
.\.venv\Scripts\python.exe -m app.cli worker

$env:LOCUST_EVENT_ID="2"
.\.venv\Scripts\locust.exe -f locust/locustfile.py --headless -u 150 -r 25 -t 60s --host http://127.0.0.1:8000 --csv reports/raw/run1 --only-summary
```

## User mix

* `HotspotContentionUser` (weight 1, ~30 users): hammers **seats 1–5**. On a win it cancels immediately, so the seat is fought over again. This stresses `SELECT ... FOR UPDATE` contention *and* cancel → reallocation.
* `ConcertLifecycleUser` (weight 4, ~120 users): browse available seats → hold a random seat in 6–500 → 60% pay SUCCESS, 15% FAILURE then retry, 15% DELAYED + callback (90% SUCCEEDED / 10% FAILED), 10% abandon; 10% of confirmed bookings cancel; also checks reservation status and booking history.

`201` and `409` are both counted as correct outcomes; only `5xx`, connection errors or unexpected codes are failures.

## Results (Locust final summary)

| Metric | Value |
|---|---|
| Total requests | **26,524** |
| Failures | **0 (0.00%)** |
| Throughput | **447.2 requests/s** (aggregate) |
| Median (p50) | **9 ms** |
| p95 | **30 ms** |
| p99 | **74 ms** |
| Max | 228 ms |

Per endpoint (p50 / p95 / p99, ms):

| Endpoint | Requests | p50 | p95 | p99 |
|---|---|---|---|---|
| `POST /reservations [hotspot]` | 9,952 | 10 | 23 | 65 |
| `POST /reservations/{id}/cancel [hotspot]` | 7,005 | 9 | 20 | 46 |
| `GET /events/{id}/seats?available_only` | 6,455 | 6 | 18 | 52 |
| `GET /customers/{ref}/bookings` | 1,065 | 7 | 21 | 44 |
| `POST /reservations` (lifecycle) | 712 | 19 | 83 | 110 |
| `GET /reservations/{id}` | 551 | 12 | 53 | 73 |
| `POST /confirm [SUCCESS]` | 469 | 23 | 91 | 130 |
| `POST /confirm [FAILURE]` | 103 | 19 | 83 | 110 |
| `POST /confirm [DELAYED]` | 82 | 25 | 78 | 120 |
| `POST /payments/{id}/simulate-callback` | 82 | 23 | 74 | 150 |
| `POST /reservations/{id}/cancel` (lifecycle) | 48 | 19 | 51 | 59 |

(The CSV snapshot in `reports/raw/run1_stats.csv` was written slightly before the final summary and shows 26,514 requests / 448.1 RPS with the same percentiles.)

### HTTP status codes (every response, counted by a Locust event hook)

| Code | Count | Meaning |
|---|---|---|
| 200 | 15,675 | reads, confirmations, cancellations, callbacks |
| 201 | 7,612 | holds created |
| 202 | 82 | delayed payments accepted |
| 402 | 103 | simulated payment failures (hold kept) |
| 409 | 3,052 | lost seat races (expected contention) |
| **5xx** | **0** | — |

### Business outcomes

| Outcome | Count |
|---|---|
| Hotspot holds won / lost the race | 7,007 / 2,945 |
| Hotspot cancel → reallocate cycles | 7,005 |
| Lifecycle holds created | 605 |
| Lifecycle 409 (seat taken between browse and hold) | 107 |
| Confirmed by SUCCESS (incl. retries after FAILURE) | 469 |
| Confirmed by delayed callback | 74 |
| Delayed callbacks that FAILED (hold kept) | 8 |
| Confirmed bookings cancelled with refund | 48 |
| Abandoned holds (never paid) | 54 |
| "Sold out" browses (no free non-hot seat left) | 5,743 |

The 495 non-hot seats sold out during the run, which is why most later lifecycle iterations saw "sold out", as at a real 6 PM launch.

## Post-run database verification

`python -m app.cli invariants`, run after the test while the API was still up:

```json
{
  "violations": {
    "1_single_active_reservation_per_seat": 0,
    "2_capacity_bound": 0,
    "3_confirmed_has_exactly_one_succeeded_charge": 0,
    "4_no_confirmation_at_or_after_expiry": 0,
    "6_succeeded_charge_only_on_confirmed_or_cancelled": 0,
    "6b_cancelled_after_charge_has_refund": 0,
    "7_no_charge_succeeded_after_expiry": 0,
    "9_no_pending_payment_on_inactive_reservation": 0,
    "9b_terminal_payment_has_completed_at": 0
  },
  "counts": {
    "reservations": {"CANCELLED": 7055, "CONFIRMED": 495, "EXPIRED": 62},
    "payments": {"CHARGE:FAILED": 111, "CHARGE:SUCCEEDED": 543, "REFUND:SUCCEEDED": 48},
    "idempotency_records": 18455
  }
}
```

Cross-checks that hold:
* `GET /api/v1/events/2` afterwards: `confirmed 495, held 0, available 5`. The 5 available seats are the hotspot seats, released by their final cancels; 495 + 5 = 500.
* 543 successful charges = 495 confirmed + 48 cancelled-and-refunded; every cancellation of a paid booking has its refund.
* 18,455 idempotency records ≈ one per mutating request, as every Locust POST sent a fresh key.

## Expiry worker observation (honest finding)

During the load run, all 62 expired holds were rewritten **6–60 ms** after their `expires_at` — faster than the worker's 2 s cycle. Because the event was sold out, students kept requesting any seat that looked free, and each such request **lazily expired** the stale hold under the seat lock. So the worker had nothing left to do; its log shows no expiries during the run.

To show the worker doing its job when no request touches the seats, a second check held 10 seats of a quiet event (event 3), then waited. **All 10 were rewritten to EXPIRED by the worker within one cycle** (worker log: `expired 10 hold(s), voided 0 pending payment(s)`), and the event summary returned to 50/50 available.

## Limitations of this test

* One machine for both load generator and system under test; Locust's own CPU use caps the observable throughput.
* 150 users for 60 s is a bounded local run, **not** the 10,000-student scenario. See [docs/scaling.md](../docs/scaling.md) for running distributed Locust (`--master` / `--worker`) on dedicated hardware, and the PgBouncer and waiting-room changes needed before such numbers mean anything.
* Network latency is loopback (~0). Real clients add RTT; server-side times would be unchanged.

Raw artifacts: `reports/raw/run1_stats.csv`, `run1_stats_history.csv`, `run1_failures.csv` (empty), `run1_console.txt`, `locust_outcomes.json`, `run1_invariants.json`.
