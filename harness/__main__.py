"""Runner CLI: `python -m harness <command>`."""

from __future__ import annotations

import argparse
import asyncio

from harness import dbadmin


def cmd_clone(_args) -> None:
    fp = asyncio.run(dbadmin.clone_trial_db())
    rows = ", ".join(f"{t}={v['rows']}" for t, v in fp["tables"].items())
    print(f"cloned and verified: {fp['checksum']}\n{rows}")


def main() -> None:
    p = argparse.ArgumentParser(prog="harness")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("clone", help="clone fielddesk_template into the trial DB and verify it").set_defaults(
        func=cmd_clone
    )
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
