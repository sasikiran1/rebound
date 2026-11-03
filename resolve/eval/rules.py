"""Rule-based interpreter: the no-model baseline for Resolve.

It turns customer text into the same typed intent the model produces, with hand-written patterns for English
scheduling language: weekdays, ordinal dates, "this/next week", parts of the day, "after/before/between" times,
negation ("except", "anyone but", "not before"), ordered fallbacks ("if not", "otherwise"), technician names
(ambiguous first names become `unclear`), and references to booking history ("same technician as last time",
"whoever did my boiler service in October"). A later message overrides the kinds of rules it mentions (dates, times,
technicians) and keeps the rest; replies that refuse or postpone decline.

It plugs into the same service, validator, evaluator, worker and SQL oracle as the model (`Resolve(interpret_fn=...,
revise_fn=...)`), so only the interpretation differs. Developed on the dev cases only, then frozen before its single
held-out run; its author had read the held-out conversations, which can only favor it.
"""

from __future__ import annotations

import copy
import datetime as dt
import re

from resolve import intent as I
from resolve.interpret import Outcome

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
SHORT = {d: d[:3] for d in WEEKDAYS}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
           "ten": 10, "eleven": 11, "twelve": 12, "noon": 12}
HOUR = r"(\d{1,2}(?::\d{2})?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|noon)\s*(am|pm|o'clock)?"
FALLBACK = re.compile(
    r"(?:\bif (?:that's|that is|it's|it is) not possible\b|\bif not\b|\botherwise\b|\bfailing that\b"
    r"|\bif \w+ (?:can't|cannot|isn't|is not|can not)\b[^,.;]*)")
DECLINE = re.compile(r"\b(leave it|don't change|do not change|not sure|call you back|get back to you|never mind|"
                     r"no thanks)\b|^no\b")
MUST = re.compile(r"\b(only|i'm away|i am away|traveling|travelling|nothing later than|can't do|cannot do)\b")
PREFER = re.compile(r"\b(if possible|ideally|preferably|would be best)\b")
PERMISSION = re.compile(r"\b(without checking|check with me|ask me)\b")


def _lower(text: str) -> str:
    """Lowercase with plain quotes, punctuation kept (for splitting into clauses and fallback segments)."""
    return text.lower().replace("’", "'").replace("‘", "'").replace("—", ",").replace("–", ",")


def _hhmm(token: str, suffix: str | None) -> str:
    if token in NUMBERS:
        h, m = NUMBERS[token], 0
    else:
        h, _, mm = token.partition(":")
        h, m = int(h), int(mm or 0)
    if suffix == "pm" and h < 12:
        h += 12
    elif suffix != "am" and 1 <= h <= 7:  # "after three" means 15:00 in a working day
        h += 12
    return f"{h:02d}:{m:02d}"


def _plus30(t: str) -> str:
    h, m = map(int, t.split(":"))
    m += 30
    return f"{h + m // 60:02d}:{m % 60:02d}"


def _rule(kind: str, quote: str, strength: str = "flexible", **fields) -> dict:
    return {"kind": kind, "strength": strength, "quote": quote, **fields}


def _dates(clause: str, facts: dict) -> tuple[list[str], list[dict]]:
    """(explicit dates, range rules) named in a clause."""
    today = dt.date.fromisoformat(facts["today"])
    out, ranges = [], []
    if re.search(r"\bthis week or next\b", clause):
        ranges.append(("dates_between", facts["this_week"]["from"], facts["next_week"]["to"], "this week or next"))
    elif "next week" in clause:
        ranges.append(("dates_between", facts["next_week"]["from"], facts["next_week"]["to"], "next week"))
    elif "this week" in clause:
        ranges.append(("dates_between", facts["this_week"]["from"], facts["this_week"]["to"], "this week"))
    for m in re.finditer(r"\b(?:(" + "|".join(MONTHS) + r")\s+)?(\d{1,2})(?:st|nd|rd|th)\b", clause):
        month = MONTHS.index(m.group(1)) + 1 if m.group(1) else today.month
        try:
            out.append(dt.date(today.year, month, int(m.group(2))).isoformat())
        except ValueError:
            continue
    if not out:  # a weekday name only counts when no explicit date was given
        for d in WEEKDAYS:
            if re.search(rf"\b{d}\b", clause):
                out.append(facts["next_date_for_weekday"][d.title()])
    for word, delta in (("today", 0), ("tomorrow", 1)):
        if re.search(rf"\b{word}\b", clause):
            out.append((today + dt.timedelta(days=delta)).isoformat())
    return list(dict.fromkeys(out)), [{"kind": k, "date_from": a, "date_to": b, "quote": q} for k, a, b, q in ranges]


