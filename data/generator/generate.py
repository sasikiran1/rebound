"""Seeded FieldDesk data and task generator.

Everything is derived from one `random.Random(seed)`, so the same seed and scale always give the
same rows, the same IDs and therefore the same checksums.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field

HORIZON_START = dt.date(2026, 11, 2)
DAYS = 21
SLOTS_PER_DAY = 16
DAY_START_HOUR = 8
SLOT_MINUTES = 30
GRID_W, GRID_H = 5, 4  # 20 regions
BOOKED_FRACTION = 0.70
CANCELLED_FRACTION = 0.05
FULL_TECHNICIANS = 400
FULL_CUSTOMERS = 40_000
MAX_WINDOW_SHIFT_DAYS = 7
MAX_INFEASIBLE_FREE_SLOTS = 8

SKILLS = ["hvac", "gas", "electrical", "plumbing", "appliance", "solar"]
SERVICES = [  # code, name, skill
    ("hvac_repair", "HVAC repair", "hvac"),
    ("ac_maintenance", "air-conditioning maintenance", "hvac"),
    ("boiler_service", "boiler service", "gas"),
    ("gas_safety_check", "gas safety check", "gas"),
    ("electrical_inspection", "electrical inspection", "electrical"),
    ("ev_charger_install", "EV charger installation", "electrical"),
    ("plumbing_repair", "plumbing repair", "plumbing"),
    ("water_heater_install", "water heater installation", "plumbing"),
    ("appliance_install", "appliance installation", "appliance"),
    ("solar_maintenance", "solar panel maintenance", "solar"),
]
TASK_MIX = {"narrow_1day": 0.6, "flexible_3day": 0.3, "infeasible": 0.1}


@dataclass
class Dataset:
    seed: int
    scale: float
    regions: list[tuple] = field(default_factory=list)
    region_distances: list[tuple] = field(default_factory=list)
    skills: list[tuple] = field(default_factory=list)
    service_types: list[tuple] = field(default_factory=list)
    technicians: list[tuple] = field(default_factory=list)
    technician_skills: list[tuple] = field(default_factory=list)
    technician_regions: list[tuple] = field(default_factory=list)
    customers: list[tuple] = field(default_factory=list)
    slots: list[tuple] = field(default_factory=list)
    appointments: list[tuple] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)

    def tables(self) -> dict[str, tuple[list[str], list[tuple]]]:
        """Table name -> (columns, rows), in foreign-key load order."""
        return {
            "regions": (["id", "code", "grid_x", "grid_y"], self.regions),
            "region_distances": (["from_region", "to_region", "distance"], self.region_distances),
            "skills": (["id", "code"], self.skills),
            "service_types": (["id", "code", "name", "skill_id"], self.service_types),
            "technicians": (["id", "name", "home_region"], self.technicians),
            "technician_skills": (["technician_id", "skill_id"], self.technician_skills),
            "technician_regions": (["technician_id", "region_id"], self.technician_regions),
            "customers": (["id", "name", "region_id"], self.customers),
            "slots": (["id", "technician_id", "slot_date", "starts_at"], self.slots),
            "appointments": (["id", "customer_id", "service_type_id", "slot_id", "status"], self.appointments),
        }


def day(index: int) -> dt.date:
    return HORIZON_START + dt.timedelta(days=index)


def build(seed: int, scale: float, n_tasks: int, task_prefix: str) -> Dataset:
    rng = random.Random(seed)
    ds = Dataset(seed=seed, scale=scale)

    # Regions on a 5x4 grid; distance is Manhattan distance between cells.
    region_ids = list(range(1, GRID_W * GRID_H + 1))
    pos = {r: ((r - 1) % GRID_W, (r - 1) // GRID_W) for r in region_ids}
    ds.regions = [(r, f"R{r:02d}", *pos[r]) for r in region_ids]
    dist = {
        (a, b): abs(pos[a][0] - pos[b][0]) + abs(pos[a][1] - pos[b][1]) for a in region_ids for b in region_ids
    }
    ds.region_distances = [(a, b, d) for (a, b), d in dist.items()]

    skill_id = {code: i for i, code in enumerate(SKILLS, start=1)}
    ds.skills = [(i, code) for code, i in skill_id.items()]
    service_skill = {}
    for i, (code, name, skill) in enumerate(SERVICES, start=1):
        ds.service_types.append((i, code, name, skill_id[skill]))
        service_skill[i] = skill_id[skill]
    services_for_skill: dict[int, list[int]] = {}
    for svc, sk in service_skill.items():
        services_for_skill.setdefault(sk, []).append(svc)

    # Technicians: home region round-robin, 2-3 skills, coverage = home + 1-3 adjacent regions.
    n_tech = max(len(region_ids), round(FULL_TECHNICIANS * scale))
    tech_skills: dict[int, list[int]] = {}
    tech_cov: dict[int, list[int]] = {}
    for t in range(1, n_tech + 1):
        home = region_ids[(t - 1) % len(region_ids)]
        ds.technicians.append((t, f"Technician {t:03d}", home))
        tech_skills[t] = sorted(rng.sample(list(skill_id.values()), rng.choice([2, 3])))
        adjacent = [r for r in region_ids if dist[(home, r)] == 1]
        extra = rng.sample(adjacent, min(len(adjacent), rng.choice([1, 2, 2, 3])))
        tech_cov[t] = sorted([home, *extra])
        ds.technician_skills += [(t, s) for s in tech_skills[t]]
        ds.technician_regions += [(t, r) for r in tech_cov[t]]

    n_cust = round(FULL_CUSTOMERS * scale)
    customers_by_region: dict[int, list[int]] = {r: [] for r in region_ids}
    customer_region = {}
    for c in range(1, n_cust + 1):
        r = rng.choice(region_ids)
        ds.customers.append((c, f"Customer {c:05d}", r))
        customers_by_region[r].append(c)
        customer_region[c] = r

    # Slots and appointments. ~70% of slots carry a scheduled appointment for a customer the
    # technician is qualified to serve; a few free slots carry cancelled appointments.
    slot_info: dict[int, tuple[int, int]] = {}  # slot -> (technician, day index)
    slots_by_tech_day: dict[tuple[int, int], list[int]] = {}
    booked: dict[int, int] = {}  # slot -> scheduled appointment id
    appt_info: dict[int, tuple[int, int, int]] = {}  # appointment -> (customer, service, slot)
    slot_id = 0
    for t in range(1, n_tech + 1):
        for d in range(DAYS):
            for k in range(SLOTS_PER_DAY):
                slot_id += 1
                starts = dt.datetime.combine(day(d), dt.time(DAY_START_HOUR)) + dt.timedelta(
                    minutes=SLOT_MINUTES * k
                )
                ds.slots.append((slot_id, t, day(d), starts))
                slot_info[slot_id] = (t, d)
                slots_by_tech_day.setdefault((t, d), []).append(slot_id)

    def add_appointment(slot: int, status: str, region: int | None = None, service: int | None = None,
                        exclude: set[int] | frozenset[int] = frozenset()):
        t, _ = slot_info[slot]
        if service is None:
            service = rng.choice([s for sk in tech_skills[t] for s in services_for_skill.get(sk, [])])
        if region is None:
            region = rng.choice(tech_cov[t])
        customer = rng.choice([c for c in customers_by_region[region] if c not in exclude])
        appt = len(ds.appointments) + 1
        ds.appointments.append((appt, customer, service, slot, status))
        if status == "scheduled":
            booked[slot] = appt
            appt_info[appt] = (customer, service, slot)
        return appt

    cancelled_p = CANCELLED_FRACTION / (1 - BOOKED_FRACTION)
    for s in range(1, slot_id + 1):
        if rng.random() < BOOKED_FRACTION:
            add_appointment(s, "scheduled")
        elif rng.random() < cancelled_p:
            add_appointment(s, "cancelled")

    techs_for: dict[tuple[int, int], list[int]] = {}  # (region, skill) -> technicians
    for t in tech_cov:
        for r in tech_cov[t]:
            for sk in tech_skills[t]:
                techs_for.setdefault((r, sk), []).append(t)

    def qualifying(region: int, service: int, days: range) -> list[int]:
        out = []
        for t in techs_for.get((region, service_skill[service]), []):
            for d in days:
                out += slots_by_tech_day[(t, d)]
        return out

    ds.tasks = _select_tasks(
        rng, n_tasks, task_prefix, appt_info, slot_info, customer_region, booked, qualifying,
        add_appointment, ds,
    )
    return ds


def _select_tasks(rng, n_tasks, prefix, appt_info, slot_info, customer_region, booked, qualifying,
                  add_appointment, ds) -> list[dict]:
    """Pick goals that cannot interfere with each other.

    For every accepted task we track its qualifying slots Q (right service skill, covers the
    customer's region, inside the window), its free candidates F and its original slot O.
    A new task is accepted only if its free candidates are disjoint from everyone's F (no slot
    stealing), its original slot is in nobody's Q, and its Q holds nobody's original slot (freeing an
    old slot never changes another goal's feasibility), and its (region, service, date) triples are new.
    """
    counts = {k: round(n_tasks * share) for k, share in TASK_MIX.items()}
    counts["narrow_1day"] = n_tasks - counts["flexible_3day"] - counts["infeasible"]
    # Infeasible tasks first: their blocker appointments must exist before feasibility is judged.
    kinds = ["infeasible"] * counts["infeasible"]
    rest = ["narrow_1day"] * counts["narrow_1day"] + ["flexible_3day"] * counts["flexible_3day"]
    rng.shuffle(rest)
    kinds += rest

    per_key: dict[tuple[int, int, int], int] = {}  # (customer, service, day) -> scheduled count
    for customer, service, slot in appt_info.values():
        key = (customer, service, slot_info[slot][1])
        per_key[key] = per_key.get(key, 0) + 1
    candidates = sorted(appt_info)
    rng.shuffle(candidates)

    claimed_free: set[int] = set()
    claimed_qual: set[int] = set()
    original_slots: set[int] = set()
    triples: set[tuple[int, int, int]] = set()
    used_customers: set[int] = set()
    service_code = {row[0]: row[1] for row in ds.service_types}
    service_name = {row[0]: row[2] for row in ds.service_types}
    tasks: list[dict] = []
    cursor = 0

    for kind in kinds:
        length = 3 if kind == "flexible_3day" else 1
        while cursor < len(candidates):
            appt = candidates[cursor]
            cursor += 1
            customer, service, slot = appt_info[appt]
            d0 = slot_info[slot][1]
            region = customer_region[customer]
            if customer in used_customers or per_key[(customer, service, d0)] != 1:
                continue
            if slot in claimed_qual:
                continue
            starts = [
                k for k in range(DAYS - length + 1)
                if not (k <= d0 < k + length) and abs(k - d0) <= MAX_WINDOW_SHIFT_DAYS
            ]
            start = rng.choice(starts)
            window = range(start, start + length)
            new_triples = {(region, service, d) for d in window}
            if new_triples & triples:
                continue
            qual = qualifying(region, service, window)
            if set(qual) & original_slots:
                continue
            free = [s for s in qual if s not in booked]
            if set(free) & claimed_free:
                continue
            if kind == "infeasible":
                if len(free) > MAX_INFEASIBLE_FREE_SLOTS:
                    continue
                for s in free:  # book every remaining qualifying slot for other customers
                    blocker = add_appointment(s, "scheduled", region=region, service=service,
                                              exclude=used_customers | {customer})
                    b_customer, b_service, b_slot = appt_info[blocker]
                    b_key = (b_customer, b_service, slot_info[b_slot][1])
                    per_key[b_key] = per_key.get(b_key, 0) + 1
                free = []
            elif not free:
                continue

            claimed_free |= set(free)
            claimed_qual |= set(qual)
            original_slots.add(slot)
            triples |= new_triples
            used_customers.add(customer)
            d1, d2 = day(window[0]), day(window[-1])
            when = f"on {d1}" if d1 == d2 else f"between {d1} and {d2}"
            tasks.append({
                "kind": kind,
                "expected": "defer" if kind == "infeasible" else "reschedule",
                "customer_id": customer,
                "customer_region": f"R{region:02d}",
                "allowed_regions": [f"R{region:02d}"],
                "appointment_id": appt,
                "service": service_code[service],
                "current_date": day(d0).isoformat(),
                "d1": d1.isoformat(),
                "d2": d2.isoformat(),
                "free_candidates": len(free),
                "goal_text": (
                    f"Customer {customer} wants their {service_name[service]} appointment on "
                    f"{day(d0).isoformat()} moved to any open slot {when}, same service type, with a "
                    f"technician qualified for that service who covers the customer's region."
                ),
            })
            break
    # Infeasible tasks were picked first; shuffle so arrival order does not follow selection order.
    rng.shuffle(tasks)
    return [{"task_id": f"{prefix}{i:04d}", **t} for i, t in enumerate(tasks, start=1)]
