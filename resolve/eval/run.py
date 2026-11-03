"""Run Resolve cases end to end against the live FieldDesk stack and judge each one in SQL.

    uv run python -m resolve.eval.run --split dev --arm resolve --model qwen3-14b-16k:latest --out runs/resolve/dev-q1

Arms (same fixtures, evaluator, store, worker and oracle):
  resolve  grounded interpretation (history + roster), one repair, clarification questions, corrections
  form     capable conditional form: the structured entries written in the case file; no model
  oneshot  one model call over the unprompted customer messages, no booking history, no repair, no questions

Customer turns are scripted: unprompted messages first, then one scripted reply per question asked; when the
replies run out the customer answers with GENERIC_REPLY. Turn delivery times are read from the database clock by
this harness, for the oracle. Every case writes one JSON record; nothing is dropped or retried silently.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import time
from pathlib import Path

import asyncpg

from harness import settings
from harness.provider import ProviderFailed
from resolve import intent as I
from resolve.api import FieldDesk
from resolve.eval import cases as C
from resolve.eval.oracle import verdict
from resolve.interpret import BudgetExceeded, Model, interpret
from resolve.models import make_model
from resolve.service import Resolve
from resolve.store import TERMINAL, Store
from resolve.worker import Worker

GENERIC_REPLY = "I don't have anything to add to what I already said."
CONFIRM_YES = "Yes, please book it."
CONFIRM_NO = "No, that time doesn't work for me."


def form_intent(case: C.Case, form: dict) -> dict:
    """Case-file form entries -> intent: aliases to ids, YAML dates to ISO strings, default strength."""
    def rule(r):
        r = {k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in r.items()}
        for k in ("dates", "only_on_dates"):
            if k in r:
                r[k] = [d.isoformat() if isinstance(d, dt.date) else d for d in r[k]]
        if "technician_ids" in r:
            r["technician_ids"] = case.tech_ids(r["technician_ids"])
        return {"strength": "flexible", "quote": "form", **r}
    out = {"rules": [rule(r) for r in form.get("rules", [])],
           "alternatives": [{"label": a["label"], "rules": [rule(r) for r in a["rules"]]}
                            for a in form.get("alternatives", [])],
           "summary": "form entry"}
    return I.assign_ids(out)


def form_fields(form: dict | None) -> int:
    """Exploratory effort count: values a user sets in the form (kind, each value, non-default strength, scope)."""
    if not form or form.get("decline") or form.get("unchanged"):
        return 1 if form else 0
    n = 0
    for r in form.get("rules", []) + [r for a in form.get("alternatives", []) for r in a["rules"]]:
        n += 1 + sum(1 for k in ("date_from", "date_to", "time") if k in r)
        n += len(r.get("dates", [])) + len(r.get("weekdays", [])) + len(r.get("technician_ids", []))
        n += ("strength" in r) + len(r.get("only_on_dates", []))
    return n + len(form.get("alternatives", []))


async def db_now(conn: asyncpg.Connection) -> dt.datetime:
    return await conn.fetchval("SELECT clock_timestamp()")


async def run_case(case: C.Case, arm: str, model: Model | None, store: Store, admin: asyncpg.Connection,
                   oracle: asyncpg.Connection, tag: str, confirm: bool = False) -> dict:
    rid = f"{tag}:{case.id}:{arm}"
    await C.seed(admin, case)
    await store.reset(rid)
    api = FieldDesk(settings.FIELDDESK_URL, actor=f"resolve-{arm}")
    if arm == "rules":  # same conversation flow; the rule-based interpreter replaces the model
        from resolve.eval import rules as R
        svc = Resolve(store, api, None, C.TODAY, C.HORIZON_DAYS, interpret_fn=R.interpret, revise_fn=R.revise)
    else:
        svc = Resolve(store, api, model, C.TODAY, C.HORIZON_DAYS)
    worker = Worker(store, api, owner=f"worker-{arm}")
    calls_before = len(model.log.calls) if model else 0
    t0 = time.perf_counter()
    times: list[dt.datetime] = []
    replies = iter(case.replies)
    record = {"case": case.id, "kind": case.kind, "arm": arm, "request_id": rid, "notes": []}
    effort = {"customer_turns": 0, "customer_words": 0, "form_fields": 0}

    async def say(text: str):
        times.append(await db_now(oracle))
        effort["customer_turns"] += 1
        effort["customer_words"] += len(text.split())
        return await svc.customer_says(rid, text)

    try:
        if arm == "form" and case.raw.get("form") is None:
            record["notes"].append("form cannot be completed by the customer (see case file)")
            times.append(await db_now(oracle))
        else:
            facts = await svc.load_facts(case.customer_id, case.appointment_id)
            if arm in ("resolve", "rules"):
                times.append(await db_now(oracle))
                effort["customer_turns"] += 1
                effort["customer_words"] += len(case.messages[0].split())
                await svc.open(rid, case.customer_id, case.appointment_id, case.region, case.service,
                               case.messages[0], facts, confirm=confirm)
                for m in case.messages[1:]:
                    await say(m)
            elif arm == "oneshot":
                bare = {**facts, "history": []}
                await store.create(rid, case.customer_id, case.appointment_id, case.region, case.service, C.TODAY,
                                   C.HORIZON_DAYS, facts)
                text = "\n".join(case.messages)
                times.append(await db_now(oracle))
                effort["customer_turns"] += len(case.messages)
                effort["customer_words"] += len(text.split())
                turns = [{"role": "customer", "text": m} for m in case.messages]
                for m in case.messages:
                    await store.add_turn(rid, "customer", m, "message")
                out = await interpret(model, bare, turns, repair=False)
                await store.event(rid, "interpret", attempts=out.attempts, problems=out.problems, intent=out.value)
                if out.value is None:
                    await store.set_state(rid, "unresolved", detail="; ".join(out.problems)[:1000])
                else:
                    await store.approve(rid, out.value, {"turn": 0, "by": "customer", "text": text}, None)
            else:  # form
                await store.create(rid, case.customer_id, case.appointment_id, case.region, case.service, C.TODAY,
                                   C.HORIZON_DAYS, facts)
                times.append(await db_now(oracle))
                fi = form_intent(case, case.raw["form"])
                problems = I.validate(fi, facts, None)
                if problems:
                    raise RuntimeError(f"case {case.id} form entry is invalid: {problems}")
                effort["form_fields"] += form_fields(case.raw["form"])
                await svc.approve_structured(rid, fi, "form", "form submitted")
            form_turns = iter(case.raw.get("form_turns") or [])
            if arm == "form":
                for _ in case.messages[1:]:
                    times.append(await db_now(oracle))
                    ft = next(form_turns)
                    effort["form_fields"] += form_fields(ft)
                    await svc.approve_structured(rid, form_intent(case, ft), "form", "form edited")
            if case.taken:
                await C.take(admin, case)
            for _ in range(10):
                req = await store.get(rid)
                if req is None or req["state"] in TERMINAL:
                    break
                if req["state"] == "ready":
                    await worker.run_once(rid)
                    continue
                if arm == "oneshot":
                    await store.set_state(rid, "unresolved", detail="one-shot arm cannot ask the customer")
                    record["notes"].append(f"would have asked: {req['question']['text']}")
                    break
                if arm in ("resolve", "rules") and req["question"]["kind"] == "confirm":
                    # Simulated customer: yes only to a slot they authorized (case file), otherwise no.
                    alias = case.alias_of(req["question"]["candidates"][0]["slot_id"])
                    ok = alias in {C.norm_key(k) for k in case.expected["authorized"][-1]}
                    record.setdefault("confirm_answers", []).append({"slot": alias, "yes": ok})
                    await say(CONFIRM_YES if ok else CONFIRM_NO)
                    continue
                if arm in ("resolve", "rules"):
                    await say(next(replies, GENERIC_REPLY))
                    continue
                ft = next(form_turns, {"unchanged": True})
                times.append(await db_now(oracle))
                effort["form_fields"] += form_fields(ft)
                if ft.get("decline") or ft.get("unchanged"):
                    await store.set_state(rid, "unresolved", detail="form user declined" if ft.get("decline")
                                          else "form user had nothing to change")
                else:
                    await store.approve(rid, form_intent(case, ft), {"turn": len(times) - 1, "by": "form"}, None)
    except (BudgetExceeded, ProviderFailed) as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        record["wall_s"] = round(time.perf_counter() - t0, 2)
        await api.close()
    record["verdict"] = await verdict(oracle, case, times)
    req = await store.get(rid)
    record["state"] = req and req["state"]
    record["questions"] = req["questions_asked"] if req else 0
    record["expected_questions"] = case.expected.get("questions")
    record["turns"] = [{"role": t["role"], "kind": t["kind"], "text": t["text"]}
                       for t in await store.turns(rid)] if req else []
    record["versions"] = [{"version": v["version"], "intent": v["intent"], "approved_by": v["approved_by"],
                           "diff": v["diff"]} for v in await store.versions(rid)] if req else []
    events = await store.pool.fetch("SELECT kind, data, at FROM resolve.events WHERE request_id = $1 ORDER BY id",
                                    rid)
    record["events"] = [{"kind": e["kind"], "data": json.loads(e["data"]), "at": e["at"].isoformat()}
                        for e in events]
    record["api_calls"] = api.log
    record["turn_times"] = [t.isoformat() for t in times]
    calls = model.log.calls[calls_before:] if model else []
    record["model_calls"] = calls
    record["model"] = {"calls": len(calls), "cost_usd": round(sum(c["cost_usd"] for c in calls), 6),
                       "latency_ms": sum(c["latency_ms"] for c in calls),
                       "input_tokens": sum(c["input_tokens"] for c in calls),
                       "output_tokens": sum(c["output_tokens"] for c in calls)}
    record["effort"] = effort
    record["slots"] = {k: v["id"] for k, v in case.slots.items()}
    return record


def summarize(records: list[dict]) -> dict:
    ok = {"correct", "correct_no_booking"}
    s = {"cases": len(records), "correct": sum(r["verdict"]["verdict"] in ok for r in records),
         "unauthorized": sum(bool(r["verdict"]["unauthorized"]) for r in records),
         "duplicate_effect": sum(r["verdict"]["verdict"] == "duplicate_effect" for r in records),
         "errors": sum("error" in r for r in records),
         "verdicts": {}, "model_calls": sum(r["model"]["calls"] for r in records),
         "cost_usd": round(sum(r["model"]["cost_usd"] for r in records), 6),
         "questions": sum(r["questions"] for r in records),
         "question_match": sum(1 for r in records if r["expected_questions"] is not None
                               and r["questions"] == r["expected_questions"]),
         "question_scored": sum(1 for r in records if r["expected_questions"] is not None),
         "customer_words": sum(r["effort"]["customer_words"] for r in records),
         "form_fields": sum(r["effort"]["form_fields"] for r in records),
         "median_wall_s": sorted(r["wall_s"] for r in records)[len(records) // 2] if records else None}
    for r in records:
        s["verdicts"][r["verdict"]["verdict"]] = s["verdicts"].get(r["verdict"]["verdict"], 0) + 1
    return s


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", help="dev | heldout | faults, or a path")
    ap.add_argument("--arm", default="resolve", choices=["resolve", "form", "oneshot", "rules"])
    ap.add_argument("--model", default="qwen3-14b-16k:latest")
    ap.add_argument("--cases", default="", help="comma-separated case ids (default: all)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-calls", type=int, default=None)
    ap.add_argument("--max-cost", type=float, default=None)
    ap.add_argument("--confirm", action="store_true",
                    help="product mode: every booking waits for the simulated customer's yes (resolve, rules arms)")
    a = ap.parse_args()
    path = Path(a.split) if a.split.endswith(".yaml") else C.CASE_DIR / f"{a.split}.yaml"
    cases = C.load(path)
    if a.cases:
        keep = set(a.cases.split(","))
        cases = [c for c in cases if c.id in keep]
    problems = [p for c in cases for p in C.check(c)]
    if problems:
        raise SystemExit("case file problems:\n" + "\n".join(problems))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    model = None if a.arm in ("form", "rules") else make_model(a.model, out / "raw", a.max_calls, a.max_cost)
    tag = out.name
    admin = await asyncpg.connect(settings.admin_dsn(settings.TRIAL_DB))
    oracle = await asyncpg.connect(settings.oracle_dsn())
    store = await Store(settings.admin_dsn(settings.TRIAL_DB)).open()
    records = []
    try:
        for case in cases:
            rec = await run_case(case, a.arm, model, store, admin, oracle, tag, a.confirm)
            (out / f"{case.id}.{a.arm}.json").write_text(json.dumps(rec, indent=1, default=str, ensure_ascii=False))
            records.append(rec)
            v = rec["verdict"]
            print(f"{case.id:4} {a.arm:8} {v['verdict']:20} slot={v.get('final_slot')} q={rec['questions']}"
                  f"/{rec['expected_questions']} calls={rec['model']['calls']} {rec['wall_s']}s"
                  + (f" ERROR {rec['error']}" if "error" in rec else ""), flush=True)
            if "error" in rec and "BudgetExceeded" in rec["error"]:
                break
    finally:
        await store.close()
        await admin.close()
        await oracle.close()
        if model is not None and hasattr(model.client, "close"):
            await model.client.close()
    summary = {"split": str(path), "arm": a.arm, "confirm": a.confirm,
               "model": None if a.arm in ("form", "rules") else a.model,
               "run": tag, "started_cases": [r["case"] for r in records], **summarize(records)}
    (out / f"summary.{a.arm}.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary))


if __name__ == "__main__":
    asyncio.run(main())
