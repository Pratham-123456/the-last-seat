# Technical Review Preparation

## Part 1 — 30 questions you may be asked (with answers)

### Concurrency & locking

**1. How do you guarantee a seat cannot be booked twice?**
Three layers. (a) Every transaction that decides anything about a seat first runs `SELECT ... FROM seats ... FOR UPDATE`, so decisions for one seat are serialised. (b) After acquiring the lock it reads `clock_timestamp()` and lazily expires stale holds, so decisions use fresh time and fresh state. (c) The partial unique index `uq_reservations_one_active_per_seat` makes a second `HELD`/`CONFIRMED` row physically impossible even if (a) were bypassed.

**2. Why lock the `seats` row instead of the `reservations` row?**
Before the first hold there is no reservation row to lock; two transactions locking "nothing" don't block each other. The seat row always exists and is never modified, so it is a stable mutex.

**3. What happens to the 99 losers in the stampede test?**
They queue on the seat's row lock. Each, in turn, gets the lock, re-reads committed data (READ COMMITTED), sees the winner's `HELD` row and returns `409 SEAT_UNAVAILABLE`. Measured: 1 × 201, 99 × 409, 0 × 5xx, and exactly 1 active row in PostgreSQL.

**4. Why not use `SERIALIZABLE` isolation?**
It would also be correct, but under hot-spot contention most transactions abort with serialization failures and must be retried by the caller. Pessimistic locking in READ COMMITTED queues them and answers each deterministically on the first attempt.

**5. Why not optimistic locking (version column)?**
Same retry storm under contention: 100 students read version 1, one commits version 2, 99 fail and retry, and so on. Optimistic locking fits low-contention data; a 6 PM ticket launch is the opposite.

**6. Why can't you just use a Python lock?**
It only exists inside one process. We run 4 uvicorn workers (and could run many containers). Only PostgreSQL is shared by all of them.

**7. How do you prevent deadlocks?**
A global lock order: idempotency row → seat → reservation → payment. Operations that start from a reservation or payment id look up the seat id without locking, lock the seat, then re-read the child rows `FOR UPDATE`. The worker locks seats in ascending id order with `SKIP LOCKED`, so it never waits. No deadlocks were observed in any test or in the load test.

**8. What does `SKIP LOCKED` do and why does the worker use it?**
It locks only rows nobody else has locked, skipping busy ones without waiting. The worker therefore never blocks behind an API request and multiple worker replicas share work; a skipped seat is retried next cycle.

**9. Why `clock_timestamp()` and not `now()`?**
`now()` is frozen at transaction start. A request that waited on a lock would compare `expires_at` against a stale clock and could confirm a hold that has already expired. We read `clock_timestamp()` after acquiring the lock and use that single value for the whole transaction.

**10. Is the expiry boundary inclusive or exclusive?**
Valid iff `t_now < expires_at`. At exactly `expires_at` the hold is expired. Unit-tested in `test_effective_status`.

### Expiry

**11. If the background worker crashes, can seats get stuck as HELD forever?**
No. Any request for that seat lazily expires stale holds under the seat lock before deciding, and the availability query already treats `HELD` past `expires_at` as available. The worker is housekeeping, not correctness.

**12. Why does a stale HELD row block a new insert at all?**
The partial unique index's predicate is `status IN ('HELD','CONFIRMED')`. A row whose status still says `HELD` is in the index regardless of its `expires_at`. That is why lazy expiry rewrites its status first, in the same transaction, before inserting.

**13. Can a confirmed booking expire?**
No. `CONFIRMED → EXPIRED` is forbidden; all expiry updates filter on `status = 'HELD'`.

### Payments

**14. Why commit a FAILED payment instead of rolling back?**
The failure is a real fact (an attempt happened) and must stay in the audit trail. `PaymentFailed` is a `DomainError`, so `run_mutation` commits the recorded attempt and returns 402. The hold stays `HELD` so the student can retry.

**15. How is a DELAYED payment simulated without blocking?**
We insert a `PENDING` payment row, commit, and return `202`. The "bank" answers later via `POST /payments/{id}/simulate-callback`. We never sleep inside a transaction, because that would hold the seat lock and a connection for the whole delay.

