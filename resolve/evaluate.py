"""Deterministic availability evaluation: book, ask, or leave unresolved. No model.

Given a validated intent and the complete list of open slots, a slot is *authorized* when it satisfies every hard
(must and flexible) global rule, every hard rule of at least one alternative (if any), and is acceptable under every
reading of every unclear phrase; or when the customer explicitly approved that exact slot. The best authorized slot
is booked without a question. Otherwise the customer is asked, and the question is grounded in real slots:

  clarify    an unclear phrase changes the decision: ask which reading was meant
  permission the closest slots that keep every `must` rule but break some `flexible` ones: ask before booking
  no_options nothing open keeps the `must` rules: say so and ask whether anything can change
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

WEEKDAY = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def slot_date(slot: dict) -> str:
    return slot["starts_at"][:10]


def slot_time(slot: dict) -> str:
    return slot["starts_at"][11:16]


def holds(rule: dict, slot: dict) -> bool:
    d, t = slot_date(slot), slot_time(slot)
    if rule.get("only_on_dates") and d not in rule["only_on_dates"]:
        return True
    k = rule["kind"]
    if k == "dates_between":
        return rule["date_from"] <= d <= rule["date_to"]
    if k == "on_dates":
        return d in rule["dates"]
    if k == "not_on_dates":
        return d not in rule["dates"]
    wd = WEEKDAY[dt.date.fromisoformat(d).weekday()]
    if k == "on_weekdays":
        return wd in rule["weekdays"]
    if k == "not_on_weekdays":
        return wd not in rule["weekdays"]
    if k == "start_at_or_after":
        return t >= rule["time"]
    if k == "start_before":
        return t < rule["time"]
    if k == "technician_is":
        return slot["technician_id"] in rule["technician_ids"]
    if k == "technician_is_not":
        return slot["technician_id"] not in rule["technician_ids"]
    raise ValueError(f"unknown rule kind {k!r}")


def broken(rules: list[dict], slot: dict, strengths: tuple[str, ...]) -> list[dict]:
    return [r for r in rules if r["strength"] in strengths and not holds(r, slot)]


@dataclass
class Fit:
    option: int | None  # index of the first alternative the slot satisfies; 0 when there are no alternatives
    broken_must: list[dict]
    broken_flexible: list[dict]
    broken_prefer: list[dict]
    unclear_ok: bool  # acceptable under every reading of every unclear phrase


def fit(intent: dict, slot: dict) -> Fit:
    rules = intent.get("rules", [])
    alts = intent.get("alternatives", [])
    best = None
    for i, alt in enumerate(alts or [{"rules": []}]):
        scope = rules + alt["rules"]
        cand = (len(broken(scope, slot, ("must",))), len(broken(scope, slot, ("flexible",))), i)
        if best is None or cand < best[0]:
            best = (cand, scope)
    _, scope = best
    must, flex = broken(scope, slot, ("must",)), broken(scope, slot, ("flexible",))
    option = best[0][2] if not must and not flex else None
    unclear_ok = all(item["readings"] and all(not broken(rd["rules"], slot, ("must", "flexible"))
                                              for rd in item["readings"])
                     for item in intent.get("unclear", []))
    return Fit(option, must, flex, broken(scope, slot, ("prefer",)), unclear_ok)


def authorized(intent: dict, slot: dict) -> bool:
    if slot["slot_id"] in intent.get("approved_slots", []):
        return True
    f = fit(intent, slot)
    return f.option is not None and f.unclear_ok


@dataclass
class Decision:
    kind: str  # book | clarify | permission | no_options
    slot: dict | None = None
    candidates: list[dict] = field(default_factory=list)
    unclear_index: int | None = None
    question: str | None = None
    reason: str = ""


def describe(slot: dict, names: dict[int, str]) -> str:
    d = dt.datetime.fromisoformat(slot["starts_at"])
    hour = d.strftime("%I:%M %p").lstrip("0")
    who = names.get(slot["technician_id"], f"technician {slot['technician_id']}")
    return f"{d.strftime('%A %b')} {d.day} at {hour} with {who.split()[0]}"


def decide(intent: dict, slots: list[dict], names: dict[int, str]) -> Decision:
    """`slots` must be every open slot in the searched horizon (not a truncated page)."""
    ok = [s for s in slots if authorized(intent, s)]
    if ok:
        def rank(s):
            f = fit(intent, s)
            approved = s["slot_id"] in intent.get("approved_slots", [])
            return (0 if f.option is not None else 1, f.option or 0, len(f.broken_prefer), not approved,
                    s["starts_at"], s["technician_id"])
        best = min(ok, key=rank)
        return Decision("book", slot=best, reason=f"{len(ok)} authorized slot(s); best {describe(best, names)}")
    for u, item in enumerate(intent.get("unclear", [])):
        if not item["readings"]:
            return Decision("clarify", unclear_index=u, reason="unclear phrase without readings",
                            question=f"When you said “{item['phrase']}”, what did you mean exactly?")
        labels = " or ".join(f"({i + 1}) {rd['label']}" for i, rd in enumerate(item["readings"]))
        return Decision("clarify", unclear_index=u, reason="readings disagree on every open slot",
                        question=f"When you said “{item['phrase']}”, did you mean {labels}?")
    near = []
    for s in slots:
        f = fit(intent, s)
        if not f.broken_must and f.unclear_ok:
            near.append((len(f.broken_flexible), len(f.broken_prefer), s["starts_at"], s["technician_id"], s, f))
    if near:
        near.sort(key=lambda x: x[:4])
        fewest = near[0][0]
        picks = [x for x in near if x[0] == fewest][:2]
        quotes = []
        for x in picks:
            for r in x[5].broken_flexible:
                if r["quote"] not in quotes:
                    quotes.append(r["quote"])
        options = " or ".join(describe(x[4], names) for x in picks)
        said = "; ".join(f"“{q}”" for q in quotes)
        return Decision("permission", candidates=[x[4] for x in picks],
                        reason=f"no authorized slot; {len(near)} slot(s) keep every must rule",
                        question=f"Nothing open meets everything you asked for. The closest "
                                 f"{'option is' if len(picks) == 1 else 'options are'} {options}, which goes against "
                                 f"what you said: {said}. Would that work for you?")
    musts = [r["quote"] for r, _ in _hard_must(intent)]
    said = "; ".join(f"“{q}”" for q in dict.fromkeys(musts)) or "your request"
    return Decision("no_options", reason="no open slot keeps every must rule",
                    question=f"Nothing open fits {said}. Is there anything you can change, or should we leave your "
                             f"appointment where it is?")


def _hard_must(intent: dict):
    for r in intent.get("rules", []):
        if r["strength"] == "must":
            yield r, None
    for alt in intent.get("alternatives", []):
        for r in alt["rules"]:
            if r["strength"] == "must":
                yield r, alt
