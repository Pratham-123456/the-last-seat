"""Standalone background worker that proactively expires abandoned holds.

Correctness does NOT depend on this worker (requests expire stale holds lazily
under the seat lock). The worker just keeps the table tidy and seat counts honest.

Each cycle is one short transaction:
    1. find up to N candidate seats with a past-due HELD reservation (no locks)
    2. lock those seats in ascending id order, SKIP LOCKED  (same lock order as the API)
    3. HELD -> EXPIRED for the locked seats + void their PENDING payments
    4. COMMIT, then sleep
"""

import logging
import signal
import threading

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import Reservation, ReservationStatus, Seat
from app.services.seat_service import db_now, void_pending_payments

log = logging.getLogger("last_seat.expiry_worker")


def expire_batch(session: Session, batch_size: int = 100) -> int:
    """Run one expiry cycle and COMMIT. Returns how many reservations were expired."""
    try:
        candidate_seat_ids = (
            session.execute(
                select(Reservation.seat_id)
                .where(
                    Reservation.status == ReservationStatus.HELD,
                    Reservation.expires_at <= func.clock_timestamp(),
                )
                .order_by(Reservation.seat_id)
                .limit(batch_size)
            )
            .scalars()
            .all()
        )
        if not candidate_seat_ids:
            session.commit()
            return 0

        # Lock parents first, ascending, skipping seats an API request is busy with.
        locked_seat_ids = (
            session.execute(
                select(Seat.id)
                .where(Seat.id.in_(candidate_seat_ids))
                .order_by(Seat.id)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        if not locked_seat_ids:
            session.commit()
            return 0

        t_now = db_now(session)  # taken after the locks
        expired_ids = (
            session.execute(
                update(Reservation)
                .where(
                    Reservation.seat_id.in_(locked_seat_ids),
                    Reservation.status == ReservationStatus.HELD,
                    Reservation.expires_at <= t_now,
                )
                .values(status=ReservationStatus.EXPIRED, updated_at=t_now)
                .returning(Reservation.id)
                .execution_options(synchronize_session=False)
            )
            .scalars()
            .all()
        )
        voided = void_pending_payments(session, list(expired_ids), t_now) if expired_ids else 0
        session.commit()
        if expired_ids:
            log.info("expired %d hold(s), voided %d pending payment(s)", len(expired_ids), voided)
        return len(expired_ids)
    except Exception:
        session.rollback()
        raise


def run_forever(
    session_factory,
    *,
    interval_seconds: float,
    batch_size: int,
    stop_event: threading.Event | None = None,
) -> None:
    stop_event = stop_event or threading.Event()
    log.info("expiry worker started (interval=%ss, batch=%d)", interval_seconds, batch_size)
    while not stop_event.is_set():
        try:
            with session_factory() as session:
                expired = expire_batch(session, batch_size)
        except Exception:  # keep the worker alive; next cycle retries
            log.exception("expiry cycle failed")
            expired = 0
        if expired >= batch_size:
            continue  # backlog: drain immediately
        stop_event.wait(interval_seconds)
    log.info("expiry worker stopped")


def install_signal_handlers(stop_event: threading.Event) -> None:
    def _stop(signum, _frame):
        log.info("received signal %s, finishing current cycle", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)


def main() -> None:
    from app.core.config import get_settings
    from app.db.session import dispose_engine, new_session

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = get_settings()
    stop = threading.Event()
    install_signal_handlers(stop)
    try:
        run_forever(
            new_session,
            interval_seconds=settings.expiry_worker_interval_seconds,
            batch_size=settings.expiry_worker_batch_size,
            stop_event=stop,
        )
    finally:
        dispose_engine()


if __name__ == "__main__":
    main()
