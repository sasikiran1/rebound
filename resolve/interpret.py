"""Model calls: conversation + grounded facts -> intent; a later customer message -> patch.

The model sees no availability, so it cannot invent slots. Its output is validated (intent.validate); one repair call
is allowed with the validator's problems, and a still-invalid result is never stored. Works with the Meta Responses
API (harness.provider) and local Ollama (harness.ollama), which share `client.responses.create`.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from harness.provider import ProviderFailed, cost_usd
from resolve import intent as I

INTERPRET_PROMPT = "prompts/resolve_interpret_v1.md"
REVISE_PROMPT = "prompts/resolve_revise_v1.md"
WEEKDAY = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _model_schema(schema: dict) -> dict:
    """The tool schema the model sees: no system-owned fields (rule ids, approved slots)."""
    s = copy.deepcopy(schema)

    def strip(node):
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                props.pop("id", None) if "kind" in props else None
                props.pop("approved_slots", None)
            for v in node.values():
                strip(v)
        elif isinstance(node, list):
            for v in node:
                strip(v)
    strip(s)
    return s


RECORD_TOOL = {"type": "function", "name": "record_intent",
               "description": "Record the customer's structured intent.",
               "parameters": _model_schema({**I.INTENT_SCHEMA, "required": ["rules", "summary"]})}
REVISE_TOOL = {"type": "function", "name": "revise_intent",
               "description": "Record what the customer's new message changes, as operations.",
               "parameters": {**I.PATCH_SCHEMA, "required": ["ops", "summary"]}}


def build_facts(context: dict, appointment_id: int, today: dt.date, horizon_days: int) -> dict:
    """Facts for the model and the validator, from FieldDesk's /customers/{id}/context."""
    appts = context["appointments"]
    current = next(a for a in appts if a["appointment_id"] == appointment_id)
    history = [a for a in appts if a["appointment_id"] != appointment_id]
    days = [today + dt.timedelta(days=i) for i in range(horizon_days + 1)]
    return {
        "today": today.isoformat(),
        "today_weekday": WEEKDAY[today.weekday()],
        "horizon_days": horizon_days,
        "calendar": [f"{d.isoformat()} {WEEKDAY[d.weekday()]}" for d in days],
        "customer": {"customer_id": context["customer_id"], "name": context["name"]},
        "appointment_being_moved": {k: current[k] for k in ("appointment_id", "service_name", "starts_at",
                                                             "technician_id", "technician_name")},
        "history": [{k: a[k] for k in ("appointment_id", "status", "service_name", "starts_at", "technician_id",
                                       "technician_name")} for a in history],
        "technicians": context["technicians"],
    }


@dataclass
class CallLog:
    calls: list[dict] = field(default_factory=list)

    @property
    def cost(self) -> float:
        return sum(c["cost_usd"] for c in self.calls)


@dataclass
class Model:
    client: object  # openai.AsyncOpenAI or harness.ollama.OllamaClient
    name: str
    max_output_tokens: int = 4096
    reasoning_effort: str | None = "low"  # Meta only
    log: CallLog = field(default_factory=CallLog)
    max_calls: int | None = None  # hard caps, checked before every call (paid runs)
    max_cost_usd: float | None = None

    async def tool_call(self, purpose: str, instructions: str, user: str, tool: dict,
                        extra: list[dict] | None = None) -> tuple[dict | None, str]:
        """One model call that must return one call of `tool`. Returns (arguments or None, raw text)."""
        self.check_budget()
        kwargs = dict(model=self.name, instructions=instructions,
                      input=[{"role": "user", "content": user}] + (extra or []), tools=[tool],
                      max_output_tokens=self.max_output_tokens)
        if self.reasoning_effort and not hasattr(self.client, "relay"):
            kwargs.update(reasoning={"effort": self.reasoning_effort}, store=False, parallel_tool_calls=False,
                          tool_choice="auto")
        t0 = time.perf_counter()
        entry = {"purpose": purpose, "model": self.name}
        try:
            res = await self.client.responses.create(**kwargs)
        except ProviderFailed as exc:
            entry.update(status=f"provider_failed:{exc.status}", latency_ms=round((time.perf_counter() - t0) * 1e3),
                         cost_usd=0.0, input_tokens=0, output_tokens=0)
            self.log.calls.append(entry)
            raise
        res = getattr(res, "response", res)  # harness.provider.Provider wraps the response
        usage = getattr(res, "usage", None)
        tin = getattr(usage, "input_tokens", 0) or 0
        tout = getattr(usage, "output_tokens", 0) or 0
        cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
        args, text = None, ""
        for item in res.output:
            if item.type == "function_call" and item.name == tool["name"] and args is None:
                try:
                    args = json.loads(item.arguments or "{}")
                except json.JSONDecodeError:
                    text += f"[unparseable arguments] {item.arguments[:500]}"
            elif item.type == "message":
                text += "".join(getattr(c, "text", "") for c in item.content)
        entry.update(status="ok" if args is not None else "no_tool_call",
                     latency_ms=round((time.perf_counter() - t0) * 1e3), input_tokens=tin, cached_tokens=cached,
                     output_tokens=tout, cost_usd=round(cost_usd(self.name, tin, cached, tout), 6),
                     arguments=args, text=text[:2000])
        self.log.calls.append(entry)
        return args, text

    def check_budget(self) -> None:
        if self.max_calls is not None and len(self.log.calls) >= self.max_calls:
            raise BudgetExceeded(f"model call cap {self.max_calls} reached")
        if self.max_cost_usd is not None and self.log.cost >= self.max_cost_usd:
            raise BudgetExceeded(f"spend cap ${self.max_cost_usd} reached (${self.log.cost:.4f})")


