"""Command-line entry points.

python -m app.cli seed [--event-id N --name "..." --seats N]
python -m app.cli worker
python -m app.cli db-check
python -m app.cli invariants [--event-id N]
"""

import argparse
import json
import sys

from app.core.config import assert_project_database, database_name, get_settings


def _session():
    from app.db.session import new_session

    return new_session()


def cmd_seed(args) -> int:
    from app.services.seed_service import (
        DEMO_EVENT_ID,
        DEMO_EVENT_NAME,
        DEMO_TOTAL_SEATS,
        seed_event,
    )

    with _session() as s:
        result = seed_event(
            s,
            event_id=args.event_id or DEMO_EVENT_ID,
            name=args.name or DEMO_EVENT_NAME,
            total_seats=args.seats or DEMO_TOTAL_SEATS,
        )
    print(json.dumps(result))
    return 0


def cmd_worker(_args) -> int:
    from app.jobs.reservation_expiry import main

    main()
    return 0


def cmd_db_check(_args) -> int:
    from sqlalchemy import text

    url = get_settings().database_url
    assert_project_database(url)
    with _session() as s:
        version = s.execute(text("SHOW server_version")).scalar_one()
        user = s.execute(text("SELECT current_user")).scalar_one()
    print(json.dumps({"database": database_name(url), "user": user, "server_version": version}))
    return 0


def cmd_invariants(_args) -> int:
    from app.services.invariants import check_invariants, summary_counts

    with _session() as s:
        violations = check_invariants(s)
        counts = summary_counts(s)
    print(json.dumps({"violations": violations, "counts": counts}, indent=2))
    return 0 if all(v == 0 for v in violations.values()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed", help="idempotently seed an event and its seats")
    seed.add_argument("--event-id", type=int)
    seed.add_argument("--name")
    seed.add_argument("--seats", type=int)
    seed.set_defaults(func=cmd_seed)
    sub.add_parser("worker", help="run the reservation expiry worker").set_defaults(func=cmd_worker)
    sub.add_parser("db-check", help="verify DB connectivity").set_defaults(func=cmd_db_check)
    sub.add_parser("invariants", help="check database invariants").set_defaults(func=cmd_invariants)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
