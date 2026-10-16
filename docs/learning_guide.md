# Learning Guide — DBMS & Backend Concepts, Taught Through This Codebase

Every concept below is tied to a file you can open and a query you can run. Read it top to bottom once; then use it as a reference while you rehearse the review.

---

## 1. Backend vs database, and the life of one request

* The **database** (PostgreSQL) stores data and enforces rules about it (types, constraints, transactions, locks).
* The **backend** (our FastAPI app) is a program that receives HTTP requests from clients, decides what to do, talks to the database, and sends back HTTP responses.
* The **client** (browser, Swagger UI, `curl`, Locust) never touches the database directly.

Life of `POST /api/v1/reservations` (see [architecture.md](architecture.md)):

```
client --HTTP--> uvicorn --> FastAPI route --> Pydantic validation --> run_mutation (BEGIN)
       --> reservation_service (SELECT ... FOR UPDATE, INSERT) --> COMMIT --> JSON response --> client
```

## 2. APIs, REST, HTTP

* An **API** (Application Programming Interface) is a contract: "send me *this*, I'll answer *that*". Ours is listed in [api_guide.md](api_guide.md) and rendered live at `/docs`.
* **REST** style: URLs name *resources* (`/reservations/{id}`), HTTP **methods** say what to do:
  * `GET` — read, never changes anything (safe to repeat).
  * `POST` — create something or trigger an action (`/reservations`, `/confirm`, `/cancel`). Not automatically safe to repeat — which is why we add idempotency keys (§10).
* **Status codes** tell the client what happened without parsing the body:

| Code | Meaning | Where we use it |
|---|---|---|
| 200 OK | success | reads, confirm SUCCESS, cancel |
| 201 Created | a new resource exists | a new hold |
| 202 Accepted | "received, still processing" | DELAYED payment |
| 402 Payment Required | payment failed | confirm FAILURE |
| 404 Not Found | no such seat/reservation/payment | |
| 409 Conflict | request is valid but conflicts with current state | seat taken, hold expired, late payment |
| 422 Unprocessable Entity | the JSON is malformed / fails validation | seat_number = 0 |
| 500 Internal Server Error | a bug | never seen in our tests/load test |
| 503 Service Unavailable | temporary — retry | lock timeout, DB down |

* **Headers** carry metadata: `Content-Type: application/json`, our `Idempotency-Key`, and the response header `Idempotent-Replayed: true`.
* **JSON payloads** are the request/response bodies.

## 3. FastAPI, dependency injection, Pydantic, `/docs`

Open [`app/api/routes/reservations.py`](../app/api/routes/reservations.py):

```python
@router.post("", status_code=201, response_model=ReservationActionResponse, ...)
def create_reservation(
    payload: ReservationCreate,                       # 1. JSON body -> validated Python object
    session: Session = Depends(get_db),               # 2. dependency injection
    key: str | None = Depends(idempotency_key),       # 3. header extraction + validation
):
```

1. **Pydantic** (`app/schemas/__init__.py`) declares the shape: `seat_number: int = Field(ge=1, le=100_000)`. FastAPI rejects bad input with 422 *before your code runs*.
2. **Dependency injection** (`Depends(get_db)`): FastAPI calls `get_db()` for you, passes the `Session` in, and runs the `finally: session.close()` after the response. Every request gets its own session — sessions are never shared between threads.
3. FastAPI reads the type hints and generates **OpenAPI** JSON (`/openapi.json`) and the **Swagger UI** (`/docs`) automatically.

We use plain `def` (not `async def`) routes: FastAPI runs them in a worker-thread pool, and each thread uses a normal blocking database connection. That keeps `BEGIN … COMMIT` easy to trace.

## 4. PostgreSQL vs SQLAlchemy (ORM)

An **ORM** maps Python classes to tables. In [`app/models/__init__.py`](../app/models/__init__.py):

```python
class Seat(Base):
    __tablename__ = "seats"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    seat_number: Mapped[int] = mapped_column(Integer)
```

and this Python:

```python
select(Seat).where(Seat.event_id == 1, Seat.seat_number == 42).with_for_update()
```

becomes this SQL:

```sql
SELECT seats.id, seats.event_id, seats.seat_number, seats.created_at
FROM seats WHERE seats.event_id = 1 AND seats.seat_number = 42 FOR UPDATE
```

**Why raw-SQL thinking still matters:** the ORM will happily generate SQL that is *correct for one user and wrong for two*. Locks, isolation, partial indexes and `clock_timestamp()` are database concepts; you must know exactly which statements run and in which order. That is why the critical paths in `app/services/` are written statement-by-statement with the SQL in comments, and some queries are plain `text()` SQL.

