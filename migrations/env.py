from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import assert_project_database, get_settings
from app.db.base import Base
import app.models  # noqa: F401  (registers tables on Base.metadata)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    # `alembic -x db_url=...` overrides; otherwise use the app's DATABASE_URL.
    url = context.get_x_argument(as_dictionary=True).get("db_url") or get_settings().database_url
    name = assert_project_database(url)  # aborts for any non-project database
    print(f"[alembic] target database: {name}")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
