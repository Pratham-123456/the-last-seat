from collections.abc import Iterator

from fastapi import Header
from sqlalchemy.orm import Session

from app.core.exceptions import DomainError
from app.db.session import new_session


def get_db() -> Iterator[Session]:
    """One Session per HTTP request, always closed (connection returned to the pool)."""
    session = new_session()
    try:
        yield session
    finally:
        session.close()


class InvalidIdempotencyKey(DomainError):
    status_code = 422
    code = "INVALID_IDEMPOTENCY_KEY"


def idempotency_key(
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="Optional client-generated unique key (e.g. a UUID). Repeating a request "
        "with the same key and payload replays the original response instead of re-executing.",
    ),
) -> str | None:
    if idempotency_key is None:
        return None
    key = idempotency_key.strip()
    if not 1 <= len(key) <= 128 or not key.isprintable():
        raise InvalidIdempotencyKey("Idempotency-Key must be 1-128 printable characters.")
    return key