**Migrations (Alembic):** the schema is created by versioned scripts in `migrations/versions/`, not by `create_all()`. A migration is like a Git commit for the database schema: repeatable, reviewable, applied in order (`alembic upgrade head`).

## 5. CRUD vs append-only state transitions

CRUD = Create, Read, Update, Delete. A naïve booking system would `DELETE` a reservation when it is cancelled. We **never delete**; we move rows through a **state machine** (`HELD → CONFIRMED → CANCELLED`, `HELD → EXPIRED`):

* history is preserved (who held seat 42 at 6:00:03 and let it expire?) — needed for audits, refunds, disputes;
* a late payment callback can still find its reservation and see "EXPIRED" (with `DELETE`, it would find nothing and might create a new booking);
* the partial unique index (§8.4) lets unlimited historical rows coexist with exactly one live row.

## 6. Transactions and ACID — in our code

A **transaction** is a group of statements that succeed or fail as one unit:

```sql
BEGIN;
  SELECT ... FROM seats ... FOR UPDATE;
  INSERT INTO payment_attempts ... ;      -- charge SUCCEEDED
  UPDATE reservations SET status='CONFIRMED' ... ;
COMMIT;      -- or ROLLBACK
```

SQLAlchemy starts the transaction automatically on the first statement ("autobegin"); `run_mutation` in [`idempotency_service.py`](../app/services/idempotency_service.py) ends it with `session.commit()` or `session.rollback()`.

| Letter | Meaning | Where it shows up |
|---|---|---|
| **A**tomicity | all statements commit, or none | payment row + CONFIRMED status commit together. `test_crash_mid_confirmation_rolls_back_everything` injects a crash *between* them: the payment row vanishes too. |
| **C**onsistency | every commit leaves the DB satisfying all constraints | CHECK constraints (legal statuses), FKs, the two partial unique indexes; `check_invariants()` |
| **I**solation | concurrent transactions don't see each other's half-done work | `READ COMMITTED` + `SELECT ... FOR UPDATE` so concurrent bookings of one seat run one after another |
| **D**urability | once COMMIT returns, it survives crashes | PostgreSQL's write-ahead log; `test_restart_loses_no_state` shows nothing lives in Python memory |

**What would go wrong without it:** commit the payment in one transaction and the status in another; a crash between them leaves money taken and no booking — an orphaned payment.

## 7. The check-then-act race condition

See the timeline table in [concurrency_design.md §1](concurrency_design.md). Two transactions both *check* ("is seat 42 free?" → yes), then both *act* (INSERT). Each was individually correct; together they double-book.

Why not a Python `threading.Lock`? It only protects threads *inside one process*. We run 4 uvicorn worker processes (and could run 20 containers). Only the database is shared by all of them, so the lock must live there.

## 8. Locking in PostgreSQL