def _times(clause: str, facts: dict) -> list[dict]:
    rules = []
    m = re.search(rf"\bbetween {HOUR} and {HOUR}", clause)
    if m:
        a, b = _hhmm(m.group(1), m.group(2)), _hhmm(m.group(3), m.group(4) or m.group(2))
        return [_rule("start_at_or_after", m.group(0), time=a), _rule("start_before", m.group(0), time=b)]
    if re.search(r"\bsame time\b", clause) and re.search(r"\b(current|existing|now)\b", clause):
        start = facts["appointment_being_moved"]["starts_at"][11:16]
        q = re.search(r"same time[^.,;]*", clause).group(0)
        return [_rule("start_at_or_after", q, time=start), _rule("start_before", q, time=_plus30(start))]
    for m in re.finditer(rf"\b(?:nothing|not) (?:before|earlier than) {HOUR}", clause):
        rules.append(_rule("start_at_or_after", m.group(0), time=_hhmm(m.group(1), m.group(2))))
    for m in re.finditer(rf"\b{HOUR} or later\b", clause):
        rules.append(_rule("start_at_or_after", m.group(0), time=_hhmm(m.group(1), m.group(2))))
    for m in re.finditer(rf"(?<!nothing )(?<!not )\b(?:after|from) {HOUR}", clause):
        rules.append(_rule("start_at_or_after", m.group(0), time=_hhmm(m.group(1), m.group(2))))
    for m in re.finditer(rf"(?<!nothing )(?<!not )\b(?:before|by) {HOUR}", clause):
        rules.append(_rule("start_before", m.group(0), time=_hhmm(m.group(1), m.group(2))))
    m = re.search(rf"\bat {HOUR}", clause)
    if m and not rules:
        t = _hhmm(m.group(1), m.group(2))
        rules += [_rule("start_at_or_after", m.group(0), time=t), _rule("start_before", m.group(0), time=_plus30(t))]
    if not rules:
        for word, kind, t in (("morning", "start_before", "12:00"), ("afternoon", "start_at_or_after", "12:00"),
                              ("evening", "start_at_or_after", "17:00")):
            m = re.search(rf"\b{word}\b", clause)
            if m and not re.search(rf"\b(not|no|except)\b[^,.;]{{0,25}}\b{word}\b", clause):
                rules.append({**_rule(kind, word, time=t), "_part_of_day": True})
    return rules


def _history_refs(clause: str, facts: dict) -> list[dict] | None:
    """Past visits the clause points at, or None if it points at none."""
    hist = sorted((h for h in facts["history"] if h["status"] == "completed"), key=lambda h: h["starts_at"])
    if not hist:
        return None
    if re.search(r"\b(last time|last visit|my last|came last|previous visit)\b", clause):
        return [hist[-1]]
    month = next((i + 1 for i, mo in enumerate(MONTHS) if re.search(rf"\b(in|from) {mo}\b", clause)), None)
    service = [h for h in hist if any(w in clause for w in I.normalize(h["service_name"]).split() if len(w) > 3)]
    if month is None and not service:
        return None
    picks = [h for h in (service or hist) if month is None or int(h["starts_at"][5:7]) == month]
    return picks or None


