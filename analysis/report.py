"""Static report.html per trial and per experiment (jinja2 template, matplotlib figures inlined as PNG)."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from jinja2 import Template  # noqa: E402

# Reference palette, light steps (dataviz skill): categorical slots 1-3, recessive ink, fault band neutral.
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, GRID, BAND = "#0b0b0b", "#52514e", "#e4e3df", "#d9d8d3"

plt.rcParams.update({
    "font.size": 9, "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.spines.top": False,
    "axes.spines.right": False, "figure.facecolor": "#fcfcfb", "axes.facecolor": "#fcfcfb",
    "axes.titlesize": 10, "axes.titleweight": "bold", "axes.titlecolor": INK, "legend.frameon": False,
})


def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _fault_band(ax, result):
    f = result["fault"]
    if f.get("enabled", True) and f["t_on"] is not None:
        ax.axvspan(f["t_on"], f["t_off"], color=BAND, alpha=0.6, lw=0, label="fault")
    t_rec = result["recovery"]["t_rec_s"]
    if t_rec is not None:
        x = f["t_off"] + t_rec
        ax.axvline(x, color=INK2, lw=1, ls="--")
        ax.annotate(f"recovered (T_rec {t_rec:.0f} s)", (x, 1), xycoords=("data", "axes fraction"),
                    xytext=(4, -10), textcoords="offset points", color=INK2, fontsize=8)


def trial_figures(trial_dir: Path, result: dict) -> dict[str, str]:
    figs = {}
    per_sec = pd.read_csv(trial_dir / "per_second.csv") if (trial_dir / "per_second.csv").stat().st_size > 1 else None
    refs = result["recovery"]["refs"]
    if per_sec is not None and len(per_sec):
        fig, ax = plt.subplots(figsize=(8, 2.6))
        ax.plot(per_sec.t, per_sec.bg_p95_ms, color=BLUE, lw=2)
        if refs.get("b_p95"):
            limit = max(1.5 * refs["b_p95"], refs["b_p95"] + 20)
            ax.axhline(limit, color=INK2, lw=1, ls=":")
            ax.annotate("healthy limit", (per_sec.t.max(), limit), xytext=(-60, 4), textcoords="offset points",
                        color=INK2, fontsize=8)
        _fault_band(ax, result)
        ax.set_yscale("log")
        ax.set_title("Background p95 latency, trailing 10 s (backend side, ms, log scale)")
        ax.set_xlabel("seconds from trial start")
        figs["p95"] = _png(fig)

        fig, ax = plt.subplots(figsize=(8, 2.6))
        ax.plot(per_sec.t, per_sec.in_flight, color=BLUE, lw=2, label="in-flight requests")
        ax.plot(per_sec.t, per_sec.pool_waiters, color=ORANGE, lw=2, label="pool waiters")
        _fault_band(ax, result)
        ax.legend(loc="upper right")
        ax.set_title("Backend resources, trailing 10 s mean")
        ax.set_xlabel("seconds from trial start")
        figs["resources"] = _png(fig)

    goals_path = trial_dir / "goals.csv"
    if goals_path.exists() and goals_path.stat().st_size > 1:
        goals = pd.read_csv(goals_path)
        att = pd.read_csv(trial_dir / "retry_classes.csv") if (trial_dir / "retry_classes.csv").exists() else None
        if att is not None and len(att):
            fig, ax = plt.subplots(figsize=(8, 2.6))
            att["sec10"] = (att.t // 10) * 10
            base = att[att.label == "base"].groupby("sec10").size()
            react = att[att.label != "base"].groupby("sec10").size()
            idx = range(0, int(att.sec10.max()) + 10, 10)
            ax.plot(list(idx), [base.get(i, 0) / 10 for i in idx], color=BLUE, lw=2, label="first-time operations")
            ax.plot(list(idx), [react.get(i, 0) / 10 for i in idx], color=ORANGE, lw=2,
                    label="reactions (retries, switches, restarts, other)")
            _fault_band(ax, result)
            ax.legend(loc="upper right")
            ax.set_title("Goal operations per second (10 s bins)")
            ax.set_xlabel("seconds from trial start")
            figs["ops"] = _png(fig)
        feas = goals[goals.expected == "reschedule"]
        if len(feas):
            fig, ax = plt.subplots(figsize=(4, 2.6))
            lat = feas[feas.verdict == "correct"].latency_s.dropna().sort_values()
            ys = [(i + 1) / len(feas) for i in range(len(lat))]
            ax.step(lat, ys, where="post", color=BLUE, lw=2)
            ax.set_ylim(0, 1)
            basis = result["goals"].get("completion_basis", "change_insert")
            ax.set_xlabel("seconds from goal arrival to " + ("commit acknowledgment" if basis == "commit_ack"
                                                             else "change INSERT (precedes commit)"))
            ax.set_title("Completion curve F(tau), feasible goals")
            figs["completion"] = _png(fig)
    return figs


TRIAL_TEMPLATE = Template("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rebound trial {{ r.trial_id }}</title>
<style>
:root { --bg:#f6f6f4; --card:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --line:#e4e3df; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#121211; --card:#1a1a19;
  --ink:#f0efec; --ink2:#c3c2b7; --line:#2e2d2b; } }
:root[data-theme="dark"] { --bg:#121211; --card:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --line:#2e2d2b; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.5 system-ui, sans-serif; }
main { max-width:960px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:16px; margin:28px 0 8px; }
.sub { color:var(--ink2); margin:0 0 16px; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:8px; }
.tile { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
.tile b { display:block; font-size:20px; } .tile span { color:var(--ink2); font-size:12px; }
table { border-collapse:collapse; width:100%; background:var(--card); font-size:13px; }
th, td { border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; } th { color:var(--ink2); }
.fig { background:#fcfcfb; border:1px solid var(--line); border-radius:8px; padding:8px; margin:8px 0; }
.fig img { max-width:100%; height:auto; display:block; }
.wrap { overflow-x:auto; } .bad { color:#c0392b; } code { font-size:12px; }
</style></head><body><main>
<h1>Trial {{ r.trial_id }} · {{ r.condition }} · gate {{ r.gate }}</h1>
<p class="sub">{{ r.experiment_id }} · status <b>{{ r.status }}</b> · config <code>{{ r.config_hash[:19] }}</code>
{% if r.label %}· {{ r.label }}{% endif %}</p>
<div class="tiles">
 <div class="tile"><b>{{ r.recovery.verdict }}</b><span>recovery verdict</span></div>
 <div class="tile"><b>{{ fmt(r.recovery.t_rec_s, "{:.0f} s") }}</b><span>T_rec from toxic removal</span></div>
 <div class="tile"><b>{{ fmt(r.goals.success_S, "{:.1%}") }}</b><span>goal success S ({{ r.goals.scheduled }} scheduled)</span></div>
 <div class="tile"><b>{{ fmt(r.background.lost_service_D, "{:.0f}") }}</b><span>lost background service D</span></div>
 <div class="tile"><b>{{ fmt(r.model.get("cost_usd"), "${:.2f}") }}</b><span>model cost (estimated)</span></div>
</div>
{% if r.invalid_reasons or r.inconclusive_reasons %}<h2>Validity</h2><ul>
{% for x in r.invalid_reasons %}<li class="bad">invalid: {{ x }}</li>{% endfor %}
{% for x in r.inconclusive_reasons %}<li class="bad">inconclusive: {{ x }}</li>{% endfor %}</ul>{% endif %}
<h2>Recovery (backend side)</h2>
{% for k in ["p95", "resources", "ops"] %}{% if figs.get(k) %}<div class="fig"><img alt="{{ k }} chart" src="{{ figs[k] }}"></div>{% endif %}{% endfor %}
<p class="sub">Baseline references: B_p95 {{ fmt(r.recovery.refs.b_p95, "{:.1f} ms") }}, B_s {{ fmt(r.recovery.refs.b_s, "{:.4f}") }},
B_inf {{ r.recovery.refs.b_inf }}, B_wait {{ r.recovery.refs.b_wait }}. Unhealthy seconds in the 360 s window:
{{ r.recovery.unhealthy_seconds_in_window }}. Fault {{ r.fault.latency_ms }} ms for {{ r.fault.duration_s }} s,
canary rise {{ r.fault.canary_rise_ms }} ms.</p>
<h2>Goal outcomes (oracle)</h2>
<div class="wrap"><table><tr>{% for k in r.goals.verdicts %}<th>{{ k }}</th>{% endfor %}<th>F(60 s)</th><th>F(180 s)</th></tr>
<tr>{% for v in r.goals.verdicts.values() %}<td>{{ v }}</td>{% endfor %}<td>{{ fmt(r.goals.F_60s, "{:.1%}") }}</td><td>{{ fmt(r.goals.F_180s, "{:.1%}") }}</td></tr></table></div>
{% if figs.get("completion") %}<div class="fig" style="max-width:480px"><img alt="completion curve" src="{{ figs.completion }}"></div>{% endif %}
<h2>Cohorts by arrival time</h2>
<div class="wrap"><table><tr><th>cohort</th><th>goals</th><th>success</th><th>HTTP attempts / goal</th><th>DB ms / goal</th>
<th>same-call retries</th><th>switch to broad search</th><th>restarts</th><th>other</th></tr>
{% for c in r.cohorts %}<tr><td>{{ c.cohort }}</td><td>{{ c.goals }}</td><td>{{ fmt(c.success, "{:.1%}") }}</td>
<td>{{ fmt(c.R_attempts_per_goal, "{:.2f}") }}</td><td>{{ fmt(c.W_db_ms_per_goal, "{:.1f}") }}</td><td>{{ c.n_same_call_retry }}</td>
<td>{{ c.n_switch_to_broad_search }}</td><td>{{ c.n_restart }}</td><td>{{ c.n_other }}</td></tr>{% endfor %}</table></div>
{% if r.backpressure %}<h2>Back-pressure (Retry-After) compliance</h2>
<p class="sub">{{ r.backpressure.violations }} of {{ r.backpressure.followed_by_attempt }} refusals with Retry-After were
followed by the goal's next request too early ({{ r.backpressure.goals_with_violation }} goals; median gap
{{ fmt(r.backpressure.gap_s_p50, "{:.2f} s") }}; busiest goal {{ r.backpressure.max_attempts_one_goal_10s }} requests in
10 s).</p>
{% if r.backpressure.witnesses %}<div class="wrap"><table><tr><th>goal</th><th>refused attempt</th><th>t</th><th>Retry-After</th>
<th>next attempt</th><th>next tool</th><th>chosen by</th><th>gap</th></tr>
{% for w in r.backpressure.witnesses[:12] %}<tr><td>{{ w.goal_id }}</td><td><code>{{ w.refused_attempt_id }}</code></td>
<td>{{ w.t_refused }}</td><td>{{ w.retry_after_s }} s</td><td><code>{{ w.next_attempt_id }}</code></td><td>{{ w.next_tool }}</td>
<td><code>{{ w.next_model_call_id or "policy" }}</code></td><td>{{ w.gap_s }} s</td></tr>{% endfor %}</table></div>{% endif %}{% endif %}
{% if r.exposure %}<h2>Who occupied the API (in-flight seconds share)</h2>
<div class="wrap"><table><tr><th>window</th><th>goal share of in-flight time</th><th>goal share of DB round-trip ms</th>
<th>goal requests started</th><th>goal requests rejected</th><th>background requests rejected</th></tr>
{% for k, w in r.exposure.items() %}<tr><td>{{ k }} {{ w.window_s }}</td><td>{{ fmt(w.goal_share_inflight, "{:.2%}") }}</td>
<td>{{ fmt(w.goal_share_db, "{:.2%}") }}</td><td>{{ w.goal_requests_started }}</td><td>{{ w.goal_requests_rejected }}</td>
<td>{{ w.background_requests_rejected }}</td></tr>{% endfor %}</table></div>{% endif %}
<h2>Details</h2>
<div class="wrap"><table>
<tr><th>Useful-service check</th><td><code>{{ js(r.useful_service) }}</code></td></tr>
<tr><th>Gate</th><td><code>{{ js(r.gate_stats) }}</code></td></tr>
<tr><th>DB round-trip time (incl. proxy latency, not DB CPU)</th><td><code>{{ js(r.db_time) }}</code></td></tr>
<tr><th>Provenance</th><td><code>{{ js({"config_hash": r.config_hash, "compat_fingerprint": r.get("compat_fingerprint"), "analysis_fingerprint": r.get("analysis_fingerprint"), "metrics_version": r.get("metrics_version")}) }}</code></td></tr>
<tr><th>Model</th><td><code>{{ js(r.model) }}</code></td></tr>
<tr><th>Retries</th><td><code>{{ js(r.retries) }}</code></td></tr>
<tr><th>Runner health</th><td><code>{{ js(r.runner) }}</code></td></tr>
<tr><th>Clock check</th><td><code>{{ js(r.clock_check) }}</code></td></tr>
</table></div>
</main></body></html>
""")


def fmt(v, spec: str) -> str:
    if v is None:
        return "–"
    try:
        return spec.format(v)
    except (ValueError, TypeError):
        return str(v)


def js(v) -> str:
    return json.dumps(v, default=str)


def trial_report(trial_dir: str | Path, result: dict, out_dir: str | Path | None = None) -> Path:
    """Render from the derived CSVs in `out_dir` (default: the bundle, as during a trial)."""
    d = Path(out_dir) if out_dir is not None else Path(trial_dir)
    figs = trial_figures(d, result)
    out = d / "report.html"
    out.write_text(TRIAL_TEMPLATE.render(r=result, figs=figs, fmt=fmt, js=js))
    return out
