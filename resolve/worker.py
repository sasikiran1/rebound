"""The one Resolve worker: executes the frozen approved version of a ready request. No model.

For each claimed request:
  1. Reconcile first. If an earlier attempt was sent but its answer never recorded (the worker died, or the reply
     was lost), resend the *same* slot with the *same* idempotency key. FieldDesk returns the original change for a
     key it has already committed (`replayed: true`) and writes nothing, so a committed booking is confirmed rather
     than repeated; a 409 for that slot means the key never committed (else it would replay), so the search runs.
  2. Search every open slot in the horizon (all pages) and decide (evaluate.decide).
  3. book: record the attempt (slot, key, `sending`) durably, then POST. permission/clarify/no_options: store the
     question and hand the request back to the customer (needs_input).
The idempotency key is `resolve:<request_id>:v<version>`: stable per request and approved version, not a hash of the
intent (two different requests may have identical content). Approval of a new version is refused while a request
is claimed, so the version being executed cannot change underneath the worker.

Guarantee, as tested: with one worker at a time per request, a crash at any of the injected points (after claim,
after recording the attempt, after the POST returned) leads to at most one applied change. It relies on FieldDesk's
idempotency-key table; it is not a general exactly-once claim.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os

from resolve.api import ApiError, FieldDesk
from resolve.evaluate import decide, describe
from resolve.store import Store

MAX_QUESTIONS = 3
CRASH_POINTS = ("after_claim", "after_record", "after_send")


def _exit(point: str) -> None:
    os._exit(86)  # as abrupt as SIGKILL: no cleanup, no lease release


class Worker:
    def __init__(self, store: Store, api: FieldDesk, owner: str = "worker-1", lease_s: float = 8.0,
                 crash_at: str | None = None, crash=_exit, before_post=None):
        self.store, self.api, self.owner, self.lease_s = store, api, owner, lease_s
        self.crash_at = crash_at or os.environ.get("RESOLVE_CRASH_AT") or None
        self.crash = crash
        self.before_post = before_post

    def _maybe_crash(self, point: str) -> None:
        if self.crash_at == point:
            self.crash(point)

    async def run_once(self, request_id: str | None = None) -> dict | None:
        req = await self.store.claim(self.owner, self.lease_s, request_id)
        if req is None:
            return None
        return await self.process(req)

    async def _resend(self, req: dict) -> dict:
        """Resend the recorded attempt with its own key. `same_slot` means the appointment already sits in that slot:
        the first attempt committed while this resend waited on its row lock, before its key was visible. Asking
        again then returns the committed change (`replayed`). Anything else propagates."""
        args = (req["appointment_id"], req["customer_id"], req["attempt_slot"], req["attempt_key"])
        for n in range(3):
            try:
                return await self.api.reschedule(*args)
            except ApiError as exc:
                if exc.body.get("type") != "same_slot" or n == 2:
                    raise
                await self.store.event(req["request_id"], "worker.reconcile_same_slot", attempt=n + 1)
                await asyncio.sleep(0.5)

    async def _release(self, rid: str, reason: str, **fields) -> dict:
        await self.store.set_state(rid, "ready", owner=self.owner, **fields)
        await self.store.event(rid, "worker.retry_later", owner=self.owner, reason=reason)
        return {"request_id": rid, "result": "retry_later", "reason": reason}

    async def _finish(self, req: dict, res: dict, slot: dict | None, slot_id: int, how: str) -> dict:
        rid = req["request_id"]
        ok = await self.store.set_state(rid, "completed", owner=self.owner, change_id=res["change_id"],
                                        booked_slot=slot_id, attempt_state="acked",
                                        detail=f"{how}; replayed={res.get('replayed')}")
        await self.store.event(rid, "worker.completed", owner=self.owner, version=req["version"],
                               change_id=res["change_id"], slot_id=slot_id, replayed=res.get("replayed"), how=how)
        if ok:
            names = {t["technician_id"]: t["name"] for t in req["facts"]["technicians"]}
            where = describe(slot, names) if slot else f"slot {slot_id}"
            await self.store.add_turn(rid, "assistant", f"Done. Your appointment is now {where}.", "completed")
        return {"request_id": rid, "result": "completed", "change_id": res["change_id"], "slot_id": slot_id,
                "replayed": res.get("replayed"), "how": how}

    async def process(self, req: dict) -> dict:
        rid, version = req["request_id"], req["version"]
        key = f"resolve:{rid}:v{version}"
        await self.store.event(rid, "worker.claimed", owner=self.owner, version=version,
                               pending_attempt=req["attempt_state"])
        self._maybe_crash("after_claim")
        intent = await self.store.intent(rid, version)
        facts = req["facts"]
        names = {t["technician_id"]: t["name"] for t in facts["technicians"]}

        if req["attempt_state"] == "sending":  # 1. reconcile an uncertain earlier attempt before anything else
            try:
                res = await self._resend(req)
            except ApiError as exc:
                if exc.uncertain or exc.body.get("type") == "same_slot":
                    return await self._release(rid, f"reconcile: {exc.status} {exc.body.get('type', '')}".strip())
                await self.store.event(rid, "worker.reconciled", outcome="not_committed", error=exc.status,
                                       slot_id=req["attempt_slot"])
                if not await self.store.record_attempt(rid, self.owner, None, None, None):
                    return {"request_id": rid, "result": "lease_lost"}
            else:
                await self.store.event(rid, "worker.reconciled", outcome="committed", slot_id=req["attempt_slot"],
                                       replayed=res.get("replayed"))
                try:
                    appt = await self.api.appointment(req["appointment_id"])
                    slot = {k: appt[k] for k in ("slot_id", "starts_at", "technician_id")}
                except ApiError:
                    slot = None
                return await self._finish(req, res, slot, req["attempt_slot"], "reconciled earlier attempt")

        today = req["today"]
        last = today + dt.timedelta(days=req["horizon_days"])
        for _ in range(3):  # 2. search and decide; a 409 (slot taken meanwhile) searches again
            try:
                slots = await self.api.search_all(req["service"], today.isoformat(), last.isoformat(),
                                                  [req["region"]])
            except ApiError as exc:
                return await self._release(rid, f"search: {exc.status}")
            d = decide(intent, slots, names)
            await self.store.event(rid, "worker.decided", version=version, decision=d.kind, reason=d.reason,
                                   open_slots=len(slots), slot_id=d.slot and d.slot["slot_id"],
                                   candidates=[c["slot_id"] for c in d.candidates])
            if d.kind != "book":
                return await self._ask(req, d)
            if req.get("confirm") and d.slot["slot_id"] not in intent.get("approved_slots", []):
                d.kind, d.candidates = "confirm", [d.slot]
                d.question = (f"I can move you to {describe(d.slot, names)}. Shall I book it? Your current "
                              "appointment stays as it is until you say yes.")
                return await self._ask(req, d)
            slot = d.slot
            if not await self.store.record_attempt(rid, self.owner, slot["slot_id"], key, "sending"):
                return {"request_id": rid, "result": "lease_lost"}
            self._maybe_crash("after_record")
            if self.before_post is not None:  # fault checks: inject a network fault exactly around the write
                await self.before_post(req, slot)
            try:
                res = await self.api.reschedule(req["appointment_id"], req["customer_id"], slot["slot_id"], key)
            except ApiError as exc:
                if exc.uncertain:  # may have committed: keep the attempt; the next claim reconciles it
                    return await self._release(rid, f"reschedule: {exc.status}")
                if exc.http_status == 409:
                    await self.store.event(rid, "worker.slot_taken", slot_id=slot["slot_id"])
                    await self.store.record_attempt(rid, self.owner, None, None, None)
                    continue
                await self.store.set_state(rid, "unresolved", owner=self.owner,
                                           detail=f"reschedule refused: {exc.status} {exc.message[:200]}")
                await self.store.event(rid, "worker.refused", error=exc.status, message=exc.message[:200])
                return {"request_id": rid, "result": "unresolved", "reason": exc.status}
            self._maybe_crash("after_send")
            return await self._finish(req, res, slot, slot["slot_id"], "booked")
        return await self._release(rid, "slots kept being taken")

    async def _ask(self, req: dict, d) -> dict:
        rid = req["request_id"]
        if req["questions_asked"] >= MAX_QUESTIONS:
            await self.store.set_state(rid, "unresolved", owner=self.owner,
                                       detail=f"no agreement after {MAX_QUESTIONS} questions")
            await self.store.add_turn(rid, "assistant", "I couldn't find a time that works, so I've left your "
                                      "appointment as it is. A colleague will follow up.", "unresolved")
            return {"request_id": rid, "result": "unresolved", "reason": "question limit"}
        question = {"kind": d.kind, "text": d.question, "candidates": d.candidates,
                    "unclear_index": d.unclear_index, "version": req["version"]}
        await self.store.set_state(rid, "needs_input", owner=self.owner, question=question,
                                   questions_asked=req["questions_asked"] + 1)
        await self.store.add_turn(rid, "assistant", d.question, d.kind)
        return {"request_id": rid, "result": "needs_input", "kind": d.kind, "question": d.question,
                "candidates": [c["slot_id"] for c in d.candidates]}


async def _main() -> None:
    """A separate worker process.

    `python -m resolve.worker --request ID [--crash-at after_send]` polls one request until it leaves `ready`
    (completed, unresolved or needs_input); used by the demo and the recovery checks.
    `python -m resolve.worker` (no --request) is the long-running service: it claims any ready request, or one whose
    lease expired, until SIGTERM/SIGINT, then finishes the request in hand and exits. Prints one JSON line per pass."""
    import argparse
    import json
    import signal

    from harness import settings
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", default=None)
    ap.add_argument("--owner", default=f"worker-{os.getpid()}")
    ap.add_argument("--crash-at", choices=CRASH_POINTS)
    ap.add_argument("--lease-s", type=float, default=8.0)
    ap.add_argument("--poll-s", type=float, default=1.0)
    ap.add_argument("--max-s", type=float, default=None, help="stop after this long (default: 120 s with --request)")
    a = ap.parse_args()
    store = await Store(settings.admin_dsn(settings.TRIAL_DB)).open()
    api = FieldDesk(settings.FIELDDESK_URL, actor="resolve-worker")
    worker = Worker(store, api, owner=a.owner, lease_s=a.lease_s, crash_at=a.crash_at)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    max_s = a.max_s if a.max_s is not None else (120.0 if a.request else None)
    end = loop.time() + max_s if max_s else None
    try:
        if a.request is None:  # service mode
            print(json.dumps({"owner": a.owner, "mode": "service", "started": True}), flush=True)
            while not stop.is_set() and (end is None or loop.time() < end):
                res = await worker.run_once()
                if res is not None:
                    print(json.dumps({"owner": a.owner, **res}, default=str), flush=True)
                    continue  # more work may be waiting
                try:
                    await asyncio.wait_for(stop.wait(), a.poll_s)
                except TimeoutError:
                    pass
            print(json.dumps({"owner": a.owner, "mode": "service", "stopped": True}), flush=True)
            return
        while end is None or loop.time() < end:
            req = await store.get(a.request)
            if req is None or req["state"] in ("completed", "unresolved", "needs_input"):
                print(json.dumps({"request_id": a.request, "state": req and req["state"], "owner": a.owner}),
                      flush=True)
                break
            res = await worker.run_once(a.request)
            if res is not None:
                print(json.dumps({"owner": a.owner, **res}, default=str), flush=True)
            await asyncio.sleep(a.poll_s)
    finally:
        await api.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(_main())
