"""Resolve case files (data/resolve/*.yaml): loading, and seeding each case into its own region of the trial DB.

Every case gets a region, technicians, a customer, completed history, the current appointment and exactly the open
slots it lists, under id ranges no generated row uses. The real FieldDesk API then serves only those slots for the
case's customer, so the expected outcomes can be enumerated by hand.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
import yaml

TODAY = dt.date(2026, 11, 10)  # fixture clock: Tuesday
HORIZON_DAYS = 13
DAYS = {"tue": "2026-11-10", "wed": "2026-11-11", "thu": "2026-11-12", "fri": "2026-11-13", "sat": "2026-11-14",
        "mon2": "2026-11-16", "tue2": "2026-11-17", "wed2": "2026-11-18", "thu2": "2026-11-19", "fri2": "2026-11-20"}
SKILL_OF = {"hvac_repair": "hvac", "ac_maintenance": "hvac", "boiler_service": "gas", "gas_safety_check": "gas",
            "electrical_inspection": "electrical", "plumbing_repair": "plumbing"}
CASE_DIR = Path("data/resolve")


@dataclass
class Case:
    id: str
    n: int
    kind: str
    raw: dict
    techs: dict[str, dict]  # alias -> {id, name, skills}
    slots: dict[str, dict]  # key "fri 15:30 dana" -> {id, date, time, tech}
    messages: list[str]
    replies: list[str]
    expected: dict
    service: str = "hvac_repair"
    taken: list[str] = field(default_factory=list)

    @property
    def region(self) -> str:
        return f"Z{self.n:02d}"

    @property
    def region_id(self) -> int:
        return 200 + self.n

    @property
    def customer_id(self) -> int:
        return 900_000 + self.n

    @property
    def other_customer_id(self) -> int:
        return 950_000 + self.n

    @property
    def appointment_id(self) -> int:
        return 800_000 + self.n * 100

    def slot_id(self, key: str) -> int:
        return self.slots[norm_key(key)]["id"]

    def alias_of(self, slot_id: int) -> str | None:
        return next((k for k, s in self.slots.items() if s["id"] == slot_id), None)

    def tech_ids(self, aliases: list[str]) -> list[int]:
        return [self.techs[a]["id"] for a in aliases]


def case_number(case_id: str) -> int:
    m = re.fullmatch(r"([DHF])(\d+)", case_id)
    if not m:
        raise ValueError(f"bad case id {case_id!r}")
    return {"D": 0, "H": 10, "F": 50}[m.group(1)] + int(m.group(2))


def norm_key(key: str) -> str:
    return " ".join(key.split())


def parse_slot(key: str) -> tuple[str, str, str]:
    day, time, tech = norm_key(key).split(" ")
    return DAYS[day], time, tech


def load(path: str | Path) -> list[Case]:
    cases = []
    for raw in yaml.safe_load(Path(path).read_text()):
        n = case_number(raw["id"])
        service = raw.get("service", "hvac_repair")
        techs = {}
        for k, (alias, spec) in enumerate(raw["technicians"].items()):
            spec = {"name": spec} if isinstance(spec, str) else dict(spec)
            techs[alias] = {"id": 900_000 + n * 10 + k, "name": spec["name"],
                            "skills": spec.get("skills", [SKILL_OF[service]])}
        grid = raw.get("filler_grid")  # {techs, days, times}: every combination is an open slot
        filler = [f"{d} {t} {tech}" for d in grid["days"] for t in grid["times"] for tech in grid["techs"]] if grid \
            else []
        keys = [norm_key(k) for k in raw.get("open", [])] + filler
        slots = {}
        for i, key in enumerate(dict.fromkeys(keys)):
            date, time, tech = parse_slot(key)
            slots[key] = {"id": 9_000_000 + n * 1000 + i, "date": date, "time": time, "tech": tech}
        cases.append(Case(raw["id"], n, raw["kind"], raw, techs, slots, raw["messages"], raw.get("replies", []),
                          raw["expected"], service, [norm_key(k) for k in raw.get("taken", [])]))
    return cases


def check(case: Case) -> list[str]:
    """Internal consistency of a hand-written case (not a judgment of any system)."""
    p = []
    e = case.expected
    for stage in e["authorized"]:
        p += [f"{case.id}: authorized slot {k!r} is not open" for k in stage if norm_key(k) not in case.slots]
    p += [f"{case.id}: final slot {k!r} is not open" for k in e["final"] if norm_key(k) not in case.slots]
    p += [f"{case.id}: final slot {k!r} is taken" for k in e["final"] if norm_key(k) in case.taken]
    last = {norm_key(k) for k in e["authorized"][-1]}
    p += [f"{case.id}: final slot {k!r} is not authorized at the last turn" for k in e["final"]
          if norm_key(k) not in last]
    turns = len(case.messages) + len(case.replies)
    if len(e["authorized"]) != turns:
        p.append(f"{case.id}: {len(e['authorized'])} authorized stages for {turns} customer turns")
    for key in case.slots:
        if case.slots[key]["tech"] not in case.techs:
            p.append(f"{case.id}: slot {key!r} names unknown technician")
    return p


async def unseed(conn: asyncpg.Connection, case: Case) -> None:
    lo, hi = 9_000_000 + case.n * 1000, 9_000_000 + case.n * 1000 + 999
    appts = f"SELECT id FROM appointments WHERE customer_id IN ({case.customer_id}, {case.other_customer_id})"
    await conn.execute(f"DELETE FROM appointment_changes WHERE appointment_id IN ({appts})")
    await conn.execute(f"DELETE FROM appointments WHERE customer_id IN ({case.customer_id}, {case.other_customer_id})")
    await conn.execute("DELETE FROM slots WHERE id BETWEEN $1 AND $2", lo, hi)
    tech_lo, tech_hi = 900_000 + case.n * 10, 900_000 + case.n * 10 + 9
    await conn.execute("DELETE FROM technician_skills WHERE technician_id BETWEEN $1 AND $2", tech_lo, tech_hi)
    await conn.execute("DELETE FROM technician_regions WHERE technician_id BETWEEN $1 AND $2", tech_lo, tech_hi)
    await conn.execute("DELETE FROM technicians WHERE id BETWEEN $1 AND $2", tech_lo, tech_hi)
    await conn.execute("DELETE FROM customers WHERE id IN ($1, $2)", case.customer_id, case.other_customer_id)
    await conn.execute("DELETE FROM region_distances WHERE from_region = $1 OR to_region = $1", case.region_id)
    await conn.execute("DELETE FROM regions WHERE id = $1", case.region_id)


async def seed(conn: asyncpg.Connection, case: Case) -> None:
    """(Re)create the case's world. Taken slots are still open here; `take` books them later."""
    async with conn.transaction():
        await unseed(conn, case)
        await conn.execute("INSERT INTO regions (id, code, grid_x, grid_y) VALUES ($1, $2, 0, 0)",
                           case.region_id, case.region)
        await conn.execute("INSERT INTO region_distances VALUES ($1, $1, 0)", case.region_id)
        skills = {r["code"]: r["id"] for r in await conn.fetch("SELECT id, code FROM skills")}
        services = {r["code"]: r["id"] for r in await conn.fetch("SELECT id, code FROM service_types")}
        for t in case.techs.values():
            await conn.execute("INSERT INTO technicians (id, name, home_region) VALUES ($1, $2, $3)",
                               t["id"], t["name"], case.region_id)
            await conn.execute("INSERT INTO technician_regions VALUES ($1, $2)", t["id"], case.region_id)
            for s in t["skills"]:
                await conn.execute("INSERT INTO technician_skills VALUES ($1, $2)", t["id"], skills[s])
        await conn.execute("INSERT INTO customers (id, name, region_id) VALUES ($1, $2, $3), ($4, $5, $3)",
                           case.customer_id, case.raw["customer"], case.region_id, case.other_customer_id,
                           "Other customer")
        next_slot = 9_000_000 + case.n * 1000 + 900  # history and current-appointment slots
        next_appt = case.appointment_id

        async def slot_row(date: str, time: str, tech: str) -> int:
            nonlocal next_slot
            next_slot += 1
            when = dt.datetime.fromisoformat(f"{date}T{time}")
            await conn.execute("INSERT INTO slots (id, technician_id, slot_date, starts_at) VALUES ($1, $2, $3, $4)",
                               next_slot, case.techs[tech]["id"], when.date(), when)
            return next_slot

        cur_date, cur_time, cur_tech = parse_slot(case.raw["current"])
        await conn.execute(
            "INSERT INTO appointments (id, customer_id, service_type_id, slot_id, status) "
            "VALUES ($1, $2, $3, $4, 'scheduled')",
            next_appt, case.customer_id, services[case.service], await slot_row(cur_date, cur_time, cur_tech))
        for h in case.raw.get("history", []):
            next_appt += 1
            await conn.execute(
                "INSERT INTO appointments (id, customer_id, service_type_id, slot_id, status) "
                "VALUES ($1, $2, $3, $4, 'completed')",
                next_appt, case.customer_id, services[h.get("service", case.service)],
                await slot_row(str(h["date"]), h["time"], h["tech"]))
        for s in case.slots.values():
            when = dt.datetime.fromisoformat(f"{s['date']}T{s['time']}")
            await conn.execute("INSERT INTO slots (id, technician_id, slot_date, starts_at) VALUES ($1, $2, $3, $4)",
                               s["id"], case.techs[s["tech"]]["id"], when.date(), when)


async def take(conn: asyncpg.Connection, case: Case, keys: list[str] | None = None) -> list[int]:
    """Another customer books these slots (the first choice disappearing while the request waits)."""
    taken = []
    services = {r["code"]: r["id"] for r in await conn.fetch("SELECT id, code FROM service_types")}
    for i, key in enumerate(keys if keys is not None else case.taken):
        sid = case.slot_id(key)
        await conn.execute(
            "INSERT INTO appointments (id, customer_id, service_type_id, slot_id, status) "
            "VALUES ($1, $2, $3, $4, 'scheduled')",
            case.appointment_id + 50 + i, case.other_customer_id, services[case.service], sid)
        taken.append(sid)
    return taken
