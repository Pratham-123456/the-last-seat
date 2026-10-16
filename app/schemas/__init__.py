"""Pydantic v2 request/response models: the API's typed contract (shown in /docs)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CUSTOMER_REF_PATTERN = r"^[A-Za-z0-9_.@:\-]+$"


class ReservationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: int = Field(ge=1, examples=[1])
    seat_number: int = Field(ge=1, le=100_000, examples=[42])
    customer_ref: str = Field(
        min_length=1,
        max_length=128,
        pattern=CUSTOMER_REF_PATTERN,
        examples=["22BCE1234"],
        description="Demo caller identifier (e.g. a VIT registration number). Not authentication.",
    )

    @field_validator("customer_ref", mode="before")
    @classmethod
    def strip_customer_ref(cls, v):
        return v.strip() if isinstance(v, str) else v


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: Literal["SUCCESS", "FAILURE", "DELAYED"] = Field(
        description="Which simulated payment outcome to produce.", examples=["SUCCESS"]
    )


class CallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["SUCCEEDED", "FAILED"] = Field(examples=["SUCCEEDED"])


class PaymentOut(BaseModel):
    id: uuid.UUID
    reservation_id: uuid.UUID
    attempt_number: int
    payment_type: str
    scenario: str
    status: str
    related_payment_id: uuid.UUID | None
    created_at: datetime
    completed_at: datetime | None
    updated_at: datetime


class ReservationOut(BaseModel):
    id: uuid.UUID
    event_id: int
    seat_id: int
    seat_number: int
    customer_ref: str
    status: str = Field(description="Status stored in PostgreSQL.")
    effective_status: str = Field(
        description="Status as of now: a HELD row past expires_at is reported as EXPIRED "
        "even if the expiry worker has not rewritten it yet."
    )
    created_at: datetime
    expires_at: datetime
    seconds_remaining: float | None = Field(
        description="Seconds left on an active hold; null otherwise."
    )
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    updated_at: datetime
    payments: list[PaymentOut] = []


class ReservationActionResponse(BaseModel):
    result: str = Field(examples=["HELD", "CONFIRMED", "PAYMENT_PENDING", "CANCELLED"])
    message: str
    reservation: ReservationOut
    payment: PaymentOut | None = None
    next_step: str | None = None


class PaymentDetailResponse(BaseModel):
    payment: PaymentOut
    reservation: ReservationOut


class SeatOut(BaseModel):
    seat_id: int
    seat_number: int
    status: Literal["AVAILABLE", "HELD", "CONFIRMED"]
    is_available: bool
    held_until: datetime | None = None


class SeatSummary(BaseModel):
    total: int
    available: int
    held: int
    confirmed: int


class EventOut(BaseModel):
    id: int
    name: str
    total_seats: int
    starts_at: datetime
    seat_summary: SeatSummary


class EventListResponse(BaseModel):
    as_of: datetime
    events: list[EventOut]


class SeatListResponse(BaseModel):
    event_id: int
    as_of: datetime
    count: int
    seats: list[SeatOut]


class BookingHistoryResponse(BaseModel):
    customer_ref: str
    as_of: datetime
    count: int
    reservations: list[ReservationOut]


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: object | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