def _technicians(clause: str, facts: dict) -> tuple[list[dict], list[dict]]:
    """(rules, unclear items) for technicians named or pointed at in a clause."""
    roster = {t["technician_id"]: t["name"] for t in facts["technicians"]}
    negated_ctx = re.search(r"\b(anyone but|not|except|no)\b", clause)
    prefer = bool(PREFER.search(clause))
    rules, unclear = [], []
    refs = _history_refs(clause, facts) if re.search(r"\b(same|whoever|who came|who did|technician|person|"
                                                     r"one who)\b", clause) else None
    if refs:
        techs = list(dict.fromkeys(h["technician_id"] for h in refs))
        kind = "technician_is_not" if re.search(r"\bnot\b[^,.;]*\b(technician|person|one)\b", clause) \
            else "technician_is"
        if len(techs) == 1:
            h = next(h for h in refs if h["technician_id"] == techs[0])
            rules.append(_rule(kind, clause.strip(), "prefer" if prefer else "flexible", technician_ids=techs,
                               from_appointment=h["appointment_id"]))
        else:
            unclear.append({"phrase": clause.strip(), "readings": [
                {"label": roster.get(t, str(t)), "rules": [_rule(kind, clause.strip(), technician_ids=[t])]}
                for t in techs[:3]]})
        return rules, unclear
    seen = set()
    for tid, name in roster.items():
        first, *rest = I.normalize(name).split()
        m = re.search(rf"\b{re.escape(first)}\b", clause)
        if not m or tid in seen:
            continue
        same = [t for t, n in roster.items() if I.normalize(n).split()[0] == first]
        named = [t for t in same if any(re.search(rf"\b{re.escape(w)}\b", clause)
                                        for w in I.normalize(roster[t]).split()[1:])]
        seen.update(same)
        before = clause[:m.start()]
        kind = "technician_is_not" if negated_ctx and re.search(r"(anyone but|not|except|no)\W+(\w+\W+){0,2}$",
                                                                before) else "technician_is"
        strength = "prefer" if prefer else "flexible"
        if len(same) > 1 and len(named) != 1:
            unclear.append({"phrase": first, "readings": [
                {"label": roster[t], "rules": [_rule(kind, first, strength, technician_ids=[t])]} for t in same[:3]]})
        else:
            rules.append(_rule(kind, first, strength, technician_ids=named or [tid]))
    return rules, unclear


def parse_text(text: str, facts: dict) -> dict:
    """One message (or one fallback segment) to rules and unclear items."""
    rules, unclear = [], []
    for clause in re.split(r"[.;!?]|\bbut\b", _lower(text)):  # split before normalizing: it drops punctuation
        clause = I.normalize(clause)
        if not clause:
            continue
        must = bool(MUST.search(clause))
        negated_dates = re.search(r"\b(except|not on|away on|can't do|cannot do|i'm away|i am away)\b", clause)
        if not PERMISSION.search(clause):  # "don't move it to next week without checking": a permission, not a date
            dates, ranges = _dates(clause, facts)
            for r in ranges:
                rules.append(_rule("dates_between", r["quote"], "must" if must else "flexible",
                                   date_from=r["date_from"], date_to=r["date_to"]))
            if dates:
                weekday_only = negated_dates and not re.search(r"\d", clause)
                if weekday_only:
                    days = [SHORT[d] for d in WEEKDAYS if re.search(rf"\b{d}\b", clause)]
                    rules.append(_rule("not_on_weekdays", clause, weekdays=days))
                else:
                    rules.append(_rule("not_on_dates" if negated_dates else "on_dates", clause,
                                       "must" if must and not negated_dates else "flexible", dates=dates))
        times = _times(clause, facts)
        if times and PREFER.search(text) and re.search(r"\b(ok|okay|fine|too)\b", I.normalize(text)):
            times = [{**t, "strength": "prefer"} for t in times]  # "ideally X, but Y is OK too"
        rules += times
        trules, tunclear = _technicians(clause, facts)
        rules += trules
        unclear += tunclear
    if any(r["kind"] in GROUPS["time"] and not r.get("_part_of_day") for r in rules):
        rules = [r for r in rules if not r.get("_part_of_day")]  # an explicit clock time outranks "morning"
    return {"rules": [{k: v for k, v in r.items() if k != "_part_of_day"} for r in rules], "unclear": unclear}


