# Project Status

**Current milestone:** All phases A–H complete except Docker runtime verification.
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
| G. Docker & Compose | ⚠️ Written, config validated, **not run** | `docker compose config` OK; Docker Desktop engine never started (WSL `docker-desktop` Stopped) |
| H. Docs, lint, git readiness | ✅ Done (git repo initialised; first commit left to the owner) | Ruff clean; 73/73 tests |

## Latest successful test run

`pytest -v -s` → **73 passed in 65.31 s** (24 unit, 40 integration, 9 concurrency). `ruff check .` and `ruff format --check .` clean.

## Data created on the local server (all additive)

| Database | Contents |
|---|---|
| `last_seat_db` | event 1 = demo concert (500 seats, untouched by tests/load), event 2 = load-test run (500 seats, sold out), event 3 = worker check + API examples + live demo (50 seats) |
| `last_seat_test_db` | rebuilt by the test fixtures on every run (only this DB is ever truncated) |

No other database or role on the server was created, modified or read.

## Blockers / open items

1. **Docker runtime not verified.** Next action: open Docker Desktop, resolve whatever dialog or error prevents the engine from starting, then run `docker compose up --build -d` and `irm http://127.0.0.1:8000/health/ready`.
2. **CI workflow not executed.** It runs once the repo is pushed to GitHub.
3. **Initial git commit** not made yet: the repository is initialised and `.env` is confirmed ignored.

## Next actions

* Personalise [DECISIONS.md](DECISIONS.md) (drafts).
* Rehearse with [docs/review_questions.md](docs/review_questions.md) and `scripts/demo.ps1`.
