"""Database-backed Idempotency-Key protocol + the single transaction wrapper.

`run_mutation` owns BEGIN/COMMIT/ROLLBACK for every POST endpoint:

    BEGIN
      [lock idempotency row]  -> replay stored response if already completed
      business operation      -> (status, body)  or  DomainError
      [store (status, body) in the idempotency row]
    COMMIT                    (any unexpected exception -> ROLLBACK of everything)
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.exceptions import Conflict, DomainError, IdempotencyMismatch
from app.models import IdempotencyRecord

REPLAY_HEADER = "Idempotent-Replayed"


def canonical_json(value: Any) -> str:
    """Stable text form: same data -> same string, regardless of key order/whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def request_fingerprint(scope: str, path_params: dict, body: Any) -> str:
    payload = {"scope": scope, "path": path_params, "body": body}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass
class MutationResult:
    status_code: int
    body: dict
    replayed: bool = False
    headers: dict = field(default_factory=dict)

    def to_response(self) -> JSONResponse:
        headers = dict(self.headers)
        if self.replayed:
            headers[REPLAY_HEADER] = "true"
        return JSONResponse(status_code=self.status_code, content=self.body, headers=headers)


def _acquire(session: Session, scope: str, key: str, request_hash: str) -> IdempotencyRecord:
    """INSERT ... ON CONFLICT DO NOTHING, then SELECT ... FOR UPDATE.

    If another transaction is processing the same (scope, key) right now, our INSERT
    waits for it on the unique index; once it commits we see its finished record.
    """
    inserted_id = session.execute(
        pg_insert(IdempotencyRecord)
        .values(
            scope=scope,
            idempotency_key=key,
            request_hash=request_hash,
            created_at=func.clock_timestamp(),
        )
        .on_conflict_do_nothing(constraint="uq_idempotency_records_scope_idempotency_key")
        .returning(IdempotencyRecord.id)
    ).scalar_one_or_none()
    record = session.execute(
        select(IdempotencyRecord)
        .where(IdempotencyRecord.scope == scope, IdempotencyRecord.idempotency_key == key)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if record.request_hash != request_hash:
        raise IdempotencyMismatch(
            "This Idempotency-Key was already used with a different request payload.",
            details={"scope": scope, "idempotency_key": key},
        )
    if inserted_id is None and record.response_status is None:
        # Unreachable in practice (we always store a response before COMMIT).
        raise Conflict(
            "A request with this Idempotency-Key is still in progress.",
            code="IDEMPOTENCY_REQUEST_IN_PROGRESS",
        )
    return record


def run_mutation(
    session: Session,
    *,
    scope: str,
    idempotency_key: str | None,
    path_params: dict,
    body: Any,
    operation: Callable[[], tuple[int, dict]],
) -> MutationResult:
    try:
        record = None
        if idempotency_key:
            record = _acquire(
                session, scope, idempotency_key, request_fingerprint(scope, path_params, body)
            )
            if record.response_status is not None:
                session.commit()  # releases the row lock; nothing changed
                return MutationResult(record.response_status, record.response_body, replayed=True)

        try:
            status_code, response_body = operation()
        except DomainError as err:
            if isinstance(err, IdempotencyMismatch):
                raise
            # Expected business outcome: keep the state changes it made, store the reply.
            status_code, response_body = err.status_code, err.to_body()

        if record is not None:
            record.response_status = status_code
            record.response_body = response_body
        session.commit()
        return MutationResult(status_code, response_body)
    except Exception:
        session.rollback()  # atomicity: nothing of this request survives
        raise
