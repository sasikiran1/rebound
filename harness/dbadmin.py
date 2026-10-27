"""Template fingerprints and per-trial database cloning (PREPARING)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import asyncpg

from harness import settings

TABLES = [
    "regions", "region_distances", "skills", "service_types", "technicians", "technician_skills",
    "technician_regions", "customers", "slots", "appointments", "appointment_changes",
]


async def fingerprint(conn: asyncpg.Connection) -> dict:
    """Row count and an order-independent content hash per table, plus one combined checksum."""
    tables = {}
    for t in TABLES:
        row = await conn.fetchrow(
            f"SELECT count(*) AS n, md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) AS h "
            f"FROM {t} x"
        )
        tables[t] = {"rows": row["n"], "md5": row["h"]}
    combined = hashlib.sha256(json.dumps(tables, sort_keys=True).encode()).hexdigest()
    return {"tables": tables, "checksum": f"sha256:{combined}"}


async def clone_trial_db(database: str = settings.TRIAL_DB) -> dict:
    """Drop and re-clone the trial DB from the template, ANALYZE it, and verify it against the
    fingerprint recorded when the template was built. Raises on any mismatch."""
    recorded = json.loads(Path(settings.TEMPLATE_MANIFEST).read_text())
    admin = await asyncpg.connect(settings.admin_dsn())
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{database}" TEMPLATE "{settings.TEMPLATE_DB}"')
    finally:
        await admin.close()
    conn = await asyncpg.connect(settings.admin_dsn(database))
    try:
        await conn.execute("ANALYZE")
        actual = await fingerprint(conn)
    finally:
        await conn.close()
    if actual["checksum"] != recorded["fingerprint"]["checksum"]:
        diff = [
            t for t in TABLES
            if actual["tables"][t] != recorded["fingerprint"]["tables"].get(t)
        ]
        raise RuntimeError(f"clone of {settings.TEMPLATE_DB} does not match recorded fingerprint: {diff}")
    return actual
