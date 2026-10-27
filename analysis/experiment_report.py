"""Experiment report: `python -m analysis.experiment_report runs/<experiment>` -> summary.json + report.html."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
from jinja2 import Template

from analysis.experiment import summarize
from analysis.report import AQUA, BLUE, INK2, ORANGE, _png, fmt, js

CELL_ORDER = [("agent", "off"), ("agent", "on"), ("scripted", "off"), ("scripted", "on"), ("replay", "off")]
COLORS = {"agent": BLUE, "scripted": ORANGE, "replay": AQUA}


def dot_figure(df: pd.DataFrame, col: str, title: str, cap: float | None = None, log: bool = False) -> str | None:
    d = df[df.status == "valid"]
    if not len(d):
        return None
    fig, ax = plt.subplots(figsize=(7, 2.6))
    labels = []
    for i, (cond, gate) in enumerate(CELL_ORDER):
        g = d[(d.condition == cond) & (d.gate == gate)]
        labels.append(f"{cond}\ngate {gate}")
        vals = [cap if (v is None or (isinstance(v, float) and math.isnan(v))) and cap else v for v in g[col]]
        vals = [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
        ax.scatter([i] * len(vals), vals, s=48, color=COLORS[cond], edgecolor="#fcfcfb", linewidth=1.5, zorder=3)
    ax.set_xticks(range(len(CELL_ORDER)), labels)
    ax.set_xlim(-0.5, len(CELL_ORDER) - 0.5)
    if cap:
        ax.axhline(cap, color=INK2, lw=1, ls=":")
        ax.annotate("not recovered within window", (len(CELL_ORDER) - 0.5, cap), xytext=(-150, 4),
                    textcoords="offset points", color=INK2, fontsize=8)
    if log:
        ax.set_yscale("symlog")
    ax.set_title(title)
    return _png(fig)


TEMPLATE = Template("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rebound results</title>
<style>
:root { --bg:#f6f6f4; --card:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --line:#e4e3df; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#121211; --card:#1a1a19;
  --ink:#f0efec; --ink2:#c3c2b7; --line:#2e2d2b; } }
:root[data-theme="dark"] { --bg:#121211; --card:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --line:#2e2d2b; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.5 system-ui, sans-serif; }
main { max-width:1040px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:16px; margin:28px 0 8px; } .sub { color:var(--ink2); }
table { border-collapse:collapse; width:100%; background:var(--card); font-size:12.5px; }
th, td { border-bottom:1px solid var(--line); padding:5px 7px; text-align:left; white-space:nowrap; }
th { color:var(--ink2); } .wrap { overflow-x:auto; }
.fig { background:#fcfcfb; border:1px solid var(--line); border-radius:8px; padding:8px; margin:8px 0; }
.fig img { max-width:100%; height:auto; display:block; } code { font-size:12px; white-space:pre-wrap; }
</style></head><body><main>
<h1>Rebound · {{ name }}</h1>
<p class="sub">{{ n_trials }} trials ({{ n_valid }} valid). Every trial is listed, including invalid ones with reasons.</p>
<h2>Recovery time by cell</h2>
{% for f in figs %}<div class="fig"><img alt="chart" src="{{ f }}"></div>{% endfor %}
<h2>Cells (valid trials)</h2>
<div class="wrap"><table><tr><th>condition</th><th>gate</th><th>fault s</th><th>n</th><th>T_rec (s)</th><th>verdicts</th>
<th>S mean</th><th>correct mean</th><th>D mean</th><th>R fault cohort</th><th>incorrect</th><th>duplicate</th><th>model $</th></tr>
{% for c in s.cells %}<tr><td>{{ c.condition }}</td><td>{{ c.gate }}</td><td>{{ c.fault_s if c.fault_on else "none" }}</td><td>{{ c.n }}</td>
<td>{{ c.t_rec_s }}</td><td>{{ c.recovery }}</td><td>{{ fmt(c.S_mean, "{:.3f}") }}</td><td>{{ fmt(c.correct_mean, "{:.1f}") }}</td>
<td>{{ fmt(c.D_mean, "{:.0f}") }}</td><td>{{ fmt(c.R_fault_mean, "{:.2f}") }}</td><td>{{ c.incorrect }}</td><td>{{ c.duplicate }}</td>
<td>{{ fmt(c.cost_usd, "{:.2f}") }}</td></tr>{% endfor %}</table></div>
<h2>Amplification vs replay (HTTP attempts per goal, gate off, 10 s fault)</h2>
<p><code>{{ js(s.amplification_vs_replay) }}</code></p>
<h2>Gate acceptance criteria (agent cells)</h2>
<p><code>{{ acc }}</code></p>
<h2>All trials</h2>
<div class="wrap"><table><tr>{% for k in cols %}<th>{{ k }}</th>{% endfor %}</tr>
{% for t in s.trials %}<tr>{% for k in cols %}<td>{{ cell(t[k]) }}</td>{% endfor %}</tr>{% endfor %}</table></div>
</main></body></html>
""")

COLS = ["trial_id", "label", "block", "condition", "gate", "fault_s", "status", "t_rec_s", "recovery", "scheduled",
        "correct", "incorrect", "duplicate", "failed", "unfinished", "rejected", "S", "baseline_S", "D",
        "R_baseline", "R_fault", "R_post", "reactions_fault", "bg_p95_baseline", "cost_usd", "invalid_reasons"]


def cell(v):
    if isinstance(v, float):
        return "–" if math.isnan(v) else f"{v:.3g}"
    return "–" if v is None else v


def build(exp_dir: str | Path) -> Path:
    exp_dir = Path(exp_dir)
    s = summarize(exp_dir)
    (exp_dir / "summary.json").write_text(json.dumps(s, indent=2, default=str) + "\n")
    df = pd.DataFrame(s["trials"])
    figs = []
    if len(df):
        std = df[(df.fault_s == 10) & (df.fault_on == True)]  # noqa: E712
        f = dot_figure(std, "t_rec_s", "T_rec after the 10 s fault (s; dotted line = not recovered)", cap=360)
        if f:
            figs.append(f)
        f = dot_figure(std, "D", "Lost background service D (requests)")
        if f:
            figs.append(f)
        f = dot_figure(std, "R_fault", "HTTP attempts per goal, fault cohort")
        if f:
            figs.append(f)
    out = exp_dir / "report.html"
    out.write_text(TEMPLATE.render(name=exp_dir.name, s=s, figs=figs, n_trials=len(df),
                                   n_valid=int((df.status == "valid").sum()) if len(df) else 0,
                                   cols=COLS, cell=cell, fmt=fmt, js=js,
                                   acc=json.dumps(s["acceptance"], indent=1, default=str)))
    return out


if __name__ == "__main__":
    print(build(sys.argv[1]))
