"""SQLAlchemy 2.x ORM models. Each class maps to exactly one PostgreSQL table.

The schema itself is created by the Alembic migration in migrations/versions/;
these classes mirror it so Python code can query it with type hints.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ReservationStatus(StrEnum):
    HELD = "HELD"
    CONFIRMED = "CONFIRMED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class PaymentType(StrEnum):
    CHARGE = "CHARGE"
    REFUND = "REFUND"


class PaymentScenario(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    DELAYED = "DELAYED"
    REFUND = "REFUND"


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    VOIDED = "VOIDED"


ACTIVE_STATUSES = (ReservationStatus.HELD, ReservationStatus.CONFIRMED)

TZ = DateTime(timezone=True)  # -> TIMESTAMPTZ


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (CheckConstraint("total_seats > 0", name="total_seats_positive"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    total_seats: Mapped[int] = mapped_column(Integer)
    starts_at: Mapped[datetime] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    seats: Mapped[list["Seat"]] = relationship(back_populates="event")


class Seat(Base):
    """Immutable inventory. Deliberately has NO `is_available` column: availability is
    derived from `reservations`, and this row is used as the per-seat mutex (FOR UPDATE)."""

    __tablename__ = "seats"
    __table_args__ = (
        UniqueConstraint("event_id", "seat_number", name="uq_seats_event_id_seat_number"),
        CheckConstraint("seat_number >= 1", name="seat_number_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    seat_number: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    event: Mapped[Event] = relationship(back_populates="seats")


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('HELD', 'CONFIRMED', 'EXPIRED', 'CANCELLED')", name="status_valid"
        ),
        # THE double-booking guarantee: at most one HELD/CONFIRMED row per seat.
        Index(
            "uq_reservations_one_active_per_seat",
            "seat_id",
            unique=True,
            postgresql_where=text("status IN ('HELD', 'CONFIRMED')"),
        ),
        Index("ix_reservations_seat_status", "seat_id", "status"),
        Index(
            "ix_reservations_expiry_scan",
            "status",
            "expires_at",
            postgresql_where=text("status = 'HELD'"),
        ),
        Index("ix_reservations_customer_created", "customer_ref", text("created_at DESC")),
        Index("ix_reservations_event_status", "event_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    seat_id: Mapped[int] = mapped_column(ForeignKey("seats.id"))
    customer_ref: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(TZ)
    expires_at: Mapped[datetime] = mapped_column(TZ)
    confirmed_at: Mapped[datetime | None] = mapped_column(TZ)
    cancelled_at: Mapped[datetime | None] = mapped_column(TZ)
    updated_at: Mapped[datetime] = mapped_column(TZ)

    seat: Mapped[Seat] = relationship()
    payments: Mapped[list["PaymentAttempt"]] = relationship(
        back_populates="reservation", order_by="PaymentAttempt.attempt_number"
    )


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"
    __table_args__ = (
        CheckConstraint("payment_type IN ('CHARGE', 'REFUND')", name="payment_type_valid"),
        CheckConstraint(
            "scenario IN ('SUCCESS', 'FAILURE', 'DELAYED', 'REFUND')", name="scenario_valid"
        ),
        CheckConstraint(
            "status IN ('PENDING', 'SUCCEEDED', 'FAILED', 'VOIDED')", name="status_valid"
        ),
        UniqueConstraint(
            "reservation_id",
            "attempt_number",
            name="uq_payment_attempts_reservation_id_attempt_number",
        ),
        # At most one successful charge per reservation: makes double-charging impossible.
        Index(
            "uq_payment_one_succeeded_charge_per_reservation",
            "reservation_id",
            unique=True,
            postgresql_where=text("payment_type = 'CHARGE' AND status = 'SUCCEEDED'"),
        ),
        Index("ix_payment_attempts_reservation_created", "reservation_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reservation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reservations.id"))
    attempt_number: Mapped[int] = mapped_column(Integer)
    payment_type: Mapped[str] = mapped_column(String(20))
    scenario: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    related_payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payment_attempts.id"))
    created_at: Mapped[datetime] = mapped_column(TZ)
    completed_at: Mapped[datetime | None] = mapped_column(TZ)
    updated_at: Mapped[datetime] = mapped_column(TZ)

    reservation: Mapped[Reservation] = relationship(back_populates="payments")


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "scope", "idempotency_key", name="uq_idempotency_records_scope_idempotency_key"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    scope: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


__all__ = [
    "ACTIVE_STATUSES",
    "Event",
    "IdempotencyRecord",
    "PaymentAttempt",
    "PaymentScenario",
    "PaymentStatus",
    "PaymentType",
    "Reservation",
    "ReservationStatus",
    "Seat",
]
