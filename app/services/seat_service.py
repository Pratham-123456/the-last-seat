"""Seat locking primitives, PostgreSQL clock, and derived seat availability.

LOCK ORDER (the whole codebase follows it, which is why it cannot deadlock):
    0. idempotency_records row (only when an Idempotency-Key is sent)
    1. seats row                (the per-seat mutex)
    2. reservations row(s)      (child of the seat)
    3. payment_attempts row(s)  (child of the reservation)
"""

from datetime import datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.core.exceptions import NotFound
from app.models import Event, PaymentAttempt, PaymentStatus, Reservation, ReservationStatus, Seat


def db_now(session: Session) -> datetime:
    """PostgreSQL's real wall clock *right now*.

    `now()` would return the time the transaction STARTED, which is stale if we just
    waited on a lock. Always call this AFTER acquiring the seat lock.
    """
    return session.execute(select(func.clock_timestamp())).scalar_one()


def lock_seat_by_number(session: Session, event_id: int, seat_number: int) -> Seat:
    """SELECT ... FROM seats WHERE event_id = :e AND seat_number = :n FOR UPDATE.

    Blocks while another transaction holds this seat's lock. In READ COMMITTED, once the
    lock is granted PostgreSQL re-reads the latest committed row version.
    """
    seat = session.execute(
        select(Seat)
        .where(Seat.event_id == event_id, Seat.seat_number == seat_number)
        .with_for_update()
    ).scalar_one_or_none()
    if seat is None:
        if session.get(Event, event_id) is None:
            raise NotFound(f"Event {event_id} does not exist.", code="EVENT_NOT_FOUND")
        raise NotFound(
            f"Seat {seat_number} does not exist for event {event_id}.", code="SEAT_NOT_FOUND"
        )
    return seat


def lock_seat_by_id(session: Session, seat_id: int) -> Seat:
    return session.execute(select(Seat).where(Seat.id == seat_id).with_for_update()).scalar_one()


def expire_reservations(session: Session, reservation_ids: list, t_now: datetime) -> int:
    """HELD -> EXPIRED for the given ids, voiding their PENDING payments.

    Caller must already hold the parent seat lock(s) (lock order step 1).
    """
    if not reservation_ids:
        return 0
    expired = (
        session.execute(
            update(Reservation)
            .where(
                Reservation.id.in_(reservation_ids), Reservation.status == ReservationStatus.HELD
            )
            .values(status=ReservationStatus.EXPIRED, updated_at=t_now)
            .returning(Reservation.id)
        )
        .scalars()
        .all()
    )
    if expired:
        void_pending_payments(session, expired, t_now)
    return len(expired)


def expire_stale_holds_for_seat(session: Session, seat_id: int, t_now: datetime) -> int:
    """Lazy expiry: free a seat whose HELD row is past expires_at, under the seat lock.

    Needed because a stale HELD row still matches the partial unique index
    `WHERE status IN ('HELD','CONFIRMED')` until someone rewrites its status.
    """
    stale_ids = (
        session.execute(
            select(Reservation.id).where(
                Reservation.seat_id == seat_id,
                Reservation.status == ReservationStatus.HELD,
                Reservation.expires_at <= t_now,
            )
        )
        .scalars()
        .all()
    )
    return expire_reservations(session, list(stale_ids), t_now)


def void_pending_payments(session: Session, reservation_ids: list, t_now: datetime) -> int:
    return session.execute(
        update(PaymentAttempt)
        .where(
            PaymentAttempt.reservation_id.in_(reservation_ids),
            PaymentAttempt.status == PaymentStatus.PENDING,
        )
        .values(status=PaymentStatus.VOIDED, completed_at=t_now, updated_at=t_now)
    ).rowcount


# ---------------------------------------------------------------------------
# Derived availability (read-only, no locks)
# ---------------------------------------------------------------------------

