"""SQL checks for the system invariants (docs/concurrency_design.md §7).

Each query returns the rows that VIOLATE an invariant, so a healthy database
returns 0 for every check. Used by the test-suite, the CLI and the load-test report.
Invariants 5, 8 and 10 are behavioural (about sequences of requests) and are proven
by dedicated tests rather than a static query.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

CHECKS: dict[str, str] = {
    "1_single_active_reservation_per_seat": """
        SELECT seat_id FROM reservations
        WHERE status IN ('HELD', 'CONFIRMED')
        GROUP BY seat_id HAVING COUNT(*) > 1
    """,
    "2_capacity_bound": """
        SELECT e.id FROM events e
        JOIN reservations r ON r.event_id = e.id AND r.status IN ('HELD', 'CONFIRMED')
        GROUP BY e.id, e.total_seats HAVING COUNT(*) > e.total_seats
    """,
    "3_confirmed_has_exactly_one_succeeded_charge": """
        SELECT r.id FROM reservations r
        LEFT JOIN payment_attempts p
               ON p.reservation_id = r.id AND p.payment_type = 'CHARGE' AND p.status = 'SUCCEEDED'
        WHERE r.status = 'CONFIRMED'
        GROUP BY r.id HAVING COUNT(p.id) <> 1
    """,
    "4_no_confirmation_at_or_after_expiry": """
        SELECT id FROM reservations
        WHERE confirmed_at IS NOT NULL AND confirmed_at >= expires_at
    """,
    "6_succeeded_charge_only_on_confirmed_or_cancelled": """
        SELECT p.id FROM payment_attempts p JOIN reservations r ON r.id = p.reservation_id
        WHERE p.payment_type = 'CHARGE' AND p.status = 'SUCCEEDED'
          AND r.status NOT IN ('CONFIRMED', 'CANCELLED')
    """,
    "6b_cancelled_after_charge_has_refund": """
        SELECT r.id FROM reservations r
        JOIN payment_attempts c ON c.reservation_id = r.id
             AND c.payment_type = 'CHARGE' AND c.status = 'SUCCEEDED'
        LEFT JOIN payment_attempts f ON f.related_payment_id = c.id AND f.payment_type = 'REFUND'
        WHERE r.status = 'CANCELLED' AND f.id IS NULL
    """,
    "7_no_charge_succeeded_after_expiry": """
        SELECT p.id FROM payment_attempts p JOIN reservations r ON r.id = p.reservation_id
        WHERE p.payment_type = 'CHARGE' AND p.status = 'SUCCEEDED'
          AND p.completed_at >= r.expires_at
    """,
    "9_no_pending_payment_on_inactive_reservation": """
        SELECT p.id FROM payment_attempts p JOIN reservations r ON r.id = p.reservation_id
        WHERE p.status = 'PENDING' AND r.status <> 'HELD'
    """,
    "9b_terminal_payment_has_completed_at": """
        SELECT id FROM payment_attempts
        WHERE status IN ('SUCCEEDED', 'FAILED', 'VOIDED') AND completed_at IS NULL
    """,
}


def check_invariants(session: Session) -> dict[str, int]:
    """Return {invariant_name: number_of_violating_rows}. All zeros == healthy."""
    result = {}
    for name, sql in CHECKS.items():
        result[name] = len(session.execute(text(sql)).all())
    session.rollback()  # read-only; end the transaction
    return result


def summary_counts(session: Session) -> dict:
    rows = session.execute(
        text("SELECT status, COUNT(*) FROM reservations GROUP BY status ORDER BY status")
    ).all()
    pay = session.execute(
        text(
            "SELECT payment_type || ':' || status, COUNT(*) FROM payment_attempts "
            "GROUP BY 1 ORDER BY 1"
        )
    ).all()
    idem = session.execute(text("SELECT COUNT(*) FROM idempotency_records")).scalar_one()
    session.rollback()
    return {"reservations": dict(rows), "payments": dict(pay), "idempotency_records": idem}
