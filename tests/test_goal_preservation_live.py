import sqlite3
import time
from types import SimpleNamespace as NS

import httpx
from fielddesk.goal_preservation import Goal, create_app

from oracle.goal_preservation import inspect
from scripts.goal_preservation_live import OrdinaryStore, session


async def test_ordinary_ablation_allows_real_avoidable_loss_but_not_duplicate(tmp_path):
    path = tmp_path / "state.sqlite"
    app = create_app(path)
    store = OrdinaryStore(path)
    app.state.store._evaluate = store._evaluate
    store.seed_slots([9, 15])
    for goal, choices in [("alice", [9, 15]), ("bob", [15])]:
        store.register(Goal(goal_id=goal, acceptable_slots=choices, expires_at=time.time() + 3600))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = (await client.get("/goals/alice/proposal", params={"slot_id": 15})).json()
        assert r["allowed"] and "alternative_slot_ids" not in r
        assert (
            await client.post("/goals/alice/commit", json={"slot_id": 15, "version": r["version"]})
        ).status_code == 200
        occupied = await client.post("/goals/bob/commit", json={"slot_id": 15, "version": r["version"] + 1})
        assert occupied.status_code == 409
    verdict = inspect(path, time.time())
    assert verdict["avoidable_loss"] == 1 and verdict["valid_bookings"] and verdict["unique_effects"]


async def test_runner_does_not_book_on_behalf_of_model_and_retains_errors(tmp_path, monkeypatch):
    class NoAction:
        def __init__(self, *args, **kwargs):
            self.responses = self

        async def create(self, **kwargs):
            return NS(output=[], dropped_output=True)

        async def close(self):
            pass

    monkeypatch.setattr("scripts.goal_preservation_live.OllamaClient", NoAction)
    app = create_app(tmp_path / "state.sqlite")
    store = app.state.store
    store.seed_slots([9])
    store.register(Goal(goal_id="alice", acceptable_slots=[9], expires_at=time.time() + 3600))
    outcome = await session(app, store, "fake-for-runner-check-only", "alice", [9], 1, tmp_path / "session")
    assert outcome["end"] == "protocol_error" and not outcome["sql_completed"]
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM bookings").fetchone()[0] == 0


async def test_runner_retains_provider_failure_without_retry(tmp_path, monkeypatch):
    calls = []

    class Broken:
        def __init__(self, *args, **kwargs):
            self.responses = self

        async def create(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("test-only provider failure")

        async def close(self):
            pass

    monkeypatch.setattr("scripts.goal_preservation_live.OllamaClient", Broken)
    app = create_app(tmp_path / "state.sqlite")
    store = app.state.store
    store.seed_slots([9])
    store.register(Goal(goal_id="alice", acceptable_slots=[9], expires_at=time.time() + 3600))
    outcome = await session(app, store, "fake-for-runner-check-only", "alice", [9], 1, tmp_path / "session")
    assert len(calls) == 1 and outcome["end"] == "runner_or_provider_error"
    assert not outcome["sql_completed"]


def test_source_archive_preserves_same_named_files(tmp_path):
    from scripts.goal_preservation_live import archive_sources

    repo = tmp_path / "repo"
    files = [repo / "backend" / "goal_preservation.py", repo / "oracle" / "goal_preservation.py"]
    for index, source in enumerate(files):
        source.parent.mkdir(parents=True)
        source.write_text(str(index))
    output = tmp_path / "result"
    paths = archive_sources(files, output, repo)
    assert len(set(paths.values())) == 2
    for index, source in enumerate(files):
        assert (output / paths[str(source)]).read_text() == str(index)
