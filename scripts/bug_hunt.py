"""Bug hunter: a model invents hard customer requests; SQL and the oracle decide what actually broke.

Usage: python scripts/bug_hunt.py <out_dir> --n 12 --provider ollama|meta [--model NAME] [--arm hardened] [--cap-usd X]

For each base task the generator model writes one tricky message plus the structured constraint it means. SQL then
decides feasibility (never the model), the agent under test attempts the message on a fresh database, and the oracle
gives the verdict. A failure is a CANDIDATE: the generator may have written a message that does not match its own
constraint, so every candidate is listed with both for a person to review.
"""

import argparse
import asyncio
import copy
import datetime as dt
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace as NS

import asyncpg

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")
from agent_v2_run import run_arm  # noqa: E402

from harness import settings  # noqa: E402
from harness.agent import _visible_text  # noqa: E402
from harness.ollama import OllamaClient  # noqa: E402
from harness.provider import cost_usd, make_client  # noqa: E402
from oracle.verdicts import CANDIDATES, meets_constraint  # noqa: E402

BRIEF = """You write realistic, tricky messages from customers of a field-service company, to test a scheduling
assistant. Write as the customer: casual, indirect, or with a detail that is easy to miss. Say times of day in words.

Appointment: {service} on {current}. The customer wants it moved to between {d1} and {d2}.
Technicians available in that window include: {techs}.

Use exactly one kind of requirement:
- start_hour: the slot must start from from_h:00 up to but not including to_h:00 (whole hours, 0-23)
- technician: only one technician id from the list above is acceptable

Reply with only one JSON object and nothing else:
{{"message": "<the customer's message; include both dates>",
  "constraint": {{"kind": "start_hour", "from_h": 9, "to_h": 11}} or {{"kind": "technician", "technician_id": 123}},
  "why_hard": "<one short sentence>"}}"""


def parse_scenario(text: str) -> dict | None:
    """The generator's JSON, or None if malformed or not one of the allowed constraint shapes."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    c = d.get("constraint")
    if not isinstance(d.get("message"), str) or not d["message"].strip() or not isinstance(c, dict):
        return None
    if c.get("kind") == "start_hour":
        ok = isinstance(c.get("from_h"), int) and isinstance(c.get("to_h"), int) and 0 <= c["from_h"] < c["to_h"] <= 24
    elif c.get("kind") == "technician":
        ok = isinstance(c.get("technician_id"), int)
    else:
        ok = False
    if not ok:
        return None
    return {"message": d["message"].strip(), "constraint": c, "why_hard": str(d.get("why_hard", ""))[:200]}


async def generate(client, args, n: int) -> tuple[list[dict], float]:
    base = [json.loads(x) for x in Path("data/tasks/tasks_eval_B.jsonl").read_text().splitlines()]
    used = {json.loads(x)["base_task"] for x in Path("data/tasks/tasks_messy_v1.jsonl").read_text().splitlines()}
    base = [t for t in base if t["expected"] == "reschedule" and t["task_id"] not in used]
    random.Random(args.seed).shuffle(base)
    conn = await asyncpg.connect(settings.oracle_dsn("fielddesk_template"))
    out, spent, rejected = [], 0.0, 0
    for t in base:
        if len(out) >= n:
            break
        if args.cap_usd is not None and spent >= args.cap_usd * 0.2:
            break
        rows = await conn.fetch(CANDIDATES, t["customer_region"], t["service"], dt.date.fromisoformat(t["d1"]),
                                dt.date.fromisoformat(t["d2"]))
        if not rows:
            continue
        techs = sorted({r["technician_id"] for r in rows})[:12]
        brief = BRIEF.format(service=t["service"].replace("_", " "), current=t["current_date"], d1=t["d1"], d2=t["d2"],
                             techs=techs)
        r = await client.responses.create(model=args.model, instructions=brief, tools=[], max_output_tokens=2048,
                                          input=[{"role": "user", "content": "Write one message."}],
                                          reasoning={"effort": "low"}, store=False)
        u = r.usage
        spent += cost_usd(args.model, u.input_tokens, 0, u.output_tokens)
        s = parse_scenario(_visible_text(r))
        if s is None:
            rejected += 1
            continue
        feasible = sum(meets_constraint({"constraint": s["constraint"]}, x["id"], x["starts_at"], x["technician_id"])
                       for x in rows)
        task = copy.deepcopy(t)
        task.update(constraint=s["constraint"], goal_text=s["message"], why_hard=s["why_hard"], feasible=feasible,
                    expected="reschedule" if feasible else "defer", base_task=t["task_id"], messy_kind="hunted",
                    split="hunted", task_id=f"H{len(out) + 1:02d}")
        out.append(task)
    await conn.close()
    print(f"generated {len(out)} scenarios, rejected {rejected} malformed, generator spend ${spent:.3f}", flush=True)
    return out, spent


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--provider", default="ollama", choices=["ollama", "meta"])
    ap.add_argument("--model", default="gemma4-12b-16k:latest")
    ap.add_argument("--arm", default="hardened")
    ap.add_argument("--seed", type=int, default=20261102)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--cap-usd", type=float, default=None)
    args = ap.parse_args()
    if args.provider == "meta" and args.cap_usd is None:
        raise SystemExit("--cap-usd is required with --provider meta")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    client = OllamaClient({"temperature": 0.7, "seed": args.seed, "num_ctx": 8192}, think=False) \
        if args.provider == "ollama" else make_client()
    scenarios, spent = await generate(client, args, args.n)
    if hasattr(client, "close"):
        await client.close()
    (out / "scenarios.jsonl").write_text("".join(json.dumps(s, sort_keys=True) + "\n" for s in scenarios))
    run_args = NS(provider=args.provider, model=args.model, concurrency=args.concurrency, cap_usd=args.cap_usd,
                  spent=spent)
    rows = await run_arm(args.arm, scenarios, run_args, out)
    by_id = {s["task_id"]: s for s in scenarios}
    candidates = [{**r, "message": by_id[r["task_id"]]["goal_text"], "why_hard": by_id[r["task_id"]]["why_hard"],
                   "expected": by_id[r["task_id"]]["expected"], "feasible": by_id[r["task_id"]]["feasible"]}
                  for r in rows if r["verdict"] != "correct"]
    (out / "findings.json").write_text(json.dumps({"verdicts": Counter(r["verdict"] for r in rows),
                                                   "candidates": candidates, "spent_usd": round(run_args.spent, 3)},
                                                  indent=2, default=str))
    print(dict(Counter(r["verdict"] for r in rows)), f"candidates {len(candidates)}, spent ${run_args.spent:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
