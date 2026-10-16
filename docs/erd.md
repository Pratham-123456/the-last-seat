# Entity-Relationship Diagram

```mermaid
erDiagram
    events ||--o{ seats : "has"
    events ||--o{ reservations : "for"
    seats ||--o{ reservations : "history of holds/bookings"
    reservations ||--o{ payment_attempts : "charges & refunds"
    payment_attempts |o--o{ payment_attempts : "REFUND.related_payment_id -> CHARGE"

    events {
        BIGINT id PK
        VARCHAR_255 name "NOT NULL"
        INTEGER total_seats "CHECK > 0"
        TIMESTAMPTZ starts_at "NOT NULL"
        TIMESTAMPTZ created_at "DEFAULT now()"
    }
    seats {
        BIGINT id PK
        BIGINT event_id FK "ON DELETE CASCADE"
        INTEGER seat_number "CHECK >= 1"
        TIMESTAMPTZ created_at "DEFAULT now()"
    }
    reservations {
        UUID id PK
        BIGINT event_id FK
        BIGINT seat_id FK
        VARCHAR_128 customer_ref "demo identifier"
        VARCHAR_20 status "HELD|CONFIRMED|EXPIRED|CANCELLED"
        TIMESTAMPTZ created_at
        TIMESTAMPTZ expires_at "t_now + HOLD_TTL"
        TIMESTAMPTZ confirmed_at "nullable"
        TIMESTAMPTZ cancelled_at "nullable"
        TIMESTAMPTZ updated_at
    }
    payment_attempts {
        UUID id PK
        UUID reservation_id FK
        INTEGER attempt_number "1,2,3... per reservation"
        VARCHAR_20 payment_type "CHARGE|REFUND"
        VARCHAR_20 scenario "SUCCESS|FAILURE|DELAYED|REFUND"
        VARCHAR_20 status "PENDING|SUCCEEDED|FAILED|VOIDED"
        UUID related_payment_id FK "nullable"
        TIMESTAMPTZ created_at
        TIMESTAMPTZ completed_at "nullable"
        TIMESTAMPTZ updated_at
    }
    idempotency_records {
        BIGINT id PK
        VARCHAR_128 scope
        VARCHAR_128 idempotency_key
        VARCHAR_64 request_hash "SHA-256 hex"
        INTEGER response_status "nullable"
        JSONB response_body "nullable"
        TIMESTAMPTZ created_at
    }
```

`idempotency_records` deliberately has no foreign keys: it stores *responses*, which can describe any resource (or an error).

## Constraints at a glance

| Table | Constraint | Kind | Purpose |
|---|---|---|---|
| events | `ck_events_total_seats_positive` | CHECK | capacity > 0 |
| seats | `uq_seats_event_id_seat_number` | UNIQUE | one seat 42 per event; makes seeding idempotent |
| seats | `ck_seats_seat_number_positive` | CHECK | seat numbers start at 1 |
| reservations | `ck_reservations_status_valid` | CHECK | only the 4 legal states |
| reservations | **`uq_reservations_one_active_per_seat`** | **PARTIAL UNIQUE** `(seat_id) WHERE status IN ('HELD','CONFIRMED')` | **no double booking, ever** |
| payment_attempts | `ck_payment_attempts_*_valid` (3) | CHECK | legal type / scenario / status |
| payment_attempts | `uq_payment_attempts_reservation_id_attempt_number` | UNIQUE | attempt numbers never collide |
| payment_attempts | **`uq_payment_one_succeeded_charge_per_reservation`** | **PARTIAL UNIQUE** `(reservation_id) WHERE payment_type='CHARGE' AND status='SUCCEEDED'` | **no double charging** |
| idempotency_records | `uq_idempotency_records_scope_idempotency_key` | UNIQUE | one record per key per endpoint |

See [database_design.md](database_design.md) for indexes and the reasoning behind each column.
