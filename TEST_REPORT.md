# Test Report

All results below were produced by actually running the commands on 2026-10-17 against the local PostgreSQL 18.4 server (database `last_seat_test_db` for pytest, `last_seat_db` for Locust).

## Final full run

```powershell
.\.venv\Scripts\ruff.exe check .            # All checks passed!
.\.venv\Scripts\ruff.exe format --check .   # 53 files already formatted
.\.venv\Scripts\python.exe -m pytest -v -s
```

Result: **73 passed in 65.31 s, 0 failed, 0 errors, 0 skipped.** Full per-test log: [`reports/raw/pytest_full_run.txt`](reports/raw/pytest_full_run.txt).

| Suite | Tests | Result |
|---|---|---|
| `tests/unit` (no DB) | 24 | 24 passed |
| `tests/integration` (real PostgreSQL) | 40 | 40 passed |
| `tests/concurrency` (real HTTP server + threads + PostgreSQL) | 9 | 9 passed |

## Concurrency suite: what was observed

| Test | Load | Observed |
|---|---|---|
| `test_100_students_one_seat_exactly_one_wins` | 100 threads, 1 barrier, seat 1 | `{201: 1, 409: 99}`; 0 × 5xx; DB: exactly 1 active row; all invariants 0 |
| `test_multi_seat_concurrent_booking` | 200 threads, 20 seats × 10 | `{201: 20, 409: 180}`; one winner per seat |
| `test_10_identical_requests_same_idempotency_key` | 10 threads, same key + body | 10 × 201, one `reservation_id`, 9 replayed, 1 row, 1 idempotency record |
| `test_10_concurrent_success_confirmations_charge_once` | 10 × SUCCESS on one hold | 1 × CONFIRMED + 9 × ALREADY_CONFIRMED; exactly 1 payment row |
| `test_10_concurrent_mixed_scenarios_at_most_one_success` | SUCCESS/FAILURE/DELAYED mix | ≤ 1 SUCCEEDED charge, ≤ 1 PENDING, 0 × 5xx |
| `test_10_concurrent_callbacks_for_one_delayed_payment` | 10 × callback SUCCEEDED | 1 × CONFIRMED + 9 × CALLBACK_ALREADY_PROCESSED |
| `test_confirm_vs_cancel_race_leaves_consistent_state` | 10 holds × (3 confirm + 3 cancel) | 0 × 5xx; all end CANCELLED; every paid one refunded |
| `test_expiry_worker_vs_reallocation_and_confirmation` | 3 worker threads + 120 requests on 40 stale holds | `{201: 40, 409: 80}`; all 40 late confirms → RESERVATION_EXPIRED; 1 active per seat; 0 worker errors |
| `test_boundary_expiry_with_real_clock` | 1 s TTL, 60 requests at the boundary | `{RESERVATION_EXPIRED: 30, HELD: 30}` this run; ≤ 1 active per seat; invariants 0 |

## Integration coverage (40 tests)

* **Reservations:** hold creates server-side expiry; second student gets 409; 404 seat/event; 422 envelope; lazy expiry without worker; listing + summary counts.
* **Payments:** happy path → history; FAILURE recorded (committed) + retry; DELAYED → PENDING → 409 PAYMENT_ALREADY_PENDING → callback CONFIRMED → duplicate callback no-op; DELAYED + FAILED callback → retry; late callback after expiry → VOIDED + EXPIRED + 409; late callback after seat reallocated cannot double-book; confirm after expiry → 409 + seat freed + EXPIRED terminal; real 1 s TTL expiry; confirm twice → one charge; unknown ids → 404/422.
* **Cancellation:** HELD cancel frees seat → reallocation (history kept); CONFIRMED cancel → REFUND linked to charge → reallocation; cancel twice idempotent and CANCELLED terminal; cancel expired → 409; cancel with pending payment voids it, late callback rejected.
* **Idempotency:** replay identical (header `Idempotent-Replayed`), payload mismatch 409, no key = new request, FAILURE replay creates no second attempt, per-endpoint scoping, key reused on a different reservation is a mismatch, invalid key 422.
* **Expiry worker:** expires only past-due HELD (ignores fresh HELD and CONFIRMED, voids PENDING); batch size respected; `SKIP LOCKED` skips a seat locked by another transaction instead of waiting.
* **Atomicity / restart / schema:** crash between payment insert and confirmation rolls back everything (including the idempotency record), and the retry then succeeds; engine dispose + new app instance loses no state and replays; all constraints/indexes exist and `seats` has no availability column; the partial unique index rejects a second active row inserted with raw SQL; CHECK constraints reject invalid states; seeding is idempotent.
* **History / health:** booking history newest-first across all statuses; reservation status; `/health/live`, `/health/ready` (reports `last_seat_test_db`), OpenAPI paths, `/docs`.

