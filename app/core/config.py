"""Typed application settings loaded from environment variables / .env.

Environment variables always win over values in .env, which is how the test
suite redirects the app to `last_seat_test_db`.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The ONLY databases this project is ever allowed to connect to.
ALLOWED_DATABASES = frozenset({"last_seat_db", "last_seat_test_db"})
TEST_DATABASE = "last_seat_test_db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8-sig", extra="ignore"
    )

    database_url: str = Field(
        default="postgresql+psycopg://last_seat_app:change-me@localhost:5432/last_seat_db"
    )
    test_database_url: str | None = None

    hold_ttl_seconds: int = Field(default=300, ge=1, le=3600)
    expiry_worker_interval_seconds: float = Field(default=5.0, gt=0)
    expiry_worker_batch_size: int = Field(default=100, ge=1, le=10_000)

    db_pool_size: int = Field(default=20, ge=1)
    db_max_overflow: int = Field(default=30, ge=0)
    # Max concurrent sync request handlers per API process (AnyIO thread limiter).
    api_thread_limit: int = Field(default=50, ge=1)
    # A request that waits longer than this for a row lock fails fast (503) instead of hanging.
    db_lock_timeout_ms: int = Field(default=10_000, ge=0)
    db_statement_timeout_ms: int = Field(default=30_000, ge=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def database_name(url: str) -> str:
    return make_url(url).database or ""


def assert_project_database(url: str, *, allowed: frozenset[str] = ALLOWED_DATABASES) -> str:
    """Abort unless `url` points at one of the project's dedicated databases.

    This is the guard that protects every other database on the server: migrations,
    seeding, the API, the worker and the test fixtures all call it before connecting.
    """
    name = database_name(url)
    if name not in allowed:
        raise RuntimeError(
            f"Refusing to use database {name!r}: only {sorted(allowed)} are allowed "
            "for this project."
        )
    return name