**16. What if the callback arrives after the hold expired and someone else bought the seat?**
The callback locks seat → reservation → payment, sees the reservation is `EXPIRED` (or makes it so if it is past `expires_at`), marks the payment `VOIDED` and returns `409 LATE_PAYMENT_VOIDED`. It never revives the reservation. Tested in `test_late_callback_after_seat_reallocated_cannot_double_book`.

**17. How do you stop double charging?**
`uq_payment_one_succeeded_charge_per_reservation` (at most one `SUCCEEDED` `CHARGE` per reservation), plus confirm returns `200 ALREADY_CONFIRMED` without creating a charge when the reservation is already confirmed, plus `409 PAYMENT_ALREADY_PENDING` while a delayed payment is outstanding. 10 concurrent SUCCESS confirms produced exactly 1 charge.

**18. What happens on cancelling a confirmed booking?**
Status → `CANCELLED`, and a `REFUND` row with `related_payment_id` pointing to the original charge is inserted. Because `CANCELLED` is outside the partial index, the seat is immediately reservable again.

### Idempotency

**19. What is an idempotency key and why do you need one?**
A client-chosen unique id per logical request. If the network drops the response, the client retries with the same key and gets the original response instead of a second charge or a second reservation. It satisfies requirement #6 ("consistent when requests are repeated") and bonus #4.

**20. Two identical requests with the same key arrive at the same millisecond. What happens?**
Both `INSERT ... ON CONFLICT DO NOTHING`. The second blocks on the unique index until the first commits, then does nothing; its `SELECT ... FOR UPDATE` finds the completed record and replays it. Tested with 10 concurrent requests: all got 201 with the same reservation id, and 1 row was created.

**21. Why store the idempotency record in the same transaction as the business change?**
So they can never disagree. If the business change rolls back (crash), the record rolls back too and a retry runs fresh; if it commits, the stored response is exactly what happened.

**22. Same key, different body?**
The SHA-256 fingerprint (scope + path params + canonical JSON body) differs, so we return `409 IDEMPOTENCY_KEY_PAYLOAD_MISMATCH` and roll back.

### Schema & design

**23. Why no `is_available` column on `seats`?**
It would be a second source of truth that must change whenever a reservation changes *and* whenever the clock passes `expires_at`. Missing one update gives a phantom-available or phantom-held seat. Availability is derived by a join at read time.

**24. Why never DELETE reservations?**
History (audits, refunds, disputes), and so late callbacks can still find the reservation and see it is `EXPIRED`. The partial unique index allows unlimited history rows per seat.

**25. Why are timestamps `TIMESTAMPTZ` and taken from the database?**
`TIMESTAMPTZ` is unambiguous across timezones. Using PostgreSQL's clock means all API replicas agree on "now"; their own clocks could drift.

**26. What protects your other databases on this laptop?**
A dedicated non-superuser role `last_seat_app` that owns only `last_seat_db` and `last_seat_test_db`; `assert_project_database()` refuses any other database name in the app, Alembic, seed and tests; and the test cleanup asks the server `SELECT current_database()` before it truncates anything.

### Testing, ops, scaling

**27. How do you test concurrency for real?**
A real uvicorn server runs in a background thread. 100 threads each open their own HTTP client, wait on a `threading.Barrier(100)`, and fire together. Afterwards we query PostgreSQL directly and run the invariant SQL checks.

**28. How do you prove atomicity?**
`test_crash_mid_confirmation_rolls_back_everything` monkeypatches the "mark confirmed" step to raise after the payment row is inserted. The API returns 500 with no stack trace, and afterwards there is no payment row, the reservation is still `HELD`, and no idempotency record exists, so a retry with the same key succeeds.

**29. What did the load test show?**
150 users for 60 s on one laptop: 26,524 requests, ~447 RPS, p50 9 ms, p95 30 ms, p99 74 ms, 0 failures, 0 5xx. All invariant checks returned 0 violations afterwards. It is a local bounded test, not a 10k-user benchmark.

