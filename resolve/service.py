"""Conversation side of Resolve: open a request, take customer turns, store approved versions.

Only a customer turn can approve a version: the first message approves what it states; a later message (an answer
to our question or an unprompted correction) approves the patched version built from it. The worker never widens
an intent on its own. While the worker holds a request, a new message is refused and the customer is told the
change is being made (the executing version is frozen).
"""

from __future__ import annotations

import asyncio
import datetime as dt

from resolve import intent as I
from resolve.api import ApiError, FieldDesk
from resolve.interpret import Model, Outcome, build_facts, interpret, revise
from resolve.store import TERMINAL, Store
from resolve.worker import MAX_QUESTIONS

# Never promise a booking before it exists: the customer keeps the current appointment until a change is confirmed.
PENDING = ("Your current appointment stays as it is until a change is confirmed. I'll message you when it's booked, "
           "or if I need to ask you something.")
YES = {"yes", "yes please", "yes book it", "yes please book it", "yes, please book it", "book it", "please book it",
       "ok", "okay", "sure", "confirm", "yes that works", "that works", "sounds good"}

class Resolve:
    def __init__(self, store: Store, api: FieldDesk, model: Model | None, today: dt.date, horizon_days: int,
                 interpret_fn=interpret, revise_fn=revise):
        """`interpret_fn` / `revise_fn` default to the model; the rule-based baseline passes its own."""
        self.store, self.api, self.model = store, api, model
        self.today, self.horizon_days = today, horizon_days
        self.interpret, self.revise = interpret_fn, revise_fn

    @property
    def last_day(self) -> dt.date:
        return self.today + dt.timedelta(days=self.horizon_days)

    async def load_facts(self, customer_id: int, appointment_id: int, tries: int = 4) -> dict:
        for n in range(tries):
            try:
                ctx = await self.api.context(customer_id)
                return build_facts(ctx, appointment_id, self.today, self.horizon_days)
            except ApiError as exc:
                if not exc.uncertain or n == tries - 1:
                    raise
                await asyncio.sleep(2 ** n)

    async def open(self, request_id: str, customer_id: int, appointment_id: int, region: str, service: str,
                   message: str, facts: dict | None = None, confirm: bool = False) -> dict:
        facts = facts or await self.load_facts(customer_id, appointment_id)
        await self.store.create(request_id, customer_id, appointment_id, region, service, self.today,
                                self.horizon_days, facts, confirm)
        await self.store.add_turn(request_id, "customer", message, "message")
        return await self._interpret(request_id, facts)

    async def _interpret(self, rid: str, facts: dict) -> dict:
        turns = await self.store.turns(rid)
        out = await self.interpret(self.model, facts, turns)
        await self.store.event(rid, "interpret", attempts=out.attempts, problems=out.problems,
                               intent=out.value)
        if out.value is not None:
            v = await self.store.approve(rid, out.value, {"turn": turns[-1]["n"], "by": "customer",
                                                          "text": turns[-1]["text"]}, None)
            names = {t["technician_id"]: t["name"] for t in facts["technicians"]}
            await self.store.add_turn(rid, "assistant", f"Understood: {I.explain(out.value, names)}. {PENDING}",
                                      "ack")
            return {"state": "ready", "version": v}
        return await self._unusable(rid, out)

    async def _unusable(self, rid: str, out: Outcome, settled: str = "none") -> dict:
        req = await self.store.get(rid)
        if out.horizon_only and req["questions_asked"] < MAX_QUESTIONS:
            text = (f"I can only book up to {self.last_day.strftime('%A %b')} {self.last_day.day}. "
                    "Would a date before then work, or should we leave your appointment as it is?")
            await self.store.set_state(rid, "needs_input", question={"kind": "horizon", "text": text,
                                                                     "candidates": []},
                                       questions_asked=req["questions_asked"] + 1, turn=(text, "horizon"))
            return {"state": "needs_input", "kind": "horizon"}
        return await self._close(rid, "; ".join(out.problems)[:1000],
                                 "I couldn't turn that into a booking I'm sure you'd approve, so I haven't changed "
                                 "anything. A colleague will follow up.", settled, problems=out.problems)

    async def _close(self, rid: str, detail: str, text: str, settled: str, **extra) -> dict:
        """End the request. If an earlier write is still unsettled, it must be settled first: the worker does that and
        then closes the request with this message, saying what happened to the write."""
        after = "I won't make any further change." if extra.get("declined") else \
            "I couldn't apply your latest message safely, so I won't change anything else. A colleague will follow up."
        if settled == "unknown":
            await self.store.close_pending(rid, detail, text, after)
            await self.store.add_turn(rid, "assistant", "A change I had already sent is still being confirmed. I'll "
                                      "tell you as soon as I know whether it went through.", "ack")
            return {"state": "ready", "pending_close": True, **extra}
        if settled == "applied":
            text = f"{await self._moved_note(rid)} {after}"
        await self.store.set_state(rid, "unresolved", detail=detail, turn=(text, "unresolved"))
        return {"state": "unresolved", **extra}

    async def _moved_note(self, rid: str) -> str:
        req = await self.store.get(rid)
        names = {t["technician_id"]: t["name"] for t in req["facts"]["technicians"]}
        try:
            appt = await self.api.appointment(req["appointment_id"])
            from resolve.evaluate import describe
            where = describe({k: appt[k] for k in ("slot_id", "starts_at", "technician_id")}, names)
        except (ApiError, KeyError):
            where = "the time I had already sent"
        return f"The change I had already sent went through before I could stop it, so your appointment is now {where}."

    async def _settle_outstanding(self, req: dict) -> str:
        """Before a customer turn can change or end the request, settle any write still in doubt: void its key at
        FieldDesk ('voided': it can never land; 'applied': it had committed). 'unknown' when FieldDesk cannot be
        reached: the turn's effect then waits for the worker to settle it ('none': nothing was outstanding)."""
        if req["attempt_state"] != "sending":
            return "none"
        try:
            res = await self.api.void_key(req["attempt_key"])
        except ApiError as exc:
            await self.store.event(req["request_id"], "service.settle_deferred", key=req["attempt_key"],
                                   error=exc.status)
            return "unknown"
        await self.store.clear_attempt(req["request_id"], req["attempt_key"])
        await self.store.event(req["request_id"], "service.settled_before_turn", outcome=res["outcome"],
                               key=req["attempt_key"], change_id=(res.get("change") or {}).get("change_id"))
        return res["outcome"]

    async def customer_says(self, request_id: str, text: str) -> dict:
        req = await self.store.get(request_id)
        if req["state"] in TERMINAL:
            return {"state": req["state"], "refused": "closed"}
        if req["state"] == "claimed":
            return {"state": "claimed", "refused": "executing"}
        settled = await self._settle_outstanding(req)  # before the turn is read: a correction never races a write
        n = await self.store.add_turn(request_id, "customer", text, "reply" if req["question"] else "message")
        turns = await self.store.turns(request_id)
        facts = req["facts"]
        names = {t["technician_id"]: t["name"] for t in facts["technicians"]}
        latest = req.get("pending_version") or req["version"]
        current = await self.store.intent(request_id, latest) if latest else {"rules": []}
        question = req["question"] or {}
        if question.get("kind") == "confirm" and I.normalize(text) in YES:  # plain yes: no model call needed
            out = Outcome(I.apply_patch(current, {"ops": [{"op": "accept_proposal",
                                                           "slot_id": question["candidates"][0]["slot_id"]}]},
                                        question)[0], [], 0)
        else:
            out = await self.revise(self.model, facts, turns, current, req["question"], names)
        await self.store.event(request_id, "revise", attempts=out.attempts, problems=out.problems,
                               decision=out.decision, intent=out.value)
        if out.decision == "declined":
            return await self._close(request_id, "customer declined", "Okay. I've left your appointment as it is.",
                                     settled, declined=True)
        if out.value is None:
            if out.horizon_only:
                return await self._unusable(request_id, out, settled)
            req = await self.store.get(request_id)
            if req["questions_asked"] >= MAX_QUESTIONS:
                return await self._unusable(request_id, out, settled)
            text_back = "Sorry, I couldn't apply that safely. Could you say it another way?"
            await self.store.set_state(request_id, "needs_input", question=req["question"] or {
                "kind": "repeat", "text": text_back, "candidates": []}, questions_asked=req["questions_asked"] + 1,
                turn=(text_back, "repeat"))
            return {"state": "needs_input", "problems": out.problems}
        delta = I.diff(current, out.value)
        v = await self.store.approve(request_id, out.value, {"turn": n, "by": "customer", "text": text,
                                                             "answering": (req["question"] or {}).get("kind")}, delta)
        if v is None:
            return {"state": (await self.store.get(request_id))["state"], "refused": "executing"}
        pending = (await self.store.get(request_id))["pending_version"] == v
        prefix = (await self._moved_note(request_id) + " ") if settled == "applied" else ""
        if pending:
            reply = (f"Updated: {I.explain(out.value, names)}. A change I had already sent is still being "
                     "confirmed; your update takes effect as soon as that is settled, and I'll tell you what happened.")
        elif question.get("kind") == "confirm" and delta["approved_slots"]:
            reply = "Thanks, booking it now."
        else:
            reply = f"Updated: {I.explain(out.value, names)}. {PENDING}"
        await self.store.add_turn(request_id, "assistant", prefix + reply, "ack")
        return {"state": "ready", "version": v, "diff": delta, "pending": pending, "earlier_write": settled}

    async def approve_structured(self, request_id: str, intent: dict, by: str, turn_text: str) -> int | None:
        """Form arm: a structured form submission approves a version directly (no model). An unsettled earlier write
        makes it pending until the worker settles that write (Store.approve)."""
        req = await self.store.get(request_id)
        latest = req.get("pending_version") or req["version"]
        current = await self.store.intent(request_id, latest) if latest else {"rules": []}
        n = await self.store.add_turn(request_id, "customer", turn_text, "form")
        return await self.store.approve(request_id, I.assign_ids(intent), {"turn": n, "by": by, "text": turn_text},
                                        I.diff(current, I.assign_ids(intent)))
