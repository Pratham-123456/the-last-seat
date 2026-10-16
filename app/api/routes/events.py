from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.api.routes import ERROR_RESPONSES
from app.schemas import EventListResponse, EventOut, SeatListResponse, SeatOut
from app.services import seat_service

router = APIRouter(prefix="/api/v1/events", tags=["events & seats"])


@router.get("", response_model=EventListResponse, summary="List events with live seat counts")
def list_events(session: Session = Depends(get_db)):
    return seat_service.list_events(session)


@router.get(
    "/{event_id}",
    response_model=EventOut,
    responses=ERROR_RESPONSES,
    summary="Event details with live seat summary",
)
def get_event(event_id: int = Path(ge=1), session: Session = Depends(get_db)):
    return seat_service.get_event(session, event_id)


@router.get(
    "/{event_id}/seats",
    response_model=SeatListResponse,
    responses=ERROR_RESPONSES,
    summary="All seats with derived availability",
)
def list_seats(
    event_id: int = Path(ge=1),
    available_only: bool = Query(False, description="Only return AVAILABLE seats"),
    session: Session = Depends(get_db),
):
    return seat_service.list_seats(session, event_id, available_only)


@router.get(
    "/{event_id}/seats/{seat_number}",
    response_model=SeatOut,
    responses=ERROR_RESPONSES,
    summary="One seat's current derived status",
)
def get_seat(
    event_id: int = Path(ge=1), seat_number: int = Path(ge=1), session: Session = Depends(get_db)
):
    return seat_service.get_seat(session, event_id, seat_number)
