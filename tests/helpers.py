"""Small helpers shared by integration and concurrency tests."""

import uuid

from sqlalchemy import text

from app.services.invariants import check_invariants


def hold(client, seat_number: int, customer: str = "22BCE0001", key: str | None = None, event_id=1):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(
        "/api/v1/reservations",
        json={"event_id": event_id, "seat_number": seat_number, "customer_ref": customer},
        headers=headers,
    )


def confirm(client, reservation_id, scenario: str = "SUCCESS", key: str | None = None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(
        f"/api/v1/reservations/{reservation_id}/confirm",
        json={"scenario": scenario},
        headers=headers,
    )


def cancel(client, reservation_id, key: str | None = None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(f"/api/v1/reservations/{reservation_id}/cancel", headers=headers)


def callback(client, payment_id, outcome: str = "SUCCEEDED", key: str | None = None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(
        f"/api/v1/payments/{payment_id}/simulate-callback",
        json={"outcome": outcome},
        headers=headers,
    )


def backdate_expiry(db, reservation_id, seconds_ago: float = 1.0) -> None:
    """'Time travel': make a hold's expires_at lie in the past (test DB only)."""
    db.execute(
        text(
            "UPDATE reservations SET expires_at = clock_timestamp() - make_interval(secs => :s) "
            "WHERE id = :id"
        ),
        {"s": seconds_ago, "id": uuid.UUID(str(reservation_id))},
    )
    db.commit()


def active_count(db, seat_number: int, event_id: int = 1) -> int:
    n = db.execute(
        text(
            "SELECT COUNT(*) FROM reservations r JOIN seats s ON s.id = r.seat_id "
            "WHERE s.event_id = :e AND s.seat_number = :n AND r.status IN ('HELD','CONFIRMED')"
        ),
        {"e": event_id, "n": seat_number},
    ).scalar_one()
    db.rollback()
    return n


def count(db, sql: str, **params) -> int:
    n = db.execute(text(sql), params).scalar_one()
    db.rollback()
    return n


def assert_all_invariants(db) -> None:
    violations = check_invariants(db)
    assert all(v == 0 for v in violations.values()), violations
