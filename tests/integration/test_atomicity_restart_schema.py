import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.session import dispose_engine, new_session
from app.services import payment_service
from app.services.seed_service import seed_event
from tests.helpers import assert_all_invariants, confirm, count, hold


def test_crash_mid_confirmation_rolls_back_everything(db, monkeypatch):
    """Invariant 9: payment row is inserted, then the process 'crashes' before the
    reservation update. Nothing of that request may survive."""
    from fastapi.testclient import TestClient

    from app.main import app

    def boom(*_a, **_k):
        raise RuntimeError("simulated crash between payment insert and confirmation")

    with TestClient(app, raise_server_exceptions=False) as c:
        rid = hold(c, 70).json()["reservation"]["id"]
        monkeypatch.setattr(payment_service, "_mark_confirmed", boom)
        r = confirm(c, rid, "SUCCESS", key="crash-key")
        assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"
        assert "Traceback" not in r.text

        assert count(db, "SELECT COUNT(*) FROM payment_attempts") == 0
        assert c.get(f"/api/v1/reservations/{rid}").json()["status"] == "HELD"
        # The idempotency record rolled back too, so a retry can succeed.
        assert (
            count(
                db, "SELECT COUNT(*) FROM idempotency_records " "WHERE idempotency_key='crash-key'"
            )
            == 0
        )
        monkeypatch.undo()
        assert confirm(c, rid, "SUCCESS", key="crash-key").status_code == 200
    assert_all_invariants(db)


def test_restart_loses_no_state(db):
    """Invariant 10: all state lives in PostgreSQL. Drop every pooled connection and
    build a brand-new app instance; reservations, payments and idempotency survive."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        first = hold(c, 71, key="restart-key")
        rid = first.json()["reservation"]["id"]
        confirm(c, rid, "SUCCESS")
    dispose_engine()  # "process restart": nothing cached in memory survives

    with TestClient(create_app()) as c:
        assert c.get(f"/api/v1/reservations/{rid}").json()["status"] == "CONFIRMED"
        replay = hold(c, 71, key="restart-key")
        assert replay.status_code == 201 and replay.json() == first.json()
        assert replay.headers["Idempotent-Replayed"] == "true"
    assert count(db, "SELECT COUNT(*) FROM reservations") == 1


def test_migration_created_constraints_and_indexes(db):
    names = set(
        db.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname='public'")).scalars()
    )
    for expected in (
        "uq_reservations_one_active_per_seat",
        "uq_payment_one_succeeded_charge_per_reservation",
        "ix_reservations_expiry_scan",
        "ix_reservations_customer_created",
        "ix_reservations_seat_status",
        "ix_reservations_event_status",
        "ix_payment_attempts_reservation_created",
        "uq_seats_event_id_seat_number",
        "uq_idempotency_records_scope_idempotency_key",
    ):
        assert expected in names
    partial = db.execute(
        text(
            "SELECT indexdef FROM pg_indexes WHERE indexname='uq_reservations_one_active_per_seat'"
        )
    ).scalar_one()
    assert "UNIQUE" in partial and "WHERE" in partial
    assert "seats" in set(
        db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars()
    )
    cols = set(
        db.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name='seats'")
        ).scalars()
    )
    assert "is_available" not in cols and "available" not in cols
    db.rollback()


def _insert_reservation(s, seat_id, status):
    s.execute(
        text(
            "INSERT INTO reservations (id, event_id, seat_id, customer_ref, status, created_at, "
            "expires_at, updated_at) VALUES (:id, 1, :seat, 'X', :st, now(), now(), now())"
        ),
        {"id": uuid.uuid4(), "seat": seat_id, "st": status},
    )


def test_partial_unique_index_is_the_last_line_of_defence():
    """Even code that skips the seat lock cannot create a second active row."""
    with new_session() as s:
        seat_id = s.execute(
            text("SELECT id FROM seats WHERE event_id=1 AND seat_number=72")
        ).scalar_one()
        _insert_reservation(s, seat_id, "HELD")
        with pytest.raises(IntegrityError, match="uq_reservations_one_active_per_seat"):
            _insert_reservation(s, seat_id, "CONFIRMED")
        s.rollback()
        # ...while any number of historical rows is fine.
        for st in ("EXPIRED", "CANCELLED", "EXPIRED", "HELD"):
            _insert_reservation(s, seat_id, st)
        s.rollback()


def test_check_constraints_reject_invalid_states():
    with new_session() as s:
        seat_id = s.execute(
            text("SELECT id FROM seats WHERE event_id=1 AND seat_number=73")
        ).scalar_one()
        with pytest.raises(IntegrityError, match="ck_reservations_status_valid"):
            _insert_reservation(s, seat_id, "MAYBE")
        s.rollback()
        with pytest.raises(IntegrityError, match="ck_seats_seat_number_positive"):
            s.execute(text("INSERT INTO seats (event_id, seat_number) VALUES (1, 0)"))
        s.rollback()


def test_seed_is_idempotent(db):
    with new_session() as s:
        again = seed_event(s)
    assert again == {"event_id": 1, "event_created": False, "seats_inserted": 0, "seat_count": 500}
    with new_session() as s:
        first = seed_event(s, event_id=99, name="Extra", total_seats=5)
        second = seed_event(s, event_id=99, name="Extra", total_seats=5)
    assert first["event_created"] and first["seats_inserted"] == 5
    assert not second["event_created"] and second["seats_inserted"] == 0
    assert count(db, "SELECT COUNT(*) FROM events") == 2
