"""Experimental finite-goal API. Not installed in FieldDesk's historical routes.

Allowed slot sets are trusted fixture inputs, not verified natural-language interpretations.
All writers use SQLite BEGIN IMMEDIATE; recommendations never authorize a later write.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


def matching_size(graph: dict[str, set[int]]) -> int:
    """Maximum cardinality matching; no model or heuristic scoring."""
    owners: dict[int, str] = {}

    def assign(goal: str, seen: set[int]) -> bool:
        for slot in sorted(graph[goal]):
            if slot in seen:
                continue
            seen.add(slot)
            if slot not in owners or assign(owners[slot], seen):
                owners[slot] = goal
                return True
        return False

    return sum(assign(goal, set()) for goal in sorted(graph))


class Goal(BaseModel):
    goal_id: str = Field(min_length=1, max_length=100)
    acceptable_slots: list[int] = Field(min_length=1, max_length=64)
    expires_at: float = Field(gt=0, allow_inf_nan=False)


class Proposal(BaseModel):
    slot_id: int
    version: int = Field(ge=0)


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS slots(id INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS goals(
                    id TEXT PRIMARY KEY, allowed TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS bookings(
                    goal_id TEXT PRIMARY KEY REFERENCES goals(id),
                    slot_id INTEGER NOT NULL UNIQUE REFERENCES slots(id));
                CREATE TABLE IF NOT EXISTS revision(id INTEGER PRIMARY KEY CHECK(id=1), n INTEGER NOT NULL);
                INSERT OR IGNORE INTO revision VALUES(1, 0);
            """)

    @contextmanager
    def transaction(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def seed_slots(self, slots: list[int]):
        with self.transaction() as conn:
            conn.executemany("INSERT OR IGNORE INTO slots VALUES(?)", [(s,) for s in slots])
            conn.execute("UPDATE revision SET n=n+1")

    def register(self, goal: Goal):
        allowed = sorted(set(goal.acceptable_slots))
        with self.transaction() as conn:
            if goal.expires_at <= time.time():
                raise HTTPException(422, "goal already expired")
            existing = conn.execute("SELECT * FROM goals WHERE id=?", (goal.goal_id,)).fetchone()
            if existing:
                if json.loads(existing["allowed"]) != allowed or existing["expires"] != goal.expires_at:
                    raise HTTPException(409, "goal identity already has different constraints")
                return {"goal_id": goal.goal_id, "replayed": True}
            if conn.execute("SELECT count(*) FROM goals").fetchone()[0] >= 64:
                raise HTTPException(422, "prototype limit: 64 registered goals")
            known = {r[0] for r in conn.execute("SELECT id FROM slots")}
            if not set(allowed) <= known:
                raise HTTPException(422, "unknown slot in acceptable set")
            conn.execute("INSERT INTO goals VALUES(?,?,?)", (goal.goal_id, json.dumps(allowed), goal.expires_at))
            conn.execute("UPDATE revision SET n=n+1")
        return {"goal_id": goal.goal_id, "replayed": False}

    def _evaluate(self, conn, goal_id: str, slot_id: int) -> dict:
        now = time.time()
        occupied = {r[0] for r in conn.execute("SELECT slot_id FROM bookings")}
        rows = conn.execute(
            "SELECT * FROM goals WHERE expires>? AND id NOT IN (SELECT goal_id FROM bookings)", (now,)
        ).fetchall()
        graph = {r["id"]: set(json.loads(r["allowed"])) - occupied for r in rows}
        if goal_id not in graph:
            raise HTTPException(409, "goal missing, expired or already completed")
        if slot_id not in graph[goal_id]:
            raise HTTPException(409, "slot unavailable or outside registered constraints")
        before = matching_size(graph)

        def remaining(candidate):
            return matching_size({g: choices - {candidate} for g, choices in graph.items() if g != goal_id})

        after = 1 + remaining(slot_id)
        alternatives = [s for s in sorted(graph[goal_id]) if s != slot_id and 1 + remaining(s) == before]
        return {
            "goal_id": goal_id,
            "slot_id": slot_id,
            "version": conn.execute("SELECT n FROM revision").fetchone()[0],
            "valid_until": min(r["expires"] for r in rows),
            "pending_achievable_before": before,
            "achievable_including_proposed": after,
            "opportunities_lost": before - after,
            "allowed": after == before,
            "alternative_slot_ids": alternatives,
            "scope": "registered_unexpired_goals; unit_capacity_slots; maximum_count",
        }

    def evaluate(self, goal_id: str, slot_id: int):
        with self.transaction() as conn:
            return self._evaluate(conn, goal_id, slot_id)

    def commit(self, goal_id: str, proposal: Proposal):
        with self.transaction() as conn:
            prior = conn.execute("SELECT slot_id FROM bookings WHERE goal_id=?", (goal_id,)).fetchone()
            if prior:
                if prior[0] != proposal.slot_id:
                    raise HTTPException(409, "completed goal cannot change slots")
                return {"goal_id": goal_id, "slot_id": prior[0], "replayed": True}
            current = conn.execute("SELECT n FROM revision").fetchone()[0]
            if current != proposal.version:
                raise HTTPException(409, "stale recommendation; evaluate again")
            receipt = self._evaluate(conn, goal_id, proposal.slot_id)
            if not receipt["allowed"]:
                raise HTTPException(409, detail={"type": "avoidable_goal_loss", "receipt": receipt})
            conn.execute("INSERT INTO bookings VALUES(?,?)", (goal_id, proposal.slot_id))
            conn.execute("UPDATE revision SET n=n+1")
        return {**receipt, "committed": True, "replayed": False}


def create_app(path: str | Path = "var/goal-preservation.sqlite") -> FastAPI:
    """Loopback research service. No authentication; never expose to an untrusted network."""
    store = Store(path)
    app = FastAPI(title="Rebound goal preservation — experimental")
    app.state.store = store

    @app.post("/goals")
    def register(goal: Goal):
        return store.register(goal)

    @app.get("/goals/{goal_id}/proposal")
    def evaluate(goal_id: str, slot_id: int):
        return store.evaluate(goal_id, slot_id)

    @app.post("/goals/{goal_id}/commit")
    def commit(goal_id: str, proposal: Proposal):
        return store.commit(goal_id, proposal)

    return app
