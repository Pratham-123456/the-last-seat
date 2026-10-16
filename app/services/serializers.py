"""Turn ORM rows into JSON-ready dicts (validated through the Pydantic schemas).

Responses are stored verbatim in idempotency_records.response_body for replay,
so everything returned from a service must be JSON-serialisable.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PaymentAttempt, Reservation, ReservationStatus
from app.schemas import PaymentOut, ReservationActionResponse, ReservationOut


def effective_status(reservation: Reservation, t_now: datetime) -> str:
    if reservation.status == ReservationStatus.HELD and t_now >= reservation.expires_at:
        return ReservationStatus.EXPIRED
    return reservation.status


def payment_model(p: PaymentAttempt) -> PaymentOut:
    return PaymentOut(
        id=p.id,
        reservation_id=p.reservation_id,
        attempt_number=p.attempt_number,
        payment_type=p.payment_type,
        scenario=p.scenario,
        status=p.status,
        related_payment_id=p.related_payment_id,
        created_at=p.created_at,
        completed_at=p.completed_at,
        updated_at=p.updated_at,
    )


def payment_dict(p: PaymentAttempt) -> dict:
    return payment_model(p).model_dump(mode="json")


def load_payments(session: Session, reservation_id) -> list[PaymentAttempt]:
    return list(
        session.execute(
            select(PaymentAttempt)
            .where(PaymentAttempt.reservation_id == reservation_id)
            .order_by(PaymentAttempt.attempt_number)
            .execution_options(populate_existing=True)
        ).scalars()
    )


def reservation_model(
    session: Session,
    r: Reservation,
    t_now: datetime,
    payments: list[PaymentAttempt] | None = None,
) -> ReservationOut:
    if payments is None:
        payments = load_payments(session, r.id)
    eff = effective_status(r, t_now)
    remaining = (
        max((r.expires_at - t_now).total_seconds(), 0.0) if eff == ReservationStatus.HELD else None
    )
    return ReservationOut(
        id=r.id,
        event_id=r.event_id,
        seat_id=r.seat_id,
        seat_number=r.seat.seat_number,
        customer_ref=r.customer_ref,
        status=r.status,
        effective_status=eff,
        created_at=r.created_at,
        expires_at=r.expires_at,
        seconds_remaining=remaining,
        confirmed_at=r.confirmed_at,
        cancelled_at=r.cancelled_at,
        updated_at=r.updated_at,
        payments=[payment_model(p) for p in payments],
    )


def reservation_dict(session: Session, r: Reservation, t_now: datetime) -> dict:
    return reservation_model(session, r, t_now).model_dump(mode="json")


def action_body(
    session: Session,
    *,
    result: str,
    message: str,
    reservation: Reservation,
    t_now: datetime,
    payment: PaymentAttempt | None = None,
    next_step: str | None = None,
) -> dict:
    return ReservationActionResponse(
        result=result,
        message=message,
        reservation=reservation_model(session, reservation, t_now),
        payment=payment_model(payment) if payment is not None else None,
        next_step=next_step,
    ).model_dump(mode="json")