class BudgetExceeded(Exception):
    pass


def _user_message(facts: dict, turns: list[dict], extra: dict | None = None) -> str:
    body = {"facts": facts, "customer_messages": [t["text"] for t in turns if t["role"] == "customer"]}
    if extra:
        body.update(extra)
    return json.dumps(body, indent=1, ensure_ascii=False)


@dataclass
class Outcome:
    value: dict | None  # validated intent (interpret) or patched intent (revise); None if unusable
    problems: list[str]
    attempts: int
    decision: str = "changed"  # revise: changed | declined
    horizon_only: bool = False


def _horizon_only(problems: list[str]) -> bool:
    return bool(problems) and all("outside the bookable horizon" in p for p in problems)


async def interpret(model: Model, facts: dict, turns: list[dict], repair: bool = True) -> Outcome:
    instructions = Path(INTERPRET_PROMPT).read_text()
    user = _user_message(facts, turns)
    said = " \n ".join(t["text"] for t in turns if t["role"] == "customer")
    extra: list[dict] = []
    problems: list[str] = []
    for attempt in (1, 2) if repair else (1,):
        args, text = await model.tool_call("interpret" if attempt == 1 else "interpret_repair", instructions, user,
                                           RECORD_TOOL, extra)
        if args is None:
            problems = ["no record_intent call" + (f"; replied with text: {text[:200]}" if text else "")]
        else:
            draft = I.assign_ids(copy.deepcopy(args))
            draft.pop("approved_slots", None)
            problems = I.validate(draft, facts, said)
            if not problems:
                return Outcome(draft, [], attempt)
            if _horizon_only(problems):  # not repairable: the date the customer asked for cannot be booked
                return Outcome(None, problems, attempt, horizon_only=True)
        extra = [{"role": "assistant", "content": json.dumps(args) if args else text[:1000]},
                 {"role": "user", "content": "That intent cannot be used:\n- " + "\n- ".join(problems)
                  + "\nCall record_intent again with a corrected intent. Never drop something the customer said "
                    "just to make it pass; quote their exact words."}]
    return Outcome(None, problems, 2)


def proposal_view(question: dict | None, names: dict[int, str]) -> dict | None:
    if not question:
        return None
    return {"question": question["text"], "kind": question["kind"],
            "proposed_slots": [{"slot_id": c["slot_id"], "starts_at": c["starts_at"],
                                "technician": names.get(c["technician_id"], str(c["technician_id"]))}
                               for c in question.get("candidates", [])],
            "unclear_index": question.get("unclear_index")}


async def revise(model: Model, facts: dict, turns: list[dict], current: dict, question: dict | None,
                 names: dict[int, str]) -> Outcome:
    instructions = Path(REVISE_PROMPT).read_text()
    user = _user_message(facts, turns[:-1], {
        "current_intent": current, "our_last_question": proposal_view(question, names),
        "new_customer_message": turns[-1]["text"]})
    said = " \n ".join(t["text"] for t in turns if t["role"] == "customer")
    extra: list[dict] = []
    problems: list[str] = []
    for attempt in (1, 2):
        args, text = await model.tool_call("revise" if attempt == 1 else "revise_repair", instructions, user,
                                           REVISE_TOOL, extra)
        if args is None:
            problems = ["no revise_intent call" + (f"; replied with text: {text[:200]}" if text else "")]
        else:
            try:
                new, decision = I.apply_patch(current, args, question)
            except (I.PatchError, I.jsonschema.ValidationError) as exc:
                problems = [f"operations cannot be applied: {str(exc)[:300]}"]
            else:
                if decision == "declined":
                    return Outcome(current, [], attempt, decision="declined")
                problems = I.validate(new, facts, said)
                if not problems:
                    return Outcome(new, [], attempt)
                if _horizon_only(problems):
                    return Outcome(None, problems, attempt, horizon_only=True)
        extra = [{"role": "assistant", "content": json.dumps(args) if args else text[:1000]},
                 {"role": "user", "content": "Those operations cannot be used:\n- " + "\n- ".join(problems)
                  + "\nCall revise_intent again with corrected operations. Quote the customer's exact words."}]
    return Outcome(None, problems, 2)
