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

    async def _unusable(self, rid: str, out: Outcome) -> dict:
        req = await self.store.get(rid)
        if out.horizon_only and req["questions_asked"] < MAX_QUESTIONS:
            text = (f"I can only book up to {self.last_day.strftime('%A %b')} {self.last_day.day}. "
                    "Would a date before then work, or should we leave your appointment as it is?")
            await self.store.set_state(rid, "needs_input", question={"kind": "horizon", "text": text,
                                                                     "candidates": []},
                                       questions_asked=req["questions_asked"] + 1, turn=(text, "horizon"))
            return {"state": "needs_input", "kind": "horizon"}
        await self.store.set_state(rid, "unresolved", detail="; ".join(out.problems)[:1000],
                                   turn=("I couldn't turn that into a booking I'm sure you'd approve, so I haven't "
                                         "changed anything. A colleague will follow up.", "unresolved"))
        return {"state": "unresolved", "problems": out.problems}

    async def customer_says(self, request_id: str, text: str) -> dict:
        req = await self.store.get(request_id)
        if req["state"] in TERMINAL:
            return {"state": req["state"], "refused": "closed"}
        if req["state"] == "claimed":
            return {"state": "claimed", "refused": "executing"}
        n = await self.store.add_turn(request_id, "customer", text, "reply" if req["question"] else "message")
        turns = await self.store.turns(request_id)
        facts = req["facts"]
        names = {t["technician_id"]: t["name"] for t in facts["technicians"]}
        current = await self.store.intent(request_id, req["version"]) if req["version"] else {"rules": []}
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
            await self.store.set_state(request_id, "unresolved", detail="customer declined",
                                       turn=("Okay. I've left your appointment as it is.", "unresolved"))
            return {"state": "unresolved", "declined": True}
        if out.value is None:
            if out.horizon_only:
                return await self._unusable(request_id, out)
            req = await self.store.get(request_id)
            if req["questions_asked"] >= MAX_QUESTIONS:
                return await self._unusable(request_id, out)
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
        if question.get("kind") == "confirm" and delta["approved_slots"]:
            await self.store.add_turn(request_id, "assistant", "Thanks, booking it now.", "ack")
        else:
            await self.store.add_turn(request_id, "assistant", f"Updated: {I.explain(out.value, names)}. {PENDING}",
                                      "ack")
        return {"state": "ready", "version": v, "diff": delta}

    async def approve_structured(self, request_id: str, intent: dict, by: str, turn_text: str) -> int | None:
        """Form arm: a structured form submission approves a version directly (no model)."""
        req = await self.store.get(request_id)
        current = await self.store.intent(request_id, req["version"]) if req["version"] else {"rules": []}
        n = await self.store.add_turn(request_id, "customer", turn_text, "form")
        return await self.store.approve(request_id, I.assign_ids(intent), {"turn": n, "by": by, "text": turn_text},
                                        I.diff(current, I.assign_ids(intent)))
