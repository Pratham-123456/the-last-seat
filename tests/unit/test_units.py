"""Pure-Python unit tests: no database connection needed."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import assert_project_database
from app.core.exceptions import Conflict, IdempotencyMismatch, NotFound, PaymentFailed
from app.schemas import CallbackRequest, ConfirmRequest, ReservationCreate
from app.services.idempotency_service import canonical_json, request_fingerprint
from app.services.serializers import effective_status

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


# --- validation -------------------------------------------------------------


def test_reservation_payload_valid_and_stripped():
    r = ReservationCreate(event_id=1, seat_number=500, customer_ref="  22BCE1234 ")
    assert r.customer_ref == "22BCE1234"


@pytest.mark.parametrize(
    "payload",
    [
        {"event_id": 1, "seat_number": 0, "customer_ref": "a"},
        {"event_id": 0, "seat_number": 1, "customer_ref": "a"},
        {"event_id": 1, "seat_number": 1, "customer_ref": ""},
        {"event_id": 1, "seat_number": 1, "customer_ref": "   "},
        {"event_id": 1, "seat_number": 1, "customer_ref": "x" * 129},
        {"event_id": 1, "seat_number": 1, "customer_ref": "has space"},
        {"event_id": 1, "seat_number": 1, "customer_ref": "a", "status": "CONFIRMED"},
    ],
)
def test_reservation_payload_rejected(payload):
    with pytest.raises(ValidationError):
        ReservationCreate(**payload)


def test_payment_scenarios():
    for s in ("SUCCESS", "FAILURE", "DELAYED"):
        assert ConfirmRequest(scenario=s).scenario == s
    with pytest.raises(ValidationError):
        ConfirmRequest(scenario="REFUND")
    with pytest.raises(ValidationError):
        CallbackRequest(outcome="PENDING")


# --- idempotency fingerprint --------------------------------------------------


def test_canonical_json_ignores_key_order_and_whitespace():
    assert canonical_json({"b": 1, "a": [1, 2]}) == canonical_json({"a": [1, 2], "b": 1})
    assert canonical_json({"a": 1}) == '{"a":1}'


def test_fingerprint_is_sha256_and_sensitive_to_every_part():
    base = request_fingerprint("POST:/x", {"id": "1"}, {"seat": 1})
    assert len(base) == 64 and int(base, 16) >= 0
    assert base == request_fingerprint("POST:/x", {"id": "1"}, {"seat": 1})
    assert base != request_fingerprint("POST:/y", {"id": "1"}, {"seat": 1})
    assert base != request_fingerprint("POST:/x", {"id": "2"}, {"seat": 1})
    assert base != request_fingerprint("POST:/x", {"id": "1"}, {"seat": 2})


# --- state machine ----------------------------------------------------------


def _r(status, expires_in):
    return SimpleNamespace(status=status, expires_at=NOW + timedelta(seconds=expires_in))


@pytest.mark.parametrize(
    "status,expires_in,expected",
    [
        ("HELD", 10, "HELD"),
        ("HELD", 0, "EXPIRED"),  # boundary: t_now == expires_at is already expired
        ("HELD", -1, "EXPIRED"),
        ("CONFIRMED", -100, "CONFIRMED"),  # confirmed bookings never expire
        ("CANCELLED", 10, "CANCELLED"),
        ("EXPIRED", 10, "EXPIRED"),
    ],
)
def test_effective_status(status, expires_in, expected):
    assert effective_status(_r(status, expires_in), NOW) == expected


def test_domain_errors_map_to_http_and_envelope():
    assert NotFound("x").status_code == 404
    assert Conflict("x", code="SEAT_UNAVAILABLE").to_body() == {
        "error": {"code": "SEAT_UNAVAILABLE", "message": "x", "details": None}
    }
    assert PaymentFailed("x").status_code == 402
    assert IdempotencyMismatch("x").code == "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"


# --- database target guard ----------------------------------------------------


@pytest.mark.parametrize("name", ["last_seat_db", "last_seat_test_db"])
def test_guard_allows_project_databases(name):
    assert assert_project_database(f"postgresql+psycopg://u:p@localhost:5432/{name}") == name


@pytest.mark.parametrize("name", ["postgres", "university_dbms", "last_seat_db_old", ""])
def test_guard_refuses_any_other_database(name):
    with pytest.raises(RuntimeError, match="Refusing"):
        assert_project_database(f"postgresql+psycopg://u:p@localhost:5432/{name}")