## Load test

See [`reports/load_test_report.md`](reports/load_test_report.md): 26,524 requests, 447 RPS, p50 9 ms / p95 30 ms / p99 74 ms, 0 failures, 0 × 5xx, all invariant checks 0 afterwards.

## Manual / demo verification

`scripts/demo.ps1 -EventId 3 -Seat 20 -Late` against a freshly started local API (TTL 20 s) printed:

```
=== 1. 100 students click seat 20 at the same instant ===
status codes: {409: 99, 201: 1}
=== 2. Winner's payment FAILS, hold survives, retry SUCCEEDS ===
402 PAYMENT_FAILED -> reservation still HELD
200 CONFIRMED attempt 2
=== 3. DELAYED payment on seat 21, callback arrives in time ===
202 PAYMENT_PENDING payment PENDING
callback -> 200 CONFIRMED
=== 4. Cancel the booking on seat 21: refund recorded, seat reallocated ===
200 CANCELLED refund: SUCCEEDED
next student holds it: 201 HELD
=== 5. Idempotent retry on seat 22: same key twice = one reservation ===
201 201 same id: True replayed header: true
same key, different seat -> 409 IDEMPOTENCY_KEY_PAYLOAD_MISMATCH
=== 6. Late callback: DELAYED payment on seat 24, bank answers after expiry ===
payment PENDING; waiting 21s for the hold to expire...
late callback -> 409 LATE_PAYMENT_VOIDED | payment: VOIDED | reservation: EXPIRED
```

## Docker Compose (Workflow B)

Run on 2026-10-17 with Docker Desktop 29.7.2, alongside the local PostgreSQL on 5432, which was not affected.

```powershell
docker compose up --build -d
```

| Check | Result |
|---|---|
| Image build (python:3.12-slim, non-root) | ✓ built; `docker compose exec api id` → `uid=10001(appuser)` |
| `db` (postgres:18-alpine, host port 5433, volume `the-last-seat_last_seat_pgdata`) | ✓ healthy |
| `migrate` | ✓ waited for DB, ran `0001_initial_schema`, seeded `{"event_id": 1, "seats_inserted": 500}`, exited 0 |
| `api` | ✓ healthy; `/health/ready` → `{"status":"ok","database":"last_seat_db"}` |
| `worker` | ✓ running; log shows `expired 18 hold(s)`, `expired 4 hold(s)`, … |
| Demo (`scripts/demo.py --event-id 2 --seat 10 --late`, TTL 20 s) | ✓ stampede `{201: 1, 409: 99}`; FAILURE → retry CONFIRMED; DELAYED → callback CONFIRMED; cancel → refund → reallocated; idempotent replay; late callback → `409 LATE_PAYMENT_VOIDED` |
| Locust, event 3, `-u 80 -r 20 -t 30s` | ✓ **7,459 requests, 249 RPS, p50 19 ms, p95 110 ms, p99 170 ms, 0 failures, 0 × 5xx** (raw: `reports/raw/docker_run_*`) |
| Invariants in container DB (`docker compose exec api python -m app.cli invariants`) | ✓ all 9 checks = 0 violations |
| Graceful stop (`docker compose stop worker`) | ✓ `received signal 15, finishing current cycle` → `expiry worker stopped` |
| Local `last_seat_db` afterwards | ✓ unchanged (still events 1, 2, 3) |

The containerised latency is higher than in Workflow A. Requests cross the Docker Desktop/WSL 2 network layer and the API runs 2 workers instead of 4. Both figures come from one laptop and aren't directly comparable.

Bug found and fixed during this run: the `worker` and `migrate` services inherited the image's HTTP `HEALTHCHECK`, which targets the API on port 8000, so the worker would have been reported as unhealthy. Both now set `healthcheck: disable: true` in `compose.yaml`.

## Not verified

* **GitHub Actions workflow** (`.github/workflows/tests.yml`): written but not executed (repository not pushed).
