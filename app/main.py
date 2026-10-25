"""FastAPI application factory: routers, lifespan, and the standard error envelope."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import anyio.to_thread
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from psycopg import errors as pg_errors
from sqlalchemy.exc import DBAPIError, IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import bookings, events, health, payments, reservations
from app.core.config import get_settings
from app.core.exceptions import DomainError, error_body
from app.db.session import dispose_engine, get_engine

log = logging.getLogger("last_seat.api")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Transient contention errors: the client should simply retry (ideally with its Idempotency-Key).
RETRYABLE_DB_ERRORS = (
    pg_errors.LockNotAvailable  # lock_timeout exceeded
    | pg_errors.DeadlockDetected
    | pg_errors.QueryCanceled  # statement_timeout exceeded
    | pg_errors.SerializationFailure
)

DESCRIPTION = """
Concurrency-safe ticket booking backend for **The Last Seat** (CodeChef-VIT 2026-27).

* Seats are locked with `SELECT ... FOR UPDATE`; a partial unique index makes double booking
  impossible at the storage level.
* Holds expire after `HOLD_TTL_SECONDS` (lazily under the seat lock, and by a background worker).
* Payments are simulated: `SUCCESS`, `FAILURE`, `DELAYED` (+ `simulate-callback`).
* Every POST accepts an optional `Idempotency-Key` header for safe retries.
"""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_engine()  # validates DATABASE_URL targets a project database at startup
    # Sync endpoints run in AnyIO's worker-thread pool; size it to our connection pool.
    anyio.to_thread.current_default_thread_limiter().total_tokens = get_settings().api_thread_limit
    yield
    dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="The Last Seat", version="1.0.0", description=DESCRIPTION, lifespan=lifespan
    )

    @app.exception_handler(DomainError)
    async def domain_error_handler(_req: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_body())

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_req: Request, exc: RequestValidationError):
        details = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_body("VALIDATION_ERROR", "Request validation failed.", details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_req: Request, exc: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(status_code=exc.status_code, content=error_body(code, str(exc.detail)))

    @app.exception_handler(DBAPIError)
    async def db_error_handler(_req: Request, exc: DBAPIError):
        orig = exc.orig
        if isinstance(orig, RETRYABLE_DB_ERRORS):
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "1"},
                content=error_body(
                    "DATABASE_BUSY",
                    "The database was too busy to finish this request; retry it "
                    "(with the same Idempotency-Key).",
                ),
            )
        if isinstance(exc, IntegrityError):
            constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
            return JSONResponse(
                status_code=409,
                content=error_body(
                    "CONSTRAINT_VIOLATION",
                    "The request violates a database constraint.",
                    {"constraint": constraint},
                ),
            )
        log.exception("database error")
        return JSONResponse(
            status_code=503,
            content=error_body("DATABASE_UNAVAILABLE", "A database error occurred."),
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(_req: Request, exc: Exception):
        log.exception("unhandled error", exc_info=exc)
        return JSONResponse(
            status_code=500, content=error_body("INTERNAL_ERROR", "An unexpected error occurred.")
        )

    for module in (health, events, reservations, payments, bookings):
        app.include_router(module.router)

    # Browser frontend: plain HTML/CSS/JS calling the JSON API above on the same origin.
    app.mount("/app", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/app/")

    return app


app = create_app()
