"""Engine (connection pool) and Session factory.

One `Session` == one unit of work == one PostgreSQL transaction at a time.
SQLAlchemy "autobegins" a transaction on the first statement; our services end it
explicitly with `session.commit()` or `session.rollback()`.
"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import assert_project_database, get_settings

_engine: Engine | None = None

# autoflush: pending ORM changes are sent to PostgreSQL before every query in the
# same transaction. expire_on_commit=False: objects stay readable after COMMIT.
SessionLocal = sessionmaker(expire_on_commit=False)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        assert_project_database(settings.database_url)
        _engine = create_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            pool_timeout=30,
            # Server-side safety nets: never wait forever for a lock or a query.
            connect_args={
                "options": (
                    f"-c lock_timeout={settings.db_lock_timeout_ms} "
                    f"-c statement_timeout={settings.db_statement_timeout_ms} "
                    "-c timezone=UTC"
                ),
                "application_name": "the-last-seat",
            },
        )
    return _engine


def new_session() -> Session:
    return SessionLocal(bind=get_engine())


def dispose_engine() -> None:
    """Close every pooled connection (used on shutdown and by the 'restart' test)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None
