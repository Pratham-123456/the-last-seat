from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.schemas import CUSTOMER_REF_PATTERN, BookingHistoryResponse
from app.services import reservation_service

router = APIRouter(prefix="/api/v1/customers", tags=["booking history"])


@router.get(
    "/{customer_ref}/bookings",
    response_model=BookingHistoryResponse,
    summary="All reservations (any status) for a customer, newest first",
)
def booking_history(
    customer_ref: str = Path(min_length=1, max_length=128, pattern=CUSTOMER_REF_PATTERN),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_db),
):
    return reservation_service.list_customer_bookings(session, customer_ref, limit)
