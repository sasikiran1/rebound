"""Typed customer intent: the contract between the model and deterministic code.

The model proposes an intent; nothing here trusts it. `validate` checks it against the customer's own words and
SQL-backed facts (history, technician roster, calendar): every rule must quote what the customer actually said, every
technician must be named by the customer or grounded through a reference to a real past appointment, dates and time
windows must be real and possible. `apply_patch` turns a later reply into a new version by explicit operations, so
rules the reply does not touch are preserved by construction.

Rule strengths:
  must      never book or propose a slot that breaks it ("I'm away Monday")
  flexible  never book a slot that breaks it without asking; may be proposed ("this week, check with me otherwise")
  prefer    try to satisfy; may be dropped without asking ("Dana if possible")
A rule with `only_on_dates` applies only on those dates ("Monday is okay, but not before four").
`alternatives` (at most two, in order) are conditional options: a slot must satisfy every global rule and every rule
of at least one alternative; earlier alternatives win. `unclear` holds phrases the model could not ground, each with
up to three readings; a slot acceptable under every reading needs no question.
"""

from __future__ import annotations

import copy
import datetime as dt
import re

import jsonschema

KINDS = (
    "dates_between", "on_dates", "not_on_dates", "on_weekdays", "not_on_weekdays",
    "start_at_or_after", "start_before", "technician_is", "technician_is_not",
)
STRENGTHS = ("must", "flexible", "prefer")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
FIELDS = {  # the one value field each kind uses
    "dates_between": ("date_from", "date_to"), "on_dates": ("dates",), "not_on_dates": ("dates",),
    "on_weekdays": ("weekdays",), "not_on_weekdays": ("weekdays",), "start_at_or_after": ("time",),
    "start_before": ("time",), "technician_is": ("technician_ids",), "technician_is_not": ("technician_ids",),
}
MAX_RULES = 12
MAX_ALTERNATIVES = 2
MAX_READINGS = 3

DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
TIME = {"type": "string", "pattern": r"^([01]\d|2[0-3]):[0-5]\d$"}
RULE = {
    "type": "object",
    "properties": {
        "kind": {"enum": list(KINDS)},
        "strength": {"enum": list(STRENGTHS)},
        "quote": {"type": "string", "minLength": 1, "maxLength": 200,
                  "description": "The customer's exact words this rule comes from."},
        "date_from": DATE, "date_to": DATE,
        "dates": {"type": "array", "items": DATE, "minItems": 1, "maxItems": 14},
        "weekdays": {"type": "array", "items": {"enum": list(WEEKDAYS)}, "minItems": 1, "maxItems": 7},
        "time": TIME,
        "technician_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 6},
        "only_on_dates": {"type": "array", "items": DATE, "minItems": 1, "maxItems": 7,
                          "description": "Optional, time and technician rules only: the rule applies only on these "
                                         "dates."},
        "from_appointment": {"type": "integer",
                             "description": "Optional: appointment_id of the past visit the customer pointed at."},
        "id": {"type": "string"},
    },
    "required": ["kind", "strength", "quote"],
    "additionalProperties": False,
}
RULES = {"type": "array", "items": RULE, "maxItems": MAX_RULES}
INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "rules": RULES,
        "alternatives": {
            "type": "array", "maxItems": MAX_ALTERNATIVES + 1,  # two from the first message, one more from a reply
            "items": {"type": "object", "properties": {"label": {"type": "string"}, "rules": {**RULES, "minItems": 1}},
                      "required": ["label", "rules"], "additionalProperties": False},
        },
        "unclear": {
            "type": "array", "maxItems": 2,
            "items": {"type": "object",
                      "properties": {
                          "phrase": {"type": "string"},
                          "readings": {"type": "array", "maxItems": MAX_READINGS, "items": {
                              "type": "object",
                              "properties": {"label": {"type": "string"}, "rules": RULES},
                              "required": ["label", "rules"], "additionalProperties": False}}},
                      "required": ["phrase", "readings"], "additionalProperties": False},
        },
        "summary": {"type": "string"},
        "approved_slots": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["rules"],
    "additionalProperties": False,
}

