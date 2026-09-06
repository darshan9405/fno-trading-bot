#!/usr/bin/env python3
"""Seed the instruments whitelist from the Upstox instrument master.

Runs automatically in the Docker entrypoint before gunicorn starts; also usable
manually. Seeded rows are inactive by default — enable the underlyings you want
to trade from the UI.

Usage:
    python scripts/seed_instruments.py              # seed (idempotent, preserves enabled flags)
    python scripts/seed_instruments.py --dry-run    # show what would be seeded, change nothing
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import create_all  # noqa: E402
from app.services import instrument_service  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the universe without touching the DB")
    args = parser.parse_args()

    # Ensure the schema exists: this runs in the Docker entrypoint before
    # gunicorn (create_app), so a fresh volume has no tables yet.
    create_all()

    df = instrument_service.fetch_master()
    universe = instrument_service.build_universe(df)

    if args.dry_run:
        active = sum(1 for _ in universe)
        print(f"{len(universe)} optionable underlyings would be seeded (all inactive by default):")
        for u in universe[:20]:
            print(f"  {u['symbol']:<16} {u['segment']:<10} lot={u['lot_size']}")
        if len(universe) > 20:
            print(f"  ... and {len(universe) - 20} more")
        return 0

    inserted, updated = instrument_service.seed_instruments()
    print(f"seeded: {inserted} inserted, {updated} updated ({len(universe)} optionable underlyings total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())