### 8.1 Row locks: `SELECT ... FOR UPDATE`
Locks the selected rows until the transaction ends. Another `FOR UPDATE` (or `UPDATE`/`DELETE`) on the same rows **waits**. Plain `SELECT` (no `FOR UPDATE`) never waits — readers are not blocked (that's MVCC: readers see the last committed version).

### 8.2 What exactly happens when B waits for A
Try it yourself in two psql windows connected to `last_seat_db`:

```sql
-- window A                                   -- window B
BEGIN;
SELECT id FROM seats WHERE event_id=1
  AND seat_number=42 FOR UPDATE;
                                              BEGIN;
                                              SELECT id FROM seats WHERE event_id=1
                                                AND seat_number=42 FOR UPDATE;
                                              -- ...hangs...
COMMIT;
                                              -- returns immediately now
                                              ROLLBACK;
```

In `READ COMMITTED`, once B gets the lock it sees A's committed changes in its next statements, so B's "is there an active hold?" query finds A's row → `409`.

### 8.3 Why lock `seats` and not `reservations`?
Before the first hold, *there is no reservation row to lock*. Two transactions locking "nothing" don't block each other. The `seats` row always exists — a perfect mutex.

### 8.4 The partial unique index — the ultimate guarantee
```sql
CREATE UNIQUE INDEX uq_reservations_one_active_per_seat
ON reservations (seat_id) WHERE status IN ('HELD', 'CONFIRMED');
```
"At most one row per seat *among rows whose status is HELD or CONFIRMED*." Even if someone writes buggy code that forgets the lock, PostgreSQL refuses the second active row (`test_partial_unique_index_is_the_last_line_of_defence`).

### 8.5 Deadlocks and lock ordering
Deadlock: A holds X wants Y; B holds Y wants X; both wait forever (PostgreSQL detects it after ~1 s and kills one). Prevention: everyone locks in the same order — **seat → reservation → payment** ([seat_service.py](../app/services/seat_service.py) docstring). Starting from a reservation id, we first look up its seat (no lock), lock the seat, *then* lock the reservation.

### 8.6 `SKIP LOCKED`
`SELECT ... FOR UPDATE SKIP LOCKED` grabs only rows nobody else has locked, without waiting. The expiry worker uses it so it never waits behind (or deadlocks with) an API request; a skipped seat is picked up next cycle.

## 9. Time: `clock_timestamp()` vs `now()`

```sql
BEGIN;
SELECT now(), clock_timestamp();   -- same-ish
SELECT pg_sleep(2);
SELECT now(), clock_timestamp();   -- now() unchanged! clock_timestamp() is 2 s later
COMMIT;
```

`now()` is the transaction **start** time. A request that waited 800 ms for a lock would judge expiry with an 800 ms-old clock. Rule in our code: lock first, then `t_now = db_now(session)` ([seat_service.py](../app/services/seat_service.py)), use that one value everywhere in the transaction. Valid iff `t_now < expires_at`.

## 10. Expiry: lazy vs worker

* **Lazy expiry:** whenever a request touches a seat (under its lock), stale holds on that seat are flipped to `EXPIRED` first. Correctness never depends on a background process.
* **Worker** ([reservation_expiry.py](../app/jobs/reservation_expiry.py)): every few seconds, expire up to 100 past-due holds in one short transaction using `SKIP LOCKED`. Keeps counts tidy for seats nobody is asking about. In the load test, lazy expiry reclaimed abandoned holds within 6–60 ms because the event sold out and students kept asking; in a quiet test the worker reclaimed 10 untouched holds within one cycle.

## 11. Idempotency keys

**Idempotent** = doing it twice has the same effect as doing it once. `GET` is naturally idempotent; `POST /confirm` is not (two clicks = two charges?). The client sends a unique `Idempotency-Key: <uuid>`; the server stores `(scope, key) → (request fingerprint, response)` in `idempotency_records` **in the same transaction** as the business change:

* retry after a timeout → stored response replayed, no new charge;
* double-click (two simultaneous requests) → the second waits on the unique index, then replays;
* same key with a different body → `409 IDEMPOTENCY_KEY_PAYLOAD_MISMATCH` (client bug);
* a crash → the record rolls back with everything else, so the retry runs fresh.

## 12. Connection pooling

Opening a PostgreSQL connection costs a few ms and a server process. The SQLAlchemy **engine** keeps a pool ([session.py](../app/db/session.py)): `pool_size=20` kept open, up to `max_overflow=30` extra under load. A request borrows one, returns it at `session.close()`. Too large a pool across many processes exceeds `max_connections` (100 here) — that is why the load test ran 4 workers × 15 connections, and why production would add PgBouncer.

## 13. Testing pyramid

| Kind | Needs DB? | Example | What it catches |
|---|---|---|---|
| **Unit** | no | `tests/unit/test_units.py` — validation, SHA-256 fingerprint, expiry boundary | logic bugs, fast (0.3 s) |
| **Integration** | yes (`last_seat_test_db`) | `tests/integration/test_payments.py` — late callback voids payment | wrong SQL, wrong transitions, constraint mistakes |
| **Concurrency** | yes + real HTTP server + threads | `tests/concurrency/test_seat_stampede.py` — 100 threads, one barrier | races, deadlocks, missing locks — bugs that never appear with one user |
| **Load** | yes, running API | `locust/locustfile.py` | throughput, latency, errors under sustained traffic |

A `threading.Barrier(100)` makes 100 threads wait until all are ready, then releases them at the same instant — the closest we can get to "6:00:00 PM" on a laptop.

## 14. Glossary

* **Pessimistic locking** — lock first, then decide (our approach). **Optimistic** — decide, then detect conflicts at commit (version numbers) and retry.
* **MVCC** — Multi-Version Concurrency Control: PostgreSQL keeps old row versions so readers never block writers.
* **SAVEPOINT** — a checkpoint inside a transaction you can roll back to without abandoning the whole transaction (`session.begin_nested()` around the INSERT).
* **Partial index** — an index over only the rows matching a `WHERE`.
* **Stateless service** — keeps no per-user data in memory between requests; any replica can serve any request.
