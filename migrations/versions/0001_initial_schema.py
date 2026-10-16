"""Initial schema: events, seats, reservations, payment_attempts, idempotency_records.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("total_seats", sa.Integer, nullable=False),
        sa.Column("starts_at", TZ, nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("total_seats > 0", name="ck_events_total_seats_positive"),
    )

    op.create_table(
        "seats",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column(
            "event_id",
            sa.BigInteger,
            sa.ForeignKey("events.id", ondelete="CASCADE", name="fk_seats_event_id_events"),
            nullable=False,
        ),
        sa.Column("seat_number", sa.Integer, nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("event_id", "seat_number", name="uq_seats_event_id_seat_number"),
        sa.CheckConstraint("seat_number >= 1", name="ck_seats_seat_number_positive"),
    )

    op.create_table(
        "reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            sa.BigInteger,
            sa.ForeignKey("events.id", name="fk_reservations_event_id_events"),
            nullable=False,
        ),
        sa.Column(
            "seat_id",
            sa.BigInteger,
            sa.ForeignKey("seats.id", name="fk_reservations_seat_id_seats"),
            nullable=False,
        ),
        sa.Column("customer_ref", sa.String(128), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("expires_at", TZ, nullable=False),
        sa.Column("confirmed_at", TZ, nullable=True),
        sa.Column("cancelled_at", TZ, nullable=True),
        sa.Column("updated_at", TZ, nullable=False),
        sa.CheckConstraint(
            "status IN ('HELD', 'CONFIRMED', 'EXPIRED', 'CANCELLED')",
            name="ck_reservations_status_valid",
        ),
    )
    # The storage-level guarantee against double booking.
    op.create_index(
        "uq_reservations_one_active_per_seat",
        "reservations",
        ["seat_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('HELD', 'CONFIRMED')"),
    )
    op.create_index("ix_reservations_seat_status", "reservations", ["seat_id", "status"])
    op.create_index(
        "ix_reservations_expiry_scan",
        "reservations",
        ["status", "expires_at"],
        postgresql_where=sa.text("status = 'HELD'"),
    )
    op.create_index(
        "ix_reservations_customer_created",
        "reservations",
        ["customer_ref", sa.text("created_at DESC")],
    )
    op.create_index("ix_reservations_event_status", "reservations", ["event_id", "status"])

    op.create_table(
        "payment_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "reservation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "reservations.id", name="fk_payment_attempts_reservation_id_reservations"
            ),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer, nullable=False),
        sa.Column("payment_type", sa.String(20), nullable=False),
        sa.Column("scenario", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "related_payment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "payment_attempts.id",
                name="fk_payment_attempts_related_payment_id_payment_attempts",
            ),
            nullable=True,
        ),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("completed_at", TZ, nullable=True),
        sa.Column("updated_at", TZ, nullable=False),
        sa.CheckConstraint(
            "payment_type IN ('CHARGE', 'REFUND')", name="ck_payment_attempts_payment_type_valid"
        ),
        sa.CheckConstraint(
            "scenario IN ('SUCCESS', 'FAILURE', 'DELAYED', 'REFUND')",
            name="ck_payment_attempts_scenario_valid",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'SUCCEEDED', 'FAILED', 'VOIDED')",
            name="ck_payment_attempts_status_valid",
        ),
        sa.UniqueConstraint(
            "reservation_id",
            "attempt_number",
            name="uq_payment_attempts_reservation_id_attempt_number",
        ),
    )
    op.create_index(
        "uq_payment_one_succeeded_charge_per_reservation",
        "payment_attempts",
        ["reservation_id"],
        unique=True,
        postgresql_where=sa.text("payment_type = 'CHARGE' AND status = 'SUCCEEDED'"),
    )
    op.create_index(
        "ix_payment_attempts_reservation_created",
        "payment_attempts",
        ["reservation_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("scope", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer, nullable=True),
        sa.Column("response_body", postgresql.JSONB, nullable=True),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "scope", "idempotency_key", name="uq_idempotency_records_scope_idempotency_key"
        ),
    )


def downgrade() -> None:
    # DESTRUCTIVE: drops every project table. Never run without explicit approval.
    op.drop_table("idempotency_records")
    op.drop_table("payment_attempts")
    op.drop_table("reservations")
    op.drop_table("seats")
    op.drop_table("events")
