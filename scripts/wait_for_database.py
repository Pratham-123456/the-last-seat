"""Block until PostgreSQL accepts connections (used by Docker before migrating).

Refuses to run against any database other than the project databases.

    python scripts/wait_for_database.py [--timeout 60]
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import assert_project_database, get_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()

    url = get_settings().database_url
    name = assert_project_database(url)
    u = make_url(url)
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            with psycopg.connect(
                host=u.host,
                port=u.port or 5432,
                user=u.username,
                password=u.password,
                dbname=u.database,
                connect_timeout=3,
            ) as c:
                c.execute("SELECT 1")
            print(f"database {name!r} at {u.host}:{u.port or 5432} is ready")
            return 0
        except psycopg.OperationalError as exc:
            if time.monotonic() > deadline:
                print(f"gave up waiting for database: {exc}", file=sys.stderr)
                return 1
            print("waiting for database...", flush=True)
            time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
