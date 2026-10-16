"""Domain errors: each maps to one HTTP status code + a stable machine-readable `code`.

Services raise these for *expected business outcomes* (seat taken, hold expired, ...).
They are deliberately NOT a rollback signal: state changes made before raising one
(e.g. marking a stale hold EXPIRED, recording a FAILED payment) are committed by
`run_mutation`, because they are real facts that must survive. Any *other* exception
rolls the whole transaction back.
"""

from typing import Any


class DomainError(Exception):
    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, *, details: Any = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details
        if code is not None:
            self.code = code

    def to_body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class NotFound(DomainError):
    status_code = 404
    code = "NOT_FOUND"


class Conflict(DomainError):
    status_code = 409
    code = "CONFLICT"


class PaymentFailed(DomainError):
    status_code = 402
    code = "PAYMENT_FAILED"


class IdempotencyMismatch(DomainError):
    """Raised before any business logic runs; always rolled back, never stored."""

    status_code = 409
    code = "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"


class ServiceUnavailable(DomainError):
    status_code = 503
    code = "SERVICE_UNAVAILABLE"


def error_body(code: str, message: str, details: Any = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}
