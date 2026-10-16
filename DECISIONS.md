# Engineering Reflection — DRAFT ANSWERS

> **These are drafts for you to review, rewrite in your own voice and personalise.** They describe what was actually built and measured, but the reflection should be yours: change the emphasis, add what *you* found hard, and delete anything you cannot defend in your own words.

---

## 1. What was the most technically challenging part of the implementation, and how was it approached?

The hardest part was not stopping two students from grabbing a free seat at the same instant. Row locking handles that directly. It was making **time** and **concurrency** interact correctly: holds expire, payments can be delayed past the expiry, and a background worker rewrites rows while API requests are deciding about the same seats.

Three subtleties had to be solved together:

1. **A stale hold still occupies the unique index.** The partial unique index covers `status IN ('HELD','CONFIRMED')`. A hold whose `expires_at` has passed still says `HELD` until someone rewrites it, so a new student would be rejected even though the seat is "free". The fix is *lazy expiry under the seat lock*: before deciding, the transaction flips stale holds on that seat to `EXPIRED`. Correctness then never depends on the worker running.
2. **`now()` lies after waiting for a lock.** PostgreSQL's `now()` is the transaction start time. A request that waited behind 99 others could judge expiry with a stale clock. Every decision instead uses `clock_timestamp()` read *after* the lock is granted.
3. **Late payment callbacks.** A DELAYED payment can succeed at the bank after the hold expired and the seat was sold to someone else. The callback must never revive the old reservation. It locks seat → reservation → payment, sees `EXPIRED`, marks the payment `VOIDED`, and returns `409 LATE_PAYMENT_VOIDED`.

The approach was to write the invariants down first (ten of them, in `docs/concurrency_design.md`) and turn each into either a SQL check that must return zero rows or a dedicated test. The hard cases were then reproduced deliberately: 100 threads behind a `threading.Barrier`, backdating `expires_at` to simulate time passing, three expiry-worker threads racing 120 live requests, and a 1-second TTL timed to land on the boundary.

## 2. Describe one important design decision and explain why that approach was chosen over alternatives.

**Decision: lock the parent `seats` row with `SELECT ... FOR UPDATE` and derive availability from `reservations`, instead of storing an `is_available` flag or using optimistic or serializable concurrency.**

Alternatives considered:

* **`seats.is_available` boolean, updated on every change.** That creates two sources of truth. The flag must change when a reservation changes *and* when the clock passes `expires_at`, which no single write captures. Any missed update produces a phantom-free or phantom-held seat. Deriving availability with a `LEFT JOIN` on live reservations is always consistent.
* **Optimistic concurrency (version column) or `SERIALIZABLE` isolation.** Both are correct, but under a hot spot (100 students, one seat) almost every transaction aborts and must be retried, so latency and load grow with contention. Pessimistic locking queues the transactions in order and answers each one deterministically on its first attempt. The stampede test shows 1 × 201 and 99 × 409, with no retries and no 5xx.
* **Locking the `reservations` row.** It doesn't exist before the first hold, and two transactions locking "nothing" don't block each other. The seat row always exists, so it makes a reliable mutex.
* **Redis `SET NX` for holds.** It's fast, but it creates a second source of truth outside the transaction that records payments and bookings. A Redis failover could lose holds.

The partial unique index stays as a second line of defence. Even if a future change forgets the lock, PostgreSQL refuses the second active row.

## 3. What is one limitation or failure case that the current implementation does not handle well?

**Hot-seat contention scales linearly, and there is no admission control.** Every request for the same seat queues on one row lock. Each critical section takes only a few milliseconds, so one seat handles a few hundred decisions per second. But if all 10,000 students target the same front-row seat at 6:00:00 PM, the last ones in the queue wait seconds. Past the 10 s `lock_timeout` they receive `503 DATABASE_BUSY` and must retry. The system stays *correct*: no double booking, clean rollbacks, and safe retries with an idempotency key. But it isn't *fair* or pleasant: there is no queue position and no "best available seat" fallback.

Related limitations:

* `customer_ref` is a caller-supplied identifier, not authentication. Anyone who knows a reservation UUID can confirm or cancel it, and nothing stops one person from holding many seats.
* `idempotency_records` grows forever; there is no TTL cleanup job yet.
* The Docker Compose workflow is written and its configuration validates, but it was not run in this environment.

## 4. If given one additional week, what would you improve first, and why?

**A virtual waiting room plus "best available" seat allocation**, because it addresses the main real-world risk above: the 6 PM spike.

1. Admission tokens issued at a controlled rate (a small Redis- or PostgreSQL-backed queue). Only token holders reach `POST /reservations`, so the database sees a steady load instead of a cliff.
2. A `POST /reservations/best-available` endpoint that picks a free seat with `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`. Concurrent buyers then take *different* seats instead of all fighting over one row. That turns a serial queue into parallel work.

After that: real authentication (JWT) with per-customer hold limits, a nightly cleanup of idempotency records older than 24 hours, PgBouncer in the Compose stack, and a distributed Locust run on separate hardware to measure the true limits.