OPS = ("add_rule", "remove_rule", "replace_rule", "add_alternative", "remove_alternative", "choose_reading",
       "accept_proposal", "decline")
PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "ops": {"type": "array", "maxItems": 8, "items": {
            "type": "object",
            "properties": {
                "op": {"enum": list(OPS)},
                "rule_id": {"type": "string"},
                "rule": RULE,
                "alternative": {"type": "integer", "minimum": 1, "maximum": MAX_ALTERNATIVES + 1},
                "label": {"type": "string"},
                "rules": {**RULES, "minItems": 1},
                "unclear_index": {"type": "integer", "minimum": 0},
                "reading_index": {"type": "integer", "minimum": 0},
                "slot_id": {"type": "integer"},
            },
            "required": ["op"], "additionalProperties": False}},
        "summary": {"type": "string"},
    },
    "required": ["ops"],
    "additionalProperties": False,
}


def normalize(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[^a-z0-9:' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def all_rules(intent: dict):
    """Every rule with where it lives: ('global', None), ('alternative', i) or ('reading', (u, r))."""
    for rule in intent.get("rules", []):
        yield rule, ("global", None)
    for i, alt in enumerate(intent.get("alternatives", [])):
        for rule in alt["rules"]:
            yield rule, ("alternative", i)
    for u, item in enumerate(intent.get("unclear", [])):
        for r, reading in enumerate(item["readings"]):
            for rule in reading["rules"]:
                yield rule, ("reading", (u, r))


def assign_ids(intent: dict) -> dict:
    """Give every rule a stable id (r1, r2, ...). Existing ids are kept; new rules get the next free number."""
    used = {int(r["id"][1:]) for r, _ in all_rules(intent) if re.fullmatch(r"r\d+", r.get("id", ""))}
    n = max(used, default=0)
    seen = set()
    for rule, _ in all_rules(intent):
        if not re.fullmatch(r"r\d+", rule.get("id", "")) or rule["id"] in seen:
            n += 1
            rule["id"] = f"r{n}"
        seen.add(rule["id"])
    return intent


def _date(s: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _rule_problems(rule: dict, where: str, facts: dict, said: str | None, refs: dict) -> list[str]:
    p = []
    kind = rule["kind"]
    tag = f"{where} rule {rule.get('id', '')} ({kind})".replace("  ", " ")
    for f in FIELDS[kind]:
        if f not in rule:
            p.append(f"{tag}: needs field {f!r}")
    stray = [f for f in ("date_from", "date_to", "dates", "weekdays", "time", "technician_ids")
             if f in rule and f not in FIELDS[kind]]
    if stray:
        p.append(f"{tag}: field(s) {stray} do not belong to kind {kind!r}")
    if p:
        return p
    today = _date(facts["today"])
    last = today + dt.timedelta(days=facts["horizon_days"])
    dates = [rule[f] for f in ("date_from", "date_to") if f in rule] + rule.get("dates", []) + rule.get(
        "only_on_dates", [])
    for s in dates:
        d = _date(s)
        if d is None:
            p.append(f"{tag}: {s!r} is not a real date")
        elif kind != "not_on_dates" and not today <= d <= last:
            p.append(f"{tag}: {s} is outside the bookable horizon {today}..{last}")
    if kind == "dates_between" and not p and _date(rule["date_from"]) > _date(rule["date_to"]):
        p.append(f"{tag}: date_from {rule['date_from']} is after date_to {rule['date_to']}")
    if kind in DATE_KINDS and rule.get("only_on_dates"):
        p.append(f"{tag}: only_on_dates is for time and technician rules; a date rule already names its dates")
    if said is not None and normalize(rule["quote"]) not in said:
        p.append(f"{tag}: quote {rule['quote']!r} is not in the customer's words; quote them exactly")
    ref = None
    if rule.get("from_appointment") is not None:
        ref = refs.get(rule["from_appointment"])
        if ref is None:
            p.append(f"{tag}: appointment {rule['from_appointment']} is not one of this customer's past visits")
    if kind in ("technician_is", "technician_is_not") and said is None:  # form: picked from the roster
        p += [f"{tag}: technician {tid} is not on the roster" for tid in rule["technician_ids"]
              if tid not in {t["technician_id"] for t in facts["technicians"]}]
    elif kind in ("technician_is", "technician_is_not"):
        roster = {t["technician_id"]: t["name"] for t in facts["technicians"]}
        for tid in rule["technician_ids"]:
            if tid not in roster:
                p.append(f"{tag}: technician {tid} is not on the roster")
                continue
            if ref is not None:
                if ref["technician_id"] != tid:
                    p.append(f"{tag}: past visit {ref['appointment_id']} was with technician "
                             f"{ref['technician_id']} ({ref['technician_name']}), not {tid}")
                continue
            if not _named(roster[tid], said):
                p.append(f"{tag}: technician {tid} ({roster[tid]}) was not named by the customer; set "
                         f"from_appointment if they pointed at a past visit, or leave the technician out")
        if where != "unclear reading":
            p += _name_collisions(rule, roster, said, tag)
    return p


DATE_KINDS = ("dates_between", "on_dates", "not_on_dates")


def _words(name: str) -> list[str]:
    return [w for w in normalize(name).split() if len(w) > 2]


def _named(name: str, said: str) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", said) for w in _words(name))


def _name_collisions(rule: dict, roster: dict[int, str], said: str, tag: str) -> list[str]:
    """A name the customer used that fits several technicians, and nothing else they said tells them apart."""
    p = []
    for tid in rule["technician_ids"]:
        if rule.get("from_appointment") is not None or tid not in roster:  # unknown ids are reported already
            continue
        for w in _words(roster[tid]):
            if not re.search(rf"\b{re.escape(w)}\b", said):
                continue
            same = [t for t, n in roster.items() if w in _words(n)]
            told_apart = [t for t in same if any(re.search(rf"\b{re.escape(x)}\b", said)
                                                 for x in _words(roster[t]) if x != w)]
            if len(same) > 1 and len(told_apart) != 1:
                names = ", ".join(roster[t] for t in same)
                p.append(f"{tag}: the customer's {w!r} fits {len(same)} technicians ({names}); do not guess or "
                         f"merge them: put the phrase in `unclear` with one reading per technician")
                return p
    return p


def _window_problems(rules: list[dict], where: str) -> list[str]:
    """A time window that no start time can satisfy, among hard rules with the same date scope."""
    p = []
    hard = [r for r in rules if r["strength"] != "prefer"]
    for a in (r for r in hard if r["kind"] == "start_at_or_after"):
        for b in (r for r in hard if r["kind"] == "start_before"):
            same_scope = sorted(a.get("only_on_dates", [])) == sorted(b.get("only_on_dates", []))
            if same_scope and a.get("time", "") >= b.get("time", "~"):
                p.append(f"{where}: no start time is both at or after {a['time']} and before {b['time']}")
    return p


def validate(intent: dict, facts: dict, customer_text: str | None) -> list[str]:
    """Problems that make the intent unusable; empty if it may be stored. `customer_text` is every customer turn;
    None for a structured form submission (no words to quote; technicians come from the roster picker)."""
    try:
        jsonschema.validate(intent, INTENT_SCHEMA)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(x) for x in exc.absolute_path)
        return [f"schema at {path or 'top'}: {exc.message[:200]}"]
    said = normalize(customer_text) if customer_text is not None else None
    refs = {a["appointment_id"]: a for a in facts["history"]}
    p = []
    for rule, (kind, where) in all_rules(intent):
        label = f"alternative {where + 1}" if kind == "alternative" else kind.replace("reading", "unclear reading")
        p += _rule_problems(rule, label, facts, said, refs)
    if not p:
        p += _window_problems(intent.get("rules", []), "global rules")
        for i, alt in enumerate(intent.get("alternatives", [])):
            p += _window_problems(intent.get("rules", []) + alt["rules"], f"alternative {i + 1}")
    return p


class PatchError(ValueError):
    pass


def apply_patch(intent: dict, patch: dict, proposal: dict | None) -> tuple[dict, str]:
    """New version of `intent` from explicit operations. Returns (intent, decision): decision is 'declined' if the
    customer declined, else 'changed'. Rules not named by an operation are carried over unchanged."""
    jsonschema.validate(patch, PATCH_SCHEMA)
    new = copy.deepcopy(intent)
    new.setdefault("alternatives", [])
    new.setdefault("unclear", [])
    new.setdefault("approved_slots", [])
    by_id = {r["id"]: (r, loc) for r, loc in all_rules(new)}
    if any(op["op"] == "decline" for op in patch["ops"]):
        return new, "declined"
    dropped_unclear = set()
    for op in patch["ops"]:
        kind = op["op"]
        if kind == "add_rule":
            if "rule" not in op:
                raise PatchError("add_rule needs 'rule'")
            rule = {k: v for k, v in op["rule"].items() if k != "id"}
            if op.get("alternative"):
                if op["alternative"] > len(new["alternatives"]):
                    raise PatchError(f"alternative {op['alternative']} does not exist")
                new["alternatives"][op["alternative"] - 1]["rules"].append(rule)
            else:
                new["rules"].append(rule)
        elif kind in ("remove_rule", "replace_rule"):
            if op.get("rule_id") not in by_id:
                raise PatchError(f"{kind}: unknown rule_id {op.get('rule_id')!r}")
            target, _ = by_id[op["rule_id"]]
            container = _container(new, target)
            idx = next(i for i, r in enumerate(container) if r is target)
            if kind == "remove_rule":
                container.pop(idx)
            else:
                if "rule" not in op:
                    raise PatchError("replace_rule needs 'rule'")
                if op["rule"]["kind"] != target["kind"]:  # a different kind would silently drop the old limit
                    raise PatchError(f"replace_rule must keep the kind of {target['id']} ({target['kind']}); use "
                                     f"add_rule for a new requirement, and remove_rule only if the customer dropped "
                                     f"{target['id']}")
                container[idx] = {**{k: v for k, v in op["rule"].items() if k != "id"}, "id": target["id"]}
        elif kind == "add_alternative":
            if not op.get("rules"):
                raise PatchError("add_alternative needs 'rules'")
            if len(new["alternatives"]) > MAX_ALTERNATIVES:
                raise PatchError(f"at most {MAX_ALTERNATIVES + 1} alternatives")
            new["alternatives"].append({"label": op.get("label", "added by the customer's reply"),
                                        "rules": [{k: v for k, v in r.items() if k != "id"} for r in op["rules"]]})
        elif kind == "remove_alternative":
            i = op.get("alternative")
            if not i or i > len(new["alternatives"]):
                raise PatchError(f"remove_alternative: alternative {i} does not exist")
            new["alternatives"][i - 1]["rules"] = []  # dropped below, after every op has used the old numbering
        elif kind == "choose_reading":
            u, r = op.get("unclear_index"), op.get("reading_index")
            if u is None or r is None or u >= len(new["unclear"]) or r >= len(new["unclear"][u]["readings"]):
                raise PatchError("choose_reading: no such unclear item or reading")
            new["rules"] += copy.deepcopy(new["unclear"][u]["readings"][r]["rules"])
            dropped_unclear.add(u)
        elif kind == "accept_proposal":
            offered = [c["slot_id"] for c in (proposal or {}).get("candidates", [])]
            slot = op.get("slot_id", offered[0] if len(offered) == 1 else None)
            if slot is None or slot not in offered:
                raise PatchError(f"accept_proposal: slot {slot} was not one of the proposed slots {offered}")
            new["approved_slots"].append(slot)
    new["alternatives"] = [a for a in new["alternatives"] if a["rules"]]
    new["unclear"] = [u for i, u in enumerate(new["unclear"]) if i not in dropped_unclear]
    if "summary" in patch:
        new["summary"] = patch["summary"]
    return assign_ids(new), "changed"


def _container(intent: dict, target: dict) -> list:
    if any(r is target for r in intent["rules"]):
        return intent["rules"]
    for alt in intent["alternatives"]:
        if any(r is target for r in alt["rules"]):
            return alt["rules"]
    for item in intent["unclear"]:
        for reading in item["readings"]:
            if any(r is target for r in reading["rules"]):
                return reading["rules"]
    raise PatchError("rule not found")


def explain_rule(rule: dict, names: dict[int, str], strength: bool = True) -> str:
    def day(s):
        d = dt.date.fromisoformat(s)
        return f"{d.strftime('%a %b')} {d.day}"

    def hhmm(s):
        return dt.datetime.strptime(s, "%H:%M").strftime("%I:%M %p").lstrip("0")
    def days(ds):  # consecutive runs of three or more dates read as a range
        ds = sorted(ds)
        runs, start = [], 0
        for i in range(1, len(ds) + 1):
            if i == len(ds) or dt.date.fromisoformat(ds[i]) - dt.date.fromisoformat(ds[i - 1]) != dt.timedelta(1):
                run = ds[start:i]
                runs.append(f"{day(run[0])} to {day(run[-1])}" if len(run) > 2 else " or ".join(day(d) for d in run))
                start = i
        return " or ".join(runs)
    k = rule["kind"]
    who = ", ".join(names.get(t, str(t)) for t in rule.get("technician_ids", []))
    text = {
        "dates_between": lambda: f"between {day(rule['date_from'])} and {day(rule['date_to'])}",
        "on_dates": lambda: "on " + days(rule["dates"]),
        "not_on_dates": lambda: "not on " + days(rule["dates"]),
        "on_weekdays": lambda: "on " + "/".join(w.title() for w in rule["weekdays"]),
        "not_on_weekdays": lambda: "not on " + "/".join(w.title() for w in rule["weekdays"]),
        "start_at_or_after": lambda: f"starting {hhmm(rule['time'])} or later",
        "start_before": lambda: f"starting before {hhmm(rule['time'])}",
        "technician_is": lambda: f"with {who}",
        "technician_is_not": lambda: f"not with {who}",
    }[k]()
    if rule.get("only_on_dates"):
        text += " (on " + days(rule["only_on_dates"]) + ")"
    return text + (" if possible" if rule["strength"] == "prefer" and strength else "")


def explain(intent: dict, names: dict[int, str]) -> str:
    """Deterministic plain-language reading of an intent: what the customer will be told was understood."""
    parts = [explain_rule(r, names) for r in intent.get("rules", [])]
    alts = intent.get("alternatives", [])
    if alts:
        opts = ["; ".join(explain_rule(r, names) for r in a["rules"]) for a in alts]
        parts.append("first choice " + opts[0] + "".join(f", otherwise {o}" for o in opts[1:]))
    for u in intent.get("unclear", []):
        parts.append(f"unclear: “{u['phrase']}”")
    if intent.get("approved_slots"):
        parts.append(f"approved slot(s) {', '.join(str(s) for s in intent['approved_slots'])}")
    return "; ".join(parts) or "any open time"


def diff(old: dict, new: dict) -> dict:
    """Rule ids added, removed and changed between two versions (for the approval record and the screen)."""
    a = {r["id"]: r for r, _ in all_rules(old)}
    b = {r["id"]: r for r, _ in all_rules(new)}
    return {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
            "changed": sorted(k for k in set(a) & set(b) if a[k] != b[k]),
            "approved_slots": sorted(set(new.get("approved_slots", [])) - set(old.get("approved_slots", [])))}
