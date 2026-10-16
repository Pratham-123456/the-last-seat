# Booking Workflows (sequence diagrams)

All diagrams show the real SQL issued by `app/services/`. `[idem]` steps happen only when an `Idempotency-Key` header is sent.

## 1. Happy path: hold → pay → booked

```mermaid
sequenceDiagram
    autonumber
    actor S as Student
    participant API as FastAPI route
    participant SVC as reservation/payment service
    participant PG as PostgreSQL
    S->>API: GET /api/v1/events/1/seats?available_only=true
    API->>PG: SELECT seats LEFT JOIN live reservations
    PG-->>S: 200 [seat 42 AVAILABLE, ...]
    S->>API: POST /api/v1/reservations {seat 42}
    API->>PG: BEGIN; [idem] INSERT .. ON CONFLICT DO NOTHING; SELECT .. FOR UPDATE
    SVC->>PG: SELECT seats WHERE seat_number=42 FOR UPDATE
    SVC->>PG: SELECT clock_timestamp()  (t_now)
    SVC->>PG: UPDATE reservations SET EXPIRED WHERE stale HELD
    SVC->>PG: SELECT active reservation -> none
    SVC->>PG: SAVEPOINT; INSERT reservations (HELD, expires_at=t_now+300s)
    API->>PG: [idem] UPDATE idempotency_records SET response; COMMIT
    API-->>S: 201 HELD (expires_at)
    S->>API: POST /reservations/{id}/confirm {"scenario":"SUCCESS"}
    SVC->>PG: SELECT seat_id (no lock); SELECT seats FOR UPDATE; clock_timestamp()
    SVC->>PG: SELECT reservation FOR UPDATE (HELD, t_now < expires_at)
    SVC->>PG: INSERT payment_attempts (CHARGE, SUCCEEDED)
    SVC->>PG: UPDATE reservations SET CONFIRMED
    API->>PG: COMMIT
    API-->>S: 200 CONFIRMED
    S->>API: GET /api/v1/customers/22BCE1234/bookings
    API-->>S: 200 history
```

## 2. Concurrent double-booking race (201 vs 409)

```mermaid
sequenceDiagram
    autonumber
    actor A as Student A
    actor B as Student B
    participant PG as PostgreSQL
    par simultaneous clicks
        A->>PG: BEGIN; SELECT seats(42) FOR UPDATE
    and
        B->>PG: BEGIN; SELECT seats(42) FOR UPDATE
    end
    PG-->>A: lock granted
    Note over B,PG: B BLOCKS here (row lock held by A)
    A->>PG: clock_timestamp(); no active hold; INSERT HELD
    A->>PG: COMMIT (releases lock)
    PG-->>A: 201 Created
    PG-->>B: lock granted, re-reads committed state
    B->>PG: clock_timestamp(); SELECT active -> A's HELD row
    B->>PG: COMMIT (no insert)
    PG-->>B: 409 SEAT_UNAVAILABLE
```

With 100 students the other 98 queue the same way. Measured: 1 × 201, 99 × 409, 0 × 5xx (`tests/concurrency/test_seat_stampede.py`).

## 3. Failed payment + retry

```mermaid
sequenceDiagram
    autonumber
    actor S as Student
    participant API
    participant PG as PostgreSQL
    S->>API: POST /confirm {"scenario":"FAILURE"}
    API->>PG: lock seat -> reservation; INSERT CHARGE FAILED (attempt 1)
    API->>PG: COMMIT  (failure history is kept)
    API-->>S: 402 PAYMENT_FAILED, retry_allowed=true, reservation still HELD
    S->>API: POST /confirm {"scenario":"SUCCESS"}  (before expires_at)
    API->>PG: lock seat -> reservation; INSERT CHARGE SUCCEEDED (attempt 2); CONFIRMED
    API->>PG: COMMIT
    API-->>S: 200 CONFIRMED
```

## 4. Delayed payment + callback before expiry

```mermaid
sequenceDiagram
    autonumber
    actor S as Student
    participant API
    participant GW as "Gateway (simulated)"
    participant PG as PostgreSQL
    S->>API: POST /confirm {"scenario":"DELAYED"}
    API->>PG: lock seat -> reservation; INSERT CHARGE PENDING; COMMIT
    API-->>S: 202 PAYMENT_PENDING (payment_id) — no sleeping inside the transaction
    S->>API: POST /confirm again
    API-->>S: 409 PAYMENT_ALREADY_PENDING
    GW->>API: POST /payments/{id}/simulate-callback {"outcome":"SUCCEEDED"}
    API->>PG: lock seat -> reservation -> payment; t_now < expires_at
    API->>PG: payment SUCCEEDED; reservation CONFIRMED; COMMIT
    API-->>GW: 200 CONFIRMED
    GW->>API: same callback again (duplicate webhook)
    API-->>GW: 200 CALLBACK_ALREADY_PROCESSED (nothing changes)
```

## 5. Late callback after expiry → VOIDED

```mermaid
sequenceDiagram
    autonumber
    actor A as Student A
    actor B as Student B
    participant GW as Gateway
    participant PG as PostgreSQL
    A->>PG: hold seat 12; confirm DELAYED -> payment PENDING
    Note over A,PG: expires_at passes, A's bank is still thinking
    B->>PG: POST /reservations seat 12 -> lock seat; lazy expiry:<br/>A's hold EXPIRED, A's payment VOIDED; INSERT B HELD; COMMIT
    B->>PG: confirm SUCCESS -> B CONFIRMED
    GW->>PG: callback SUCCEEDED for A's payment
    PG->>PG: lock seat -> reservation (EXPIRED) -> payment (VOIDED)
    PG-->>GW: 409 LATE_PAYMENT_VOIDED — A is NOT revived, B keeps the seat
```

If nobody took the seat, the callback itself performs the expiry (`HELD` past `expires_at` → `EXPIRED`, payment → `VOIDED`) and answers the same 409.

## 6. Cancellation + seat reallocation

```mermaid
sequenceDiagram
    autonumber
    actor A as Student A
    actor B as Student B
    participant PG as PostgreSQL
    A->>PG: hold + confirm SUCCESS (CHARGE c1 SUCCEEDED)
    A->>PG: POST /reservations/{id}/cancel
    PG->>PG: lock seat -> reservation -> charge c1
    PG->>PG: reservation CANCELLED; INSERT REFUND (related_payment_id=c1); COMMIT
    PG-->>A: 200 CANCELLED + refund record
    Note over PG: CANCELLED is outside the partial unique index -> seat free
    B->>PG: POST /reservations same seat
    PG-->>B: 201 HELD (seat reallocated)
```

Cancelling a `HELD` reservation works the same way without the refund (any `PENDING` payment is voided). Cancelling twice returns `200 ALREADY_CANCELLED`; cancelling an expired hold returns `409 RESERVATION_ALREADY_EXPIRED`.
