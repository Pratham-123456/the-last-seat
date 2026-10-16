"""Simulated payments: SUCCESS / FAILURE / DELAYED (+ webhook-style callback).

No real gateway is called and nothing ever sleeps inside a transaction: a DELAYED
payment is just a PENDING row that a later callback request completes.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import Conflict, NotFound, PaymentFailed
from app.models import (
    PaymentAttempt,
    PaymentScenario,
    PaymentStatus,
    PaymentType,
    Reservation,
    ReservationStatus,
)
from app.services import seat_service
from app.services.reservation_service import expire_one, lock_reservation, next_attempt_number
from app.services.serializers import action_body, payment_dict, reservation_dict


def _mark_confirmed(reservation: Reservation, t_now: datetime) -> None:
    """HELD -> CONFIRMED. Separate function so tests can inject a failure here (atomicity)."""
    reservation.status = ReservationStatus.CONFIRMED
    reservation.confirmed_at = t_now
    reservation.updated_at = t_now


def _succeeded_charge(session: Session, reservation_id) -> PaymentAttempt | None:
    return session.execute(
        select(PaymentAttempt).where(
            PaymentAttempt.reservation_id == reservation_id,
            PaymentAttempt.payment_type == PaymentType.CHARGE,
            PaymentAttempt.status == PaymentStatus.SUCCEEDED,
        )
    ).scalar_one_or_none()


def confirm_reservation(
    session: Session, reservation_id: uuid.UUID, scenario: str
) -> tuple[int, dict]:
    """Algorithm 2: run one simulated payment attempt against a HELD reservation."""
    r, t_now = lock_reservation(session, reservation_id)  # seat lock -> reservation lock

    if r.status == ReservationStatus.CONFIRMED:
        # Policy: never charge twice. Report the existing booking with 200.
        return 200, action_body(
            session,
            result="ALREADY_CONFIRMED",
            message="Reservation is already confirmed; " "no new charge was made.",
            reservation=r,
            t_now=t_now,
            payment=_succeeded_charge(session, r.id),
        )
    if r.status == ReservationStatus.CANCELLED:
        raise Conflict(
            "Reservation was cancelled and cannot be confirmed.",
            code="RESERVATION_NOT_ACTIVE",
            details={"reservation": reservation_dict(session, r, t_now)},
        )
    if r.status == ReservationStatus.EXPIRED or t_now >= r.expires_at:
        if r.status == ReservationStatus.HELD:
            expire_one(session, r, t_now)  # persisted: frees the seat for others
        raise Conflict(
            "The hold expired before payment; the seat has been released.",
            code="RESERVATION_EXPIRED",
            details={"reservation": reservation_dict(session, r, t_now)},
        )

    pending = session.execute(
        select(PaymentAttempt)
        .where(
            PaymentAttempt.reservation_id == r.id, PaymentAttempt.status == PaymentStatus.PENDING
        )
        .with_for_update()
    ).scalar_one_or_none()
    if pending is not None:
        raise Conflict(
            "A delayed payment is still pending for this reservation.",
            code="PAYMENT_ALREADY_PENDING",
            details={
                "payment_id": str(pending.id),
                "callback": f"POST /api/v1/payments/{pending.id}/simulate-callback",
            },
        )

    payment = PaymentAttempt(
        id=uuid.uuid4(),
        reservation_id=r.id,
        attempt_number=next_attempt_number(session, r.id),
        payment_type=PaymentType.CHARGE,
        scenario=scenario,
        created_at=t_now,
        updated_at=t_now,
    )

    if scenario == PaymentScenario.SUCCESS:
        payment.status = PaymentStatus.SUCCEEDED
        payment.completed_at = t_now
        session.add(payment)
        session.flush()
        _mark_confirmed(r, t_now)
        session.flush()
        return 200, action_body(
            session,
            result="CONFIRMED",
            message="Payment succeeded; " "your seat is booked.",
            reservation=r,
            t_now=t_now,
            payment=payment,
        )

    if scenario == PaymentScenario.FAILURE:
        payment.status = PaymentStatus.FAILED
        payment.completed_at = t_now
        session.add(payment)
        session.flush()
        # Raised AFTER recording the attempt; run_mutation commits it (history is never lost).
        raise PaymentFailed(
            "Payment failed. Your hold is still active; you may retry before it expires.",
            details={
                "payment": payment_dict(payment),
                "reservation": reservation_dict(session, r, t_now),
                "retry_allowed": True,
            },
        )

    # DELAYED: record PENDING and return immediately. No sleeping inside a transaction!
    payment.status = PaymentStatus.PENDING
    session.add(payment)
    session.flush()
    return 202, action_body(
        session,
        result="PAYMENT_PENDING",
        message="Payment is processing. The hold stays active until expires_at.",
        reservation=r,
        t_now=t_now,
        payment=payment,
        next_step=f"POST /api/v1/payments/{payment.id}/simulate-callback "
        '{"outcome": "SUCCEEDED" | "FAILED"}',
    )


def _lock_payment(
    session: Session, payment_id: uuid.UUID
) -> tuple[PaymentAttempt, Reservation, datetime]:
    """Lock order from a payment id: seat -> reservation -> payment."""
    row = session.execute(
        select(PaymentAttempt.reservation_id).where(PaymentAttempt.id == payment_id)
    ).first()
    if row is None:
        raise NotFound(f"Payment {payment_id} does not exist.", code="PAYMENT_NOT_FOUND")
    r, t_now = lock_reservation(session, row.reservation_id)
    payment = session.execute(
        select(PaymentAttempt)
        .where(PaymentAttempt.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    return payment, r, t_now


def simulate_callback(session: Session, payment_id: uuid.UUID, outcome: str) -> tuple[int, dict]:
    """Algorithm 3: the 'gateway webhook' that completes a DELAYED payment."""
    payment, r, t_now = _lock_payment(session, payment_id)

    if payment.status in (PaymentStatus.SUCCEEDED, PaymentStatus.FAILED):
        return 200, action_body(
            session,
            result="CALLBACK_ALREADY_PROCESSED",
            message=f"Payment was already {payment.status}; nothing changed.",
            reservation=r,
            t_now=t_now,
            payment=payment,
        )

    if (
        payment.status == PaymentStatus.PENDING
        and r.status == ReservationStatus.HELD
        and t_now >= r.expires_at
    ):
        expire_one(session, r, t_now)  # also voids this PENDING payment
        session.refresh(payment)

    if payment.status == PaymentStatus.PENDING and r.status != ReservationStatus.HELD:
        # Defensive: an inactive reservation must never keep a live pending payment.
        payment.status = PaymentStatus.VOIDED
        payment.completed_at = t_now
        payment.updated_at = t_now
        session.flush()

    if payment.status == PaymentStatus.VOIDED:
        # CRITICAL INVARIANT: a late callback never revives an expired/cancelled hold.
        raise Conflict(
            "The payment arrived after the hold was no longer active and was voided; "
            "no booking was made and the seat may already belong to someone else.",
            code="LATE_PAYMENT_VOIDED",
            details={
                "payment": payment_dict(payment),
                "reservation": reservation_dict(session, r, t_now),
            },
        )

    # PENDING + reservation HELD and still inside its window.
    payment.completed_at = t_now
    payment.updated_at = t_now
    if outcome == "FAILED":
        payment.status = PaymentStatus.FAILED
        session.flush()
        return 200, action_body(
            session,
            result="PAYMENT_FAILED",
            message="Delayed payment failed; the hold is still active, retry "
            "allowed before expiry.",
            reservation=r,
            t_now=t_now,
            payment=payment,
        )

    payment.status = PaymentStatus.SUCCEEDED
    session.flush()
    _mark_confirmed(r, t_now)
    session.flush()
    return 200, action_body(
        session,
        result="CONFIRMED",
        message="Delayed payment succeeded; " "your seat is booked.",
        reservation=r,
        t_now=t_now,
        payment=payment,
    )


def get_payment(session: Session, payment_id: uuid.UUID) -> dict:
    payment = session.get(PaymentAttempt, payment_id)
    if payment is None:
        raise NotFound(f"Payment {payment_id} does not exist.", code="PAYMENT_NOT_FOUND")
    t_now = seat_service.db_now(session)
    return {
        "payment": payment_dict(payment),
        "reservation": reservation_dict(session, payment.reservation, t_now),
    }
