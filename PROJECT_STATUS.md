# Project Status

**Current milestone:** All phases A–H complete and verified, including CI on GitHub.
**Last updated:** 2026-10-17

## Phases

| Phase | Status | Evidence |
|---|---|---|
| A. Environment & PostgreSQL discovery | ✅ Done | PostgreSQL 18.4 service on :5432; Python 3.12.0 venv; role `last_seat_app` (non-superuser) owns `last_seat_db`, `last_seat_test_db` (created via `scripts/bootstrap_local_db.ps1`, run by the owner) |
| B. Schema, migrations, seed | ✅ Done | `alembic upgrade head` on both DBs; seed run twice → 500 seats then 0 inserted |
| C. Reservation service + concurrency proof | ✅ Done | 100-thread stampede: 1 × 201, 99 × 409, 0 × 5xx |
| D. Payments, cancellations, idempotency | ✅ Done | integration + concurrency tests passing |
| E. Routes, errors, expiry worker | ✅ Done | all 13 endpoints; worker verified in tests and live |
| F. Locust + invariants | ✅ Done | 26,524 req, 447 RPS, 0 failures, invariants all 0 |
| G. Docker & Compose | ✅ Done | build + migrate + api + worker verified; demo, Locust (7,459 req, 0 failures) and invariants inside containers; worker healthcheck bug fixed |
| H. Docs, lint, git readiness | ✅ Done | Ruff clean; 73/73 tests; pushed to github.com/Pratham-123456/the-last-seat; CI run passed |

## Latest successful test run

`pytest -v -s` → **73 passed in 65.31 s** (24 unit, 40 integration, 9 concurrency). `ruff check .` and `ruff format --check .` clean.

## Data created on the local server (all additive)

| Database | Contents |
|---|---|
| `last_seat_db` | event 1 = demo concert (500 seats, untouched by tests/load), event 2 = load-test run (500 seats, sold out), event 3 = worker check + API examples + live demo (50 seats) |
| `last_seat_test_db` | rebuilt by the test fixtures on every run (only this DB is ever truncated) |

No other database or role on the server was created, modified or read.

## Blockers / open items

1. The Compose stack was left **running** (default 300 s TTL). Stop it with `docker compose down`; its volume keeps the container data.

## Next actions

* Personalise [DECISIONS.md](DECISIONS.md) (drafts).
* Rehearse with [docs/review_questions.md](docs/review_questions.md) and `scripts/demo.ps1`.
