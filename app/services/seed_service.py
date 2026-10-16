"""Idempotent demo-data seeding: running it twice changes nothing the second time."""

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

DEMO_EVENT_ID = 1
DEMO_EVENT_NAME = "CodeChef-VIT Live in Concert 2026"
DEMO_TOTAL_SEATS = 500
DEMO_STARTS_AT = datetime(2026, 12, 12, 13, 30, tzinfo=UTC)  # 7:00 PM IST


def seed_event(
    session: Session,
    *,
    event_id: int = DEMO_EVENT_ID,
    name: str = DEMO_EVENT_NAME,
    total_seats: int = DEMO_TOTAL_SEATS,
    starts_at: datetime = DEMO_STARTS_AT,
) -> dict:
    """Insert one event and seats 1..total_seats if they are missing. Commits."""
    created_event = session.execute(
        text(
            """
            INSERT INTO events (id, name, total_seats, starts_at)
            VALUES (:id, :name, :total, :starts_at)
            ON CONFLICT (id) DO NOTHING
            RETURNING id
            """
        ),
        {"id": event_id, "name": name, "total": total_seats, "starts_at": starts_at},
    ).first()
    # Keep the identity sequence ahead of explicitly chosen ids.
    session.execute(
        text(
            "SELECT setval(pg_get_serial_sequence('events', 'id'), "
            "(SELECT GREATEST(MAX(id), 1) FROM events))"
        )
    )
    existing_total = session.execute(
        text("SELECT total_seats FROM events WHERE id = :id"), {"id": event_id}
    ).scalar_one()
    inserted_seats = session.execute(
        text(
            """
            INSERT INTO seats (event_id, seat_number)
            SELECT :id, n FROM generate_series(1, :total) AS n
            ON CONFLICT (event_id, seat_number) DO NOTHING
            """
        ),
        {"id": event_id, "total": existing_total},
    ).rowcount
    seat_count = session.execute(
        text("SELECT COUNT(*) FROM seats WHERE event_id = :id"), {"id": event_id}
    ).scalar_one()
    session.commit()
    return {
        "event_id": event_id,
        "event_created": created_event is not None,
        "seats_inserted": inserted_seats,
        "seat_count": seat_count,
    }
