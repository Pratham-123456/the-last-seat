"""Reservation lifecycle: hold, cancel, read, booking history.

Every mutating function here runs INSIDE the transaction opened by `run_mutation`
and never commits by itself; it returns (http_status, json_body) or raises a
DomainError. That keeps the idempotency record and the business change atomic.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.exceptions import Conflict, NotFound
from app.models import (
    ACTIVE_STATUSES,
    PaymentAttempt,
    PaymentScenario,
    PaymentStatus,
    PaymentType,
    Reservation,
    ReservationStatus,
)
from app.schemas import ReservationCreate
from app.services import seat_service
from app.services.serializers import action_body, reservation_dict, reservation_model

ACTIVE_INDEX = "uq_reservations_one_active_per_seat"


def _seat_unavailable(event_id: int, seat_number: int, active: Reservation | None) -> Conflict:
    details = {"event_id": event_id, "seat_number": seat_number}
    if active is not None:
        details["seat_status"] = active.status
        if active.status == ReservationStatus.HELD:
            details["held_until"] = active.expires_at.isoformat()
    return Conflict(
        f"Seat {seat_number} for this event is currently held or confirmed by another user.",
        code="SEAT_UNAVAILABLE",
        details=details,
    )


def create_reservation(
    session: Session, data: ReservationCreate, hold_ttl_seconds: int
) -> tuple[int, dict]:
    """Algorithm 1 (docs/concurrency_design.md Â§4)."""
    # 1. Per-seat mutex. Every competing transaction for this seat queues here.
    seat = seat_service.lock_seat_by_number(session, data.event_id, data.seat_number)

    # 2. Decision timestamp taken AFTER the lock (not the stale transaction-start now()).
    t_now = seat_service.db_now(session)

    # 3. Lazy expiry: a HELD row past expires_at must stop occupying the unique index.
    seat_service.expire_stale_holds_for_seat(session, seat.id, t_now)

    # 4. Is the seat still taken by a live hold or a confirmed booking?
    active = session.execute(
        select(Reservation).where(
            Reservation.seat_id == seat.id, Reservation.status.in_(ACTIVE_STATUSES)
        )
    ).scalar_one_or_none()
    if active is not None:
        raise _seat_unavailable(data.event_id, data.seat_number, active)

    # 5. Insert the hold. The SAVEPOINT lets us survive a unique-index violation
    #    (second line of defence; unreachable while the seat lock is respected).
    reservation = Reservation(
        id=uuid.uuid4(),
        event_id=data.event_id,
        seat_id=seat.id,
        customer_ref=data.customer_ref,
        status=ReservationStatus.HELD,
        created_at=t_now,
        expires_at=t_now + timedelta(seconds=hold_ttl_seconds),
        updated_at=t_now,
    )
    try:
        with session.begin_nested():
            session.add(reservation)
            session.flush()
    except IntegrityError as exc:
        if ACTIVE_INDEX in str(exc.orig):
            raise _seat_unavailable(data.event_id, data.seat_number, None) from exc
        raise

    return 201, action_body(
        session,
        result="HELD",
        message=f"Seat {data.seat_number} is held for you for {hold_ttl_seconds} seconds.",
        reservation=reservation,
        t_now=t_now,
        next_step=f"POST /api/v1/reservations/{reservation.id}/confirm before expires_at",
    )


def lock_reservation(session: Session, reservation_id: uuid.UUID) -> tuple[Reservation, datetime]:
    """Lock order when starting from a reservation id: seat FIRST, then the reservation.

    The unlocked read of seat_id is safe because a reservation's seat never changes.
    """
    seat_id = session.execute(
        select(Reservation.seat_id).where(Reservation.id == reservation_id)
    ).scalar_one_or_none()
    if seat_id is None:
        raise NotFound(
            f"Reservation {reservation_id} does not exist.", code="RESERVATION_NOT_FOUND"
        )
    seat_service.lock_seat_by_id(session, seat_id)
    t_now = seat_service.db_now(session)
    reservation = session.execute(
        select(Reservation)
        .where(Reservation.id == reservation_id)
        .with_for_update()
        .execution_options(populate_existing=True)  # re-read the latest committed version
    ).scalar_one()
    return reservation, t_now


def expire_one(session: Session, reservation: Reservation, t_now: datetime) -> None:
    reservation.status = ReservationStatus.EXPIRED
    reservation.updated_at = t_now
    session.flush()
    seat_service.void_pending_payments(session, [reservation.id], t_now)


def next_attempt_number(session: Session, reservation_id: uuid.UUID) -> int:
    # Safe without a sequence: we hold the seat lock, so no one else is numbering attempts.
    numbers = (
        session.execute(
            select(PaymentAttempt.attempt_number).where(
                PaymentAttempt.reservation_id == reservation_id
            )
        )
        .scalars()
        .all()
    )
    return max(numbers, default=0) + 1


def cancel_reservation(session: Session, reservation_id: uuid.UUID) -> tuple[int, dict]:
    """Algorithm 4: HELD/CONFIRMED -> CANCELLED (with refund), freeing the seat."""
    r, t_now = lock_reservation(session, reservation_id)

    if r.status == ReservationStatus.CANCELLED:
        return 200, action_body(
            session,
            result="ALREADY_CANCELLED",
            message="Reservation was already cancelled.",
            reservation=r,
            t_now=t_now,
        )

    if r.status == ReservationStatus.EXPIRED or (
        r.status == ReservationStatus.HELD and t_now >= r.expires_at
    ):
        if r.status == ReservationStatus.HELD:
            expire_one(session, r, t_now)  # committed by run_mutation: a real fact
        raise Conflict(
            "The hold had already expired, so there is nothing to cancel.",
            code="RESERVATION_ALREADY_EXPIRED",
            details={"reservation": reservation_dict(session, r, t_now)},
        )

    refund = None
    if r.status == ReservationStatus.HELD:
        seat_service.void_pending_payments(session, [r.id], t_now)
        message = "Hold cancelled; the seat is free for other students."
    else:  # CONFIRMED -> record a simulated refund linked to the original charge
        charge = session.execute(
            select(PaymentAttempt)
            .where(
                PaymentAttempt.reservation_id == r.id,
                PaymentAttempt.payment_type == PaymentType.CHARGE,
                PaymentAttempt.status == PaymentStatus.SUCCEEDED,
            )
            .with_for_update()
        ).scalar_one()
        refund = PaymentAttempt(
            id=uuid.uuid4(),
            reservation_id=r.id,
            attempt_number=next_attempt_number(session, r.id),
            payment_type=PaymentType.REFUND,
            scenario=PaymentScenario.REFUND,
            status=PaymentStatus.SUCCEEDED,
            related_payment_id=charge.id,
            created_at=t_now,
            completed_at=t_now,
            updated_at=t_now,
        )
        session.add(refund)
        message = "Booking cancelled and refunded; the seat is free for other students."

    r.status = ReservationStatus.CANCELLED
    r.cancelled_at = t_now
    r.updated_at = t_now
    session.flush()
    return 200, action_body(
        session, result="CANCELLED", message=message, reservation=r, t_now=t_now, payment=refund
    )


def get_reservation(session: Session, reservation_id: uuid.UUID) -> dict:
    r = session.get(Reservation, reservation_id)
    if r is None:
        raise NotFound(
            f"Reservation {reservation_id} does not exist.", code="RESERVATION_NOT_FOUND"
        )
    return reservation_dict(session, r, seat_service.db_now(session))


def list_customer_bookings(session: Session, customer_ref: str, limit: int = 100) -> dict:
    t_now = seat_service.db_now(session)
    rows = (
        session.execute(
            select(Reservation)
            .where(Reservation.customer_ref == customer_ref)
            .options(selectinload(Reservation.payments), selectinload(Reservation.seat))
            .order_by(Reservation.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    items = [
        reservation_model(session, r, t_now, payments=list(r.payments)).model_dump(mode="json")
        for r in rows
    ]
    return {
        "customer_ref": customer_ref,
        "as_of": t_now,
        "count": len(items),
        "reservations": items,
    }


__all__ = [
    "cancel_reservation",
    "create_reservation",
    "expire_one",
    "get_reservation",
    "list_customer_bookings",
    "lock_reservation",
    "next_attempt_number",
]
