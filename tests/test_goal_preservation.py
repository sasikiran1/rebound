import itertools
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest
from fastapi import HTTPException
from fielddesk.goal_preservation import Goal, Proposal, Store, create_app, matching_size

from oracle.goal_preservation import inspect, optimum


def configured(tmp_path):
    store = Store(tmp_path / "goals.sqlite")
    store.seed_slots([9, 15])
    expiry = time.time() + 3600
    store.register(Goal(goal_id="alice", acceptable_slots=[9, 15], expires_at=expiry))
    store.register(Goal(goal_id="bob", acceptable_slots=[15], expires_at=expiry))
    return store


def test_matching_against_independent_exhaustive_oracle():
    choices = [set(s for s in range(3) if mask & (1 << s)) for mask in range(8)]
    for sets in itertools.product(choices, repeat=3):
        graph = dict(zip(("a", "b", "c"), sets, strict=True))
        assert matching_size(graph) == optimum(graph)


async def test_http_reject_alternative_commit_replay_and_sql(tmp_path):
    store = configured(tmp_path)
    app = create_app(store.path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        receipt = (await client.get("/goals/alice/proposal", params={"slot_id": 15})).json()
        assert receipt["opportunities_lost"] == 1
        assert receipt["alternative_slot_ids"] == [9]
        assert "bob" not in str(receipt)
        denied = await client.post("/goals/alice/commit", json={"slot_id": 15, "version": receipt["version"]})
        assert denied.status_code == 409
        assert inspect(store.path, time.time())["completed"] == 0
        body = {"slot_id": 9, "version": receipt["version"]}
        assert (await client.post("/goals/alice/commit", json=body)).status_code == 200
        assert (await client.post("/goals/alice/commit", json=body)).json()["replayed"]
        stale = await client.post("/goals/bob/commit", json={"slot_id": 15, "version": receipt["version"]})
        assert stale.status_code == 409
        fresh = (await client.get("/goals/bob/proposal", params={"slot_id": 15})).json()
        assert (
            await client.post("/goals/bob/commit", json={"slot_id": 15, "version": fresh["version"]})
        ).status_code == 200
    result = inspect(store.path, time.time())
    assert result["completed"] == 2 and result["avoidable_loss"] == 0
    assert result["valid_bookings"] and result["unique_effects"]
    assert inspect(Store(store.path).path, time.time()) == result  # survives process-local object replacement


def test_new_registration_invalidates_recommendation(tmp_path):
    store = configured(tmp_path)
    receipt = store.evaluate("alice", 9)
    store.register(Goal(goal_id="carol", acceptable_slots=[9], expires_at=time.time() + 3600))
    with pytest.raises(HTTPException, match="stale"):
        store.commit("alice", Proposal(slot_id=9, version=receipt["version"]))


def test_expired_goal_cannot_commit(tmp_path):
    store = configured(tmp_path)
    receipt = store.evaluate("alice", 9)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE goals SET expires=0 WHERE id='alice'")
    with pytest.raises(HTTPException, match="expired"):
        store.commit("alice", Proposal(slot_id=9, version=receipt["version"]))


def test_registration_conflict_and_constraints(tmp_path):
    store = configured(tmp_path)
    with pytest.raises(HTTPException, match="different constraints"):
        store.register(Goal(goal_id="alice", acceptable_slots=[9], expires_at=time.time() + 3600))
    with pytest.raises(HTTPException, match="unknown slot"):
        store.register(Goal(goal_id="new", acceptable_slots=[99], expires_at=time.time() + 3600))
    with pytest.raises(HTTPException, match="outside registered"):
        store.evaluate("bob", 9)


def test_racing_commits_recheck_version_under_write_lock(tmp_path):
    store = configured(tmp_path)
    version = store.evaluate("alice", 9)["version"]
    barrier = Barrier(2)

    def attempt(goal, slot):
        barrier.wait()
        try:
            Store(store.path).commit(goal, Proposal(slot_id=slot, version=version))
            return "committed"
        except HTTPException as exc:
            assert exc.status_code == 409
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, "alice", 9), pool.submit(attempt, "bob", 15)]
        assert sorted(f.result() for f in futures) == ["committed", "stale"]
    with sqlite3.connect(store.path) as conn:
        finished = {r[0] for r in conn.execute("SELECT goal_id FROM bookings")}
    for goal, slot in [("alice", 9), ("bob", 15)]:
        if goal not in finished:
            receipt = store.evaluate(goal, slot)
            store.commit(goal, Proposal(slot_id=slot, version=receipt["version"]))
    assert inspect(store.path, time.time())["completed"] == 2


def test_infeasible_cohort_preserves_maximum_count_not_every_identity(tmp_path):
    store = configured(tmp_path)
    store.register(Goal(goal_id="carol", acceptable_slots=[15], expires_at=time.time() + 3600))
    receipt = store.evaluate("bob", 15)
    assert receipt["allowed"] and receipt["pending_achievable_before"] == 2


def test_oracle_detects_unprotected_avoidable_loss(tmp_path):
    store = configured(tmp_path)
    with sqlite3.connect(store.path) as conn:
        conn.execute("INSERT INTO bookings VALUES('alice',15)")
    result = inspect(store.path, time.time())
    assert result["valid_bookings"] and result["unique_effects"]
    assert result["avoidable_loss"] == 1
