"""Runner CLI: `python -m harness <command>`."""

from __future__ import annotations

import argparse
import asyncio

from harness import dbadmin, probe


def cmd_clone(_args) -> None:
    fp = asyncio.run(dbadmin.clone_trial_db())
    rows = ", ".join(f"{t}={v['rows']}" for t, v in fp["tables"].items())
    print(f"cloned and verified: {fp['checksum']}\n{rows}")


def cmd_pulse(args) -> None:
    asyncio.run(probe.pulse(latency_ms=args.latency_ms, jitter_ms=args.jitter_ms, during_s=args.duration_s))


def cmd_endpoints(args) -> None:
    asyncio.run(probe.endpoints(n=args.n))


def main() -> None:
    p = argparse.ArgumentParser(prog="harness")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("clone", help="clone fielddesk_template into the trial DB and verify it").set_defaults(
        func=cmd_clone
    )
    sp = sub.add_parser("pulse", help="manual latency pulse with canary and FieldDesk DB time")
    sp.add_argument("--latency-ms", type=int, default=400)
    sp.add_argument("--jitter-ms", type=int, default=50)
    sp.add_argument("--duration-s", type=int, default=10)
    sp.set_defaults(func=cmd_pulse)
    sp = sub.add_parser("endpoints", help="per-endpoint DB time on the healthy system")
    sp.add_argument("--n", type=int, default=100)
    sp.set_defaults(func=cmd_endpoints)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
