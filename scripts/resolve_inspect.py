"""Print what happened in Resolve case records: intents per version, validator problems, decisions, questions.

    uv run python scripts/resolve_inspect.py runs/resolve/dev-r1 [D1 D3 ...]
"""

import json
import sys
from pathlib import Path


def rules(rs):
    out = []
    for r in rs:
        val = {k: r[k] for k in ("date_from", "date_to", "dates", "weekdays", "time", "technician_ids", "only_on_dates")
               if k in r}
        out.append(f"{r.get('id', '?')} {r['kind']}[{r['strength']}] {json.dumps(val)} <{r['quote']}>"
                   + (f" ref={r['reference']}" if r.get("reference") else ""))
    return out


def show_intent(i, pad="    "):
    for line in rules(i.get("rules", [])):
        print(pad + line)
    for n, a in enumerate(i.get("alternatives", []), 1):
        print(f"{pad}alt {n} {a['label']!r}")
        for line in rules(a["rules"]):
            print(pad + "  " + line)
    for ref in i.get("references", []):
        print(f"{pad}ref {ref}")
    for u in i.get("unclear", []):
        print(f"{pad}unclear {u['phrase']!r}: " + " | ".join(r["label"] for r in u["readings"]))
    if i.get("approved_slots"):
        print(f"{pad}approved_slots {i['approved_slots']}")


def main():
    run = Path(sys.argv[1])
    only = set(sys.argv[2:])
    for f in sorted(run.glob("*.json")):
        if f.name.startswith("summary"):
            continue
        r = json.loads(f.read_text())
        if only and r["case"] not in only:
            continue
        names = {v: k for k, v in r["slots"].items()}
        print(f"== {r['case']} {r['arm']} -> {r['verdict']['verdict']} final={r['verdict'].get('final_slot')} "
              f"state={r['state']}")
        for t in r["turns"]:
            print(f"  [{t['role']}/{t['kind']}] {t['text']}")
        for e in r["events"]:
            d = e["data"]
            if e["kind"] in ("interpret", "revise"):
                print(f"  {e['kind']}: attempts={d['attempts']} decision={d.get('decision')} problems={d['problems']}")
            elif e["kind"] == "worker.decided":
                cands = [names.get(c, c) for c in d["candidates"]]
                print(f"  decided v{d['version']}: {d['decision']} slot={names.get(d['slot_id'], d['slot_id'])} "
                      f"cands={cands} ({d['reason']})")
        for v in r["versions"]:
            print(f"  -- version {v['version']} approved_by={v['approved_by'].get('by')} diff={v['diff']}")
            show_intent(v["intent"])
        for c in r["model_calls"]:
            if c["status"] != "ok":
                print(f"  call {c['purpose']}: {c['status']} {c.get('text', '')[:300]}")
        if "error" in r:
            print("  ERROR", r["error"])


if __name__ == "__main__":
    main()