_SEATS_SQL = text(
    """
    SELECT s.id AS seat_id, s.seat_number, r.status AS r_status, r.expires_at
    FROM seats s
    LEFT JOIN reservations r
           ON r.seat_id = s.id
          AND (r.status = 'CONFIRMED' OR (r.status = 'HELD' AND r.expires_at > :t_now))
    WHERE s.event_id = :event_id
      AND (CAST(:seat_number AS INTEGER) IS NULL OR s.seat_number = CAST(:seat_number AS INTEGER))
      AND (CAST(:available_only AS BOOLEAN) = FALSE OR r.id IS NULL)
    ORDER BY s.seat_number
    """
)

_SUMMARY_SQL = text(
    """
    SELECT e.id,
           COUNT(s.id) AS total,
           COUNT(r.id) FILTER (WHERE r.status = 'HELD') AS held,
           COUNT(r.id) FILTER (WHERE r.status = 'CONFIRMED') AS confirmed
    FROM events e
    LEFT JOIN seats s ON s.event_id = e.id
    LEFT JOIN reservations r
           ON r.seat_id = s.id
          AND (r.status = 'CONFIRMED' OR (r.status = 'HELD' AND r.expires_at > :t_now))
    WHERE (CAST(:event_id AS BIGINT) IS NULL OR e.id = CAST(:event_id AS BIGINT))
    GROUP BY e.id
    """
)


def _seat_dict(row) -> dict:
    status = row.r_status or "AVAILABLE"
    return {
        "seat_id": row.seat_id,
        "seat_number": row.seat_number,
        "status": status,
        "is_available": status == "AVAILABLE",
        "held_until": row.expires_at if status == "HELD" else None,
    }


def require_event(session: Session, event_id: int) -> Event:
    event = session.get(Event, event_id)
    if event is None:
        raise NotFound(f"Event {event_id} does not exist.", code="EVENT_NOT_FOUND")
    return event


def list_seats(session: Session, event_id: int, available_only: bool = False) -> dict:
    require_event(session, event_id)
    t_now = db_now(session)
    rows = session.execute(
        _SEATS_SQL,
        {
            "event_id": event_id,
            "t_now": t_now,
            "seat_number": None,
            "available_only": available_only,
        },
    ).all()
    seats = [_seat_dict(r) for r in rows]
    return {"event_id": event_id, "as_of": t_now, "count": len(seats), "seats": seats}


def get_seat(session: Session, event_id: int, seat_number: int) -> dict:
    require_event(session, event_id)
    t_now = db_now(session)
    row = session.execute(
        _SEATS_SQL,
        {"event_id": event_id, "t_now": t_now, "seat_number": seat_number, "available_only": False},
    ).first()
    if row is None:
        raise NotFound(
            f"Seat {seat_number} does not exist for event {event_id}.", code="SEAT_NOT_FOUND"
        )
    return _seat_dict(row)


def _event_dict(event: Event, summary_row) -> dict:
    total = summary_row.total if summary_row else 0
    held = summary_row.held if summary_row else 0
    confirmed = summary_row.confirmed if summary_row else 0
    return {
        "id": event.id,
        "name": event.name,
        "total_seats": event.total_seats,
        "starts_at": event.starts_at,
        "seat_summary": {
            "total": total,
            "available": total - held - confirmed,
            "held": held,
            "confirmed": confirmed,
        },
    }


def list_events(session: Session) -> dict:
    t_now = db_now(session)
    events = session.execute(select(Event).order_by(Event.id)).scalars().all()
    summaries = {
        r.id: r for r in session.execute(_SUMMARY_SQL, {"t_now": t_now, "event_id": None}).all()
    }
    return {"as_of": t_now, "events": [_event_dict(e, summaries.get(e.id)) for e in events]}


def get_event(session: Session, event_id: int) -> dict:
    event = require_event(session, event_id)
    t_now = db_now(session)
    row = session.execute(_SUMMARY_SQL, {"t_now": t_now, "event_id": event_id}).first()
    return _event_dict(event, row)
