# Scaling Analysis — honest bottlenecks and trade-offs

## What we measured

On one laptop (i7-14650HX, 24 logical CPUs, 16 GB RAM, Windows 11, PostgreSQL 18 local; Locust running on the same machine), **4 uvicorn workers handled ~447 requests/s with p95 30 ms and p99 74 ms, 0 errors**, with 150 simulated users including a deliberate 5-seat hotspot ([load test report](../reports/load_test_report.md)). That is a local, bounded test, not a production benchmark.

The scenario in the brief — 10,000 students, 500 seats, portal opens at 6 PM — is a **write-contention** problem more than a throughput problem: at most 500 holds can ever succeed, and every other reservation attempt ends in a fast `409`.

## Bottleneck 1: hot-seat lock contention

All requests for the same seat serialise on one row lock. Each critical section is a handful of indexed statements (~1–3 ms), so a single seat can process a few hundred decisions per second; requests for *different* seats do not contend at all.

* **Effect at 6 PM:** if 10,000 students all click seat 1, they queue for that row. Latency for the last one grows linearly; `lock_timeout = 10s` (config) caps the wait and returns a retryable `503 DATABASE_BUSY` rather than hanging.
* **Mitigations (in order of cost):** client-side jitter/backoff; show live availability so clients pick different seats; *best-available* allocation (`SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` over free seats, so concurrent buyers take *different* free seats instead of fighting for the same one); a waiting room (below).

## Bottleneck 2: database connections

Each API process holds a pool (`DB_POOL_SIZE + DB_MAX_OVERFLOW`). PostgreSQL's default `max_connections` is 100; every connection costs memory. 10 API replicas × 50 connections would exceed it.

* **Fix:** put **PgBouncer** (transaction-pooling mode) between API and PostgreSQL. Our transactions are short and use no session-level state (no `SET`, no advisory locks, no prepared-statement reliance beyond the driver), so they are compatible with transaction pooling. Note: we set `lock_timeout`/`statement_timeout` via connection `options`; with PgBouncer those move to `ALTER ROLE last_seat_app SET ...`.

## Bottleneck 3: the seat listing

`GET /events/{id}/seats` joins 500 seats with live reservations on every call. It is cheap (sub-10 ms p50 measured) but is the most frequent request. Options: short-TTL HTTP caching (1–2 s staleness is acceptable for a *display* — the hold itself is still decided under the lock); a read replica for listings.

## Horizontal scaling of the API

The API is stateless (see [architecture.md](architecture.md)), so scaling it is just "run more replicas behind a load balancer". Correctness is unaffected because every guarantee is enforced inside PostgreSQL. The expiry worker can also run as multiple replicas: `FOR UPDATE SKIP LOCKED` makes them share work without blocking each other.

## Scaling PostgreSQL itself

One primary handles writes. For far larger events: partition `reservations` by `event_id`, or shard events across database clusters (a booking only ever touches one event, so events shard cleanly). Archive old `idempotency_records` (e.g. delete records older than 24 h — the keys are only meaningful for retries).

## Tier-2 bonuses: when they help and what they cost

| Bonus | Would help with | Cost / risk | Verdict for this project |
|---|---|---|---|
| **Redis** cache for seat maps | offloading the hottest read | cache invalidation: a stale "available" seat leads to a 409, a stale "held" seat hides inventory; must never be used to *decide* a booking | Useful for listings only; PostgreSQL stays authoritative. Not implemented. |
| **Redis** for holds (`SET NX EX`) | very fast hold TTLs | two sources of truth (Redis hold vs DB booking); a Redis failover can lose holds → double booking unless the DB still enforces uniqueness | Rejected: the DB-only design is simpler and provably correct. |
| **Waiting queue / virtual waiting room** | smoothing the 6 PM spike into a steady admission rate; fairness | extra service, token issuing, UX for queue position | The right next step for a real 10k-student launch. Admission control in front of an unchanged booking core. |
| **Kafka / RabbitMQ / NATS** | asynchronous payment webhooks, emails, analytics | at-least-once delivery means consumers must be idempotent (our callback endpoint already is); operational overhead | Good fit for real payment webhooks and notifications; unnecessary for the simulation. |
| **Reverse proxy** (nginx / Caddy / Traefik) | TLS, rate limiting per IP, gzip, load balancing across API replicas | another config surface | Needed for real deployment; trivial to add in front of `api` in compose. |
| **Deployment** | public demo | secrets management, exposure | Out of scope for the local submission; Docker image is deployment-ready. |

Idempotency keys (official bonus #4) **are implemented** — they solve mandatory requirement #6 inside PostgreSQL with no new infrastructure.

## How to run a bigger load test

On a dedicated machine (so Locust does not steal CPU from the API), with distributed Locust:

```powershell
# master
locust -f locust/locustfile.py --master --headless -u 5000 -r 200 -t 5m --host http://API_HOST:8000
# each worker machine/process
locust -f locust/locustfile.py --worker --master-host MASTER_IP
```

Scale `uvicorn --workers`, add PgBouncer, and raise PostgreSQL `max_connections` / `shared_buffers` before trusting numbers above a few thousand users.