**30. How would this scale to 10,000 students at 6 PM?**
Stateless API replicas behind a load balancer; PgBouncer in front of PostgreSQL; a virtual waiting room to meter admissions; best-available seat allocation with `SKIP LOCKED` to spread contention; short-TTL caching of the seat map (never of the booking decision). See [scaling.md](scaling.md).

---

## Part 2 — Line-by-line walkthrough of `create_reservation`

From [`app/services/reservation_service.py`](../app/services/reservation_service.py), called inside `run_mutation` (so a transaction is already open, and the idempotency row is locked if a key was sent):

```python
seat = seat_service.lock_seat_by_number(session, data.event_id, data.seat_number)
```
`SELECT ... FROM seats WHERE event_id=:e AND seat_number=:n FOR UPDATE`. Blocks while another transaction holds seat 42. Raises `404 SEAT_NOT_FOUND` / `EVENT_NOT_FOUND` if missing.

```python
t_now = seat_service.db_now(session)
```
`SELECT clock_timestamp()`, taken **after** the wait, so it's the real current time.

```python
seat_service.expire_stale_holds_for_seat(session, seat.id, t_now)
```
`UPDATE reservations SET status='EXPIRED' WHERE seat_id=:s AND status='HELD' AND expires_at <= :t_now` (plus voiding their pending payments). Frees the partial index slot of an abandoned hold.

```python
active = session.execute(select(Reservation).where(
    Reservation.seat_id == seat.id, Reservation.status.in_(ACTIVE_STATUSES))).scalar_one_or_none()
if active is not None:
    raise _seat_unavailable(...)
```
Is there still a live hold or a confirmed booking? Then `409 SEAT_UNAVAILABLE`. `run_mutation` commits (keeping any lazy expiry) and stores the 409 for idempotent replay.

```python
reservation = Reservation(..., status=HELD, created_at=t_now,
                          expires_at=t_now + timedelta(seconds=hold_ttl_seconds), updated_at=t_now)
with session.begin_nested():   # SAVEPOINT
    session.add(reservation)
    session.flush()             # INSERT happens here
```
The insert runs inside a savepoint. If the partial unique index ever fires (it can't while everyone respects the lock), only the savepoint is rolled back, and the `IntegrityError` becomes a clean 409 rather than a 500.

```python
return 201, action_body(...)
```
`run_mutation` then writes `(201, body)` into the idempotency record and `COMMIT`s, which releases the seat lock and lets the next waiter proceed.

---

## Part 3 — Naïve vs. locked implementation, side by side

| | Naïve (broken) | The Last Seat |
|---|---|---|
| Read | `SELECT * FROM reservations WHERE seat_id=42 AND status='HELD'` | `SELECT ... FROM seats WHERE id=42 FOR UPDATE` first |
| Concurrency | A and B both read "free" | B waits until A commits, then sees A's row |
| Expiry check | `now()` or Python `datetime.now()` | `clock_timestamp()` after the lock |
| Stale holds | rely on a cron job; seat stuck if it stops | lazy expiry under the lock + worker |
| Storage guarantee | none | partial unique index on active rows |
| Retry after timeout | second reservation / second charge | `Idempotency-Key` replays the stored response |
| Failed payment | often rolled back, so the attempt is lost | committed as `FAILED`, hold kept |
| Late payment | may confirm an expired hold, so the seat is sold twice | `VOIDED`, `409 LATE_PAYMENT_VOIDED` |
| Result in 100-thread test | multiple 201s for one seat | exactly 1 × 201, 99 × 409 |

```python
# Naïve
def reserve(session, seat_id, customer):
    if not session.query(Reservation).filter_by(seat_id=seat_id, status="HELD").first():
        session.add(Reservation(seat_id=seat_id, customer_ref=customer, status="HELD",
                                expires_at=datetime.now() + timedelta(minutes=5)))
        session.commit()
        return 201
    return 409
```

Two threads can both pass the `if` before either commits. Without the partial unique index both inserts succeed, and the seat is double-booked.
