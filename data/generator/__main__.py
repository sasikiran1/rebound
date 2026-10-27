"""Build fielddesk_template and both task sets: `python -m data.generator [--seed N --scale F]`.

Set A (exploration) and set B (frozen evaluation) come from separate task seeds, never share a customer or an
appointment, and are selected from the same template, so calibration on A transfers to B unchanged.
"""

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


TASK_SETS = {  # prefix: (file, task seed)
    "A": ("data/tasks/tasks_dev_A.jsonl", 101),
    "B": ("data/tasks/tasks_eval_B.jsonl", 202),
}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--seed", type=int, default=1729)
    p.add_argument("--scale", type=float, default=1.0, help="1.0 = full scale (400 technicians)")
    p.add_argument("--tasks", type=int, default=300, help="tasks per set (a trial schedules about 220)")
    args = p.parse_args()

    t0 = time.monotonic()
    ds = build(args.seed, args.scale, [(prefix, args.tasks, seed) for prefix, (_, seed) in TASK_SETS.items()])
    fp = asyncio.run(write_template(ds))

    task_info = {}
    for prefix, (file, task_seed) in TASK_SETS.items():
        tasks = ds.tasks[prefix]
        task_path = ROOT / file
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text("".join(json.dumps(t, sort_keys=True) + "\n" for t in tasks))
        task_info[prefix] = {
            "file": file,
            "task_seed": task_seed,
            "sha256": hashlib.sha256(task_path.read_bytes()).hexdigest(),
            "count": len(tasks),
            "by_kind": {k: sum(t["kind"] == k for t in tasks) for k in sorted({t["kind"] for t in tasks})},
        }
    manifest = {
        "generator_seed": args.seed,
        "scale": args.scale,
        "template_db": settings.TEMPLATE_DB,
        "fingerprint": fp,
        "task_sets": task_info,
    }
    out = ROOT / settings.TEMPLATE_MANIFEST
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2) + "\n")

    for table, info in fp["tables"].items():
        print(f"{table:22s} {info['rows']:>8d}")
    for prefix, info in task_info.items():
        print(f"tasks {prefix}: {info['count']} {info['by_kind']}")
    print(f"checksum {fp['checksum']}  ({time.monotonic() - t0:.1f}s)")


if __name__ == "__main__":
    main()