def parse_message(text: str, facts: dict) -> dict:
    """A whole message: ordered fallbacks become alternatives."""
    parts = [re.sub(r"^\W*then\b", "", p.strip(" ,")) for p in FALLBACK.split(_lower(text))]
    segs = [parse_text(p, facts) for p in parts if p.strip()]
    segs = [s for s in segs if s["rules"] or s["unclear"]]
    if len(segs) >= 2:
        return {"rules": [], "alternatives": [{"label": f"option {i + 1}", "rules": s["rules"]}
                                              for i, s in enumerate(segs[:2]) if s["rules"]],
                "unclear": [u for s in segs for u in s["unclear"]]}
    return segs[0] if segs else {"rules": [], "unclear": []}


GROUPS = {"date": I.DATE_KINDS + ("on_weekdays", "not_on_weekdays"),
          "time": ("start_at_or_after", "start_before"), "tech": ("technician_is", "technician_is_not")}


def _group(kind: str) -> str:
    return next(g for g, kinds in GROUPS.items() if kind in kinds)


def override(current: dict, update: dict) -> dict:
    """A later message replaces the kinds of rules it mentions and keeps every other rule."""
    new = copy.deepcopy(current)
    new.setdefault("alternatives", [])
    new.setdefault("unclear", [])
    groups = {_group(r["kind"]) for r in update["rules"]} | {
        _group(r["kind"]) for a in update.get("alternatives", []) for r in a["rules"]}
    if update.get("alternatives"):
        new["alternatives"] = update["alternatives"]
    elif "date" in groups:
        new["alternatives"] = []  # a new date replaces the earlier options
    new["rules"] = [r for r in new["rules"] if _group(r["kind"]) not in groups] + update["rules"]
    for alt in new["alternatives"]:
        alt["rules"] = [r for r in alt["rules"] if _group(r["kind"]) not in groups or update.get("alternatives")]
    new["alternatives"] = [a for a in new["alternatives"] if a["rules"]]
    if "tech" in groups:
        new["unclear"] = []
    new["unclear"] += update.get("unclear", [])
    new.pop("approved_slots", None) if "date" in groups or "time" in groups else None
    for r, _ in I.all_rules(new):
        r.pop("id", None) if r not in [x for x, _ in I.all_rules(current)] else None
    return I.assign_ids(new)


def _said(turns: list[dict]) -> str:
    return " \n ".join(t["text"] for t in turns if t["role"] == "customer")


async def interpret(_model, facts: dict, turns: list[dict], repair: bool = True) -> Outcome:
    customer = [t["text"] for t in turns if t["role"] == "customer"]
    intent = I.assign_ids(parse_message(customer[0], facts))
    for later in customer[1:]:
        intent = override(intent, parse_message(later, facts))
    problems = I.validate(intent, facts, _said(turns))
    if problems:
        horizon = all("outside the bookable horizon" in p for p in problems)
        return Outcome(None, problems, 0, horizon_only=horizon)
    return Outcome(intent, [], 0)


async def revise(_model, facts: dict, turns: list[dict], current: dict, question: dict | None,
                 names: dict[int, str]) -> Outcome:
    text = turns[-1]["text"]
    if DECLINE.search(I.normalize(text)):
        return Outcome(current, [], 0, decision="declined")
    update = parse_message(text, facts)
    if not update["rules"] and not update.get("alternatives") and not update["unclear"]:
        if question and question.get("kind") == "permission" and re.search(r"\b(yes|ok|okay|sure|fine|works)\b",
                                                                            I.normalize(text)):
            new, _ = I.apply_patch(current, {"ops": [{"op": "accept_proposal",
                                                      "slot_id": question["candidates"][0]["slot_id"]}]}, question)
            return Outcome(new, [], 0)
        return Outcome(current, [], 0, decision="declined")
    new = override(current, update)
    problems = I.validate(new, facts, _said(turns))
    if problems:
        horizon = all("outside the bookable horizon" in p for p in problems)
        return Outcome(None, problems, 0, horizon_only=horizon)
    return Outcome(new, [], 0)
