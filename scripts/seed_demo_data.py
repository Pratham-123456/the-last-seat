"""Seed the demo concert (event 1) with 500 seats. Safe to run any number of times.

python scripts/seed_demo_data.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["seed", *sys.argv[1:]]))
