"""Build fielddesk_template and a task set: `python -m data.generator [--seed N --scale F]`."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

import asyncpg

from data.generator.generate import build
from harness import settings
from harness.dbadmin import fingerprint

ROOT = Path(__file__).resolve().parents[2]


async def write_template(ds) -> dict:
    admin = await asyncpg.connect(settings.admin_dsn())
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", settings.TEMPLATE_DB)
        if exists:
            await admin.execute(f'ALTER DATABASE "{settings.TEMPLATE_DB}" IS_TEMPLATE false')
            await admin.execute(f'DROP DATABASE "{settings.TEMPLATE_DB}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{settings.TEMPLATE_DB}"')
    finally:
        await admin.close()

    conn = await asyncpg.connect(settings.admin_dsn(settings.TEMPLATE_DB))
    try:
        await conn.execute((ROOT / "data/schema.sql").read_text())
        for table, (columns, rows) in ds.tables().items():
            await conn.copy_records_to_table(table, records=rows, columns=columns)
        await conn.execute((ROOT / "data/indexes.sql").read_text())
        await conn.execute("VACUUM ANALYZE")
        fp = await fingerprint(conn)
    finally:
        await conn.close()

    admin = await asyncpg.connect(settings.admin_dsn())
    try:
        await admin.execute(f'ALTER DATABASE "{settings.TEMPLATE_DB}" IS_TEMPLATE true')
    finally:
        await admin.close()
    return fp


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--seed", type=int, default=1729)
    p.add_argument("--scale", type=float, default=0.1, help="1.0 = full scale (400 technicians)")
    p.add_argument("--tasks", type=int, default=40)
    p.add_argument("--task-file", default="data/tasks/tasks_dev_A.jsonl")
    p.add_argument("--task-prefix", default="A")
    args = p.parse_args()

    t0 = time.monotonic()
    ds = build(args.seed, args.scale, args.tasks, args.task_prefix)
    fp = asyncio.run(write_template(ds))

    task_path = ROOT / args.task_file
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text("".join(json.dumps(t, sort_keys=True) + "\n" for t in ds.tasks))
    manifest = {
        "generator_seed": args.seed,
        "scale": args.scale,
        "template_db": settings.TEMPLATE_DB,
        "fingerprint": fp,
        "tasks": {
            "file": args.task_file,
            "sha256": hashlib.sha256(task_path.read_bytes()).hexdigest(),
            "count": len(ds.tasks),
            "by_kind": {k: sum(t["kind"] == k for t in ds.tasks) for k in sorted({t["kind"] for t in ds.tasks})},
        },
    }
    out = ROOT / settings.TEMPLATE_MANIFEST
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2) + "\n")

    for table, info in fp["tables"].items():
        print(f"{table:22s} {info['rows']:>8d}")
    print(f"tasks: {manifest['tasks']['count']} {manifest['tasks']['by_kind']}")
    print(f"checksum {fp['checksum']}  ({time.monotonic() - t0:.1f}s)")


if __name__ == "__main__":
    main()
