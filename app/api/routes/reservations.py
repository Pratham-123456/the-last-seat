import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, idempotency_key
from app.api.routes import ERROR_RESPONSES
from app.core.config import get_settings
from app.schemas import (
    ConfirmRequest,
    ErrorResponse,
    ReservationActionResponse,
    ReservationCreate,
    ReservationOut,
)
from app.services import payment_service, reservation_service
from app.services.idempotency_service import run_mutation

router = APIRouter(prefix="/api/v1/reservations", tags=["reservations"])


@router.post(
    "",
    status_code=201,
    response_model=ReservationActionResponse,
    responses=ERROR_RESPONSES,
    summary="Temporarily hold a seat (status HELD, expires after HOLD_TTL_SECONDS)",
)
def create_reservation(
    payload: ReservationCreate,
    session: Session = Depends(get_db),
    key: str | None = Depends(idempotency_key),
):
    ttl = get_settings().hold_ttl_seconds
    return run_mutation(
        session,
        scope="POST:/api/v1/reservations",
        idempotency_key=key,
        path_params={},
        body=payload.model_dump(mode="json"),
        operation=lambda: reservation_service.create_reservation(session, payload, ttl),
    ).to_response()


@router.get(
    "/{reservation_id}",
    response_model=ReservationOut,
    responses=ERROR_RESPONSES,
    summary="Reservation status, expiry and payment history",
)
def get_reservation(reservation_id: uuid.UUID, session: Session = Depends(get_db)):
    return reservation_service.get_reservation(session, reservation_id)


@router.post(
    "/{reservation_id}/confirm",
    response_model=ReservationActionResponse,
    responses={
        **ERROR_RESPONSES,
        202: {"model": ReservationActionResponse, "description": "DELAYED: payment pending"},
        402: {"model": ErrorResponse, "description": "FAILURE: payment failed, hold kept"},
    },
    summary="Pay for a held seat with a simulated SUCCESS / FAILURE / DELAYED payment",
)
def confirm_reservation(
    reservation_id: uuid.UUID,
    payload: ConfirmRequest,
    session: Session = Depends(get_db),
    key: str | None = Depends(idempotency_key),
):
    return run_mutation(
        session,
        scope="POST:/api/v1/reservations/{reservation_id}/confirm",
        idempotency_key=key,
        path_params={"reservation_id": str(reservation_id)},
        body=payload.model_dump(mode="json"),
        operation=lambda: payment_service.confirm_reservation(
            session, reservation_id, payload.scenario
        ),
    ).to_response()


@router.post(
    "/{reservation_id}/cancel",
    response_model=ReservationActionResponse,
    responses=ERROR_RESPONSES,
    summary="Cancel a HELD or CONFIRMED reservation (refund recorded) and release the seat",
)
def cancel_reservation(
    reservation_id: uuid.UUID,
    session: Session = Depends(get_db),
    key: str | None = Depends(idempotency_key),
):
    return run_mutation(
        session,
        scope="POST:/api/v1/reservations/{reservation_id}/cancel",
        idempotency_key=key,
        path_params={"reservation_id": str(reservation_id)},
        body=None,
        operation=lambda: reservation_service.cancel_reservation(session, reservation_id),
    ).to_response()
