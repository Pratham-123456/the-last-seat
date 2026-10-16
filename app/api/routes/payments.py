import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, idempotency_key
from app.api.routes import ERROR_RESPONSES
from app.schemas import CallbackRequest, PaymentDetailResponse, ReservationActionResponse
from app.services import payment_service
from app.services.idempotency_service import run_mutation

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])


@router.get(
    "/{payment_id}",
    response_model=PaymentDetailResponse,
    responses=ERROR_RESPONSES,
    summary="Inspect one payment attempt",
)
def get_payment(payment_id: uuid.UUID, session: Session = Depends(get_db)):
    return payment_service.get_payment(session, payment_id)


@router.post(
    "/{payment_id}/simulate-callback",
    response_model=ReservationActionResponse,
    responses=ERROR_RESPONSES,
    summary="Simulate the payment gateway's webhook for a DELAYED (PENDING) payment",
)
def simulate_callback(
    payment_id: uuid.UUID,
    payload: CallbackRequest,
    session: Session = Depends(get_db),
    key: str | None = Depends(idempotency_key),
):
    return run_mutation(
        session,
        scope="POST:/api/v1/payments/{payment_id}/simulate-callback",
        idempotency_key=key,
        path_params={"payment_id": str(payment_id)},
        body=payload.model_dump(mode="json"),
        operation=lambda: payment_service.simulate_callback(session, payment_id, payload.outcome),
    ).to_response()
