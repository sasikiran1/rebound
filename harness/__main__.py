"""Database setup: `python -m harness clone` copies the seeded template database into the trial database Resolve uses,
and verifies its checksum."""

from __future__ import annotations

import argparse
import asyncio

from harness import dbadmin


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("clone", help="clone fielddesk_template into the trial DB and verify it")
    ap.parse_args()
    fp = asyncio.run(dbadmin.clone_trial_db())
    rows = ", ".join(f"{t}={v['rows']}" for t, v in fp["tables"].items())
    print(f"cloned and verified: {fp['checksum']}\n{rows}")


if __name__ == "__main__":
    main()
