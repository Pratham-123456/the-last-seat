"""Shared pytest setup.

SAFETY: before anything else, this file points the app at TEST_DATABASE_URL and
aborts the whole run unless that URL's database is exactly `last_seat_test_db`.
The destructive cleanup fixture additionally asks the *server* which database it
is connected to before truncating anything.
"""

import os
import socket
import threading
import time

import pytest

from app.core.config import TEST_DATABASE, Settings, database_name, get_settings

_TEST_URL = os.environ.get("TEST_DATABASE_URL") or Settings().test_database_url
if not _TEST_URL:
    pytest.exit("TEST_DATABASE_URL is not configured (.env or environment).", returncode=2)
if database_name(_TEST_URL) != TEST_DATABASE:
    pytest.exit(
        f"Refusing to run tests against {database_name(_TEST_URL)!r}; "
        f"TEST_DATABASE_URL must target {TEST_DATABASE!r}.",
        returncode=2,
    )
os.environ["DATABASE_URL"] = _TEST_URL  # the app under test now uses the test DB
get_settings.cache_clear()

DEMO_EVENT_ID = 1
DEMO_SEATS = 500


@pytest.fixture(scope="session")
def migrated_db():
    """Apply Alembic migrations (additive) and seed the demo event in the test DB."""
    from alembic import command
    from alembic.config import Config

    from app.core.config import PROJECT_ROOT
    from app.db.session import new_session
    from app.services.seed_service import seed_event

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    with new_session() as s:
        seed_event(s)
    yield


def _assert_connected_to_test_db(session) -> None:
    from sqlalchemy import text

    actual = session.execute(text("SELECT current_database()")).scalar_one()
    if actual != TEST_DATABASE:
        raise RuntimeError(f"SAFETY ABORT: connected to {actual!r}, not {TEST_DATABASE!r}")


@pytest.fixture
def clean_db(migrated_db):
    """Empty the mutable tables (test DB only) so each test starts from 500 free seats."""
    from sqlalchemy import text

    from app.db.session import new_session

    with new_session() as s:
        _assert_connected_to_test_db(s)
        s.execute(
            text("TRUNCATE payment_attempts, reservations, idempotency_records RESTART IDENTITY")
        )
        s.execute(text("DELETE FROM events WHERE id <> :id"), {"id": DEMO_EVENT_ID})
        s.commit()
    yield


@pytest.fixture
def db(clean_db):
    """A plain session for asserting directly against PostgreSQL."""
    from app.db.session import new_session

    with new_session() as s:
        yield s


@pytest.fixture
def client(clean_db):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def short_ttl(monkeypatch):
    """Make new holds expire after N seconds: `short_ttl(1)`."""

    def _set(seconds: int):
        monkeypatch.setattr(get_settings(), "hold_ttl_seconds", seconds)

    return _set


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server(migrated_db):
    """A real uvicorn server in a background thread: real HTTP, real thread pool."""
    import uvicorn

    from app.main import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
