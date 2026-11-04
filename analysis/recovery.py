"""Recovery definition (frozen in docs/protocol.md). Pure functions over per-second data.

The system is healthy at second t if over the trailing 10 s window (t-10, t]:
1. background success >= B_s - 0.01
2. background p95 latency <= max(1.5 * B_p95, B_p95 + 20 ms)
3. mean in-flight requests <= B_inf + 2
4. mean pool waiters <= B_wait + 1
Reference values come from the same trial's baseline window. Recovery time T_rec is the first k >= 0 such that
t_off + k ... t_off + k + 59 are all healthy, with the whole 60 s stretch inside the observation window.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

WINDOW_S = 10
HOLD_S = 60
FAST_S = 30


@dataclass
class Refs:
    b_p95: float  # ms
    b_s: float  # success fraction
    b_inf: float
    b_wait: float
    n_bg: int


def p95(values) -> float:
    a = np.asarray(list(values), dtype=float)
    if a.size == 0:
        return math.nan
    return float(np.percentile(a, 95, method="nearest"))


def reference_values(bg: pd.DataFrame, samples: pd.DataFrame, baseline: tuple[float, float]) -> Refs:
    """bg: columns t (arrival, s), latency_ms, ok. samples: columns t, in_flight, pool_waiters."""
    b = bg[(bg.t >= baseline[0]) & (bg.t < baseline[1])]
    s = samples[(samples.t >= baseline[0]) & (samples.t < baseline[1])]
    return Refs(b_p95=p95(b.latency_ms), b_s=float(b.ok.mean()) if len(b) else math.nan,
                b_inf=p95(s.in_flight), b_wait=p95(s.pool_waiters), n_bg=len(b))


def healthy_series(bg: pd.DataFrame, samples: pd.DataFrame, refs: Refs, t_end: int) -> pd.DataFrame:
    """One row per whole second t in [WINDOW_S, t_end]: trailing-window statistics and the four conditions."""
    p95_limit = max(1.5 * refs.b_p95, refs.b_p95 + 20.0)
    bg_sec = np.floor(bg.t.to_numpy()).astype(int)
    s_sec = np.floor(samples.t.to_numpy()).astype(int)
    lat, ok = bg.latency_ms.to_numpy(), bg.ok.to_numpy()
    inf, wait = samples.in_flight.to_numpy(), samples.pool_waiters.to_numpy()
    rows = []
    for t in range(WINDOW_S, t_end + 1):
        m = (bg_sec > t - WINDOW_S - 1) & (bg_sec <= t - 1)  # requests arriving in (t-10, t]
        ms = (s_sec > t - WINDOW_S - 1) & (s_sec <= t - 1)
        n = int(m.sum())
        succ = float(ok[m].mean()) if n else math.nan
        lat95 = p95(lat[m]) if n else math.nan
        inf_mean = float(inf[ms].mean()) if ms.any() else math.nan
        wait_mean = float(wait[ms].mean()) if ms.any() else math.nan
        c1 = n > 0 and succ >= refs.b_s - 0.01
        c2 = n > 0 and lat95 <= p95_limit
        c3 = not math.isnan(inf_mean) and inf_mean <= refs.b_inf + 2
        c4 = not math.isnan(wait_mean) and wait_mean <= refs.b_wait + 1
        rows.append({"t": t, "n_bg": n, "bg_success": succ, "bg_p95_ms": lat95, "in_flight": inf_mean,
                     "pool_waiters": wait_mean, "c_success": c1, "c_p95": c2, "c_inflight": c3, "c_waiters": c4,
                     "healthy": c1 and c2 and c3 and c4})
    return pd.DataFrame(rows)


def recovery_time(healthy: dict[int, bool], t_off: float, observe_s: float = 360,
                  hold_s: int = HOLD_S) -> float | None:
    """Seconds from t_off to the first whole second t >= t_off that starts hold_s consecutive healthy seconds, with
    the whole stretch inside the observation window [t_off, t_off + observe_s]; None if there is none."""
    start = math.ceil(t_off)
    end = t_off + observe_s
    t = start
    while t + hold_s - 1 <= end:
        if all(healthy.get(t + j, False) for j in range(hold_s)):
            return round(t - t_off, 1)
        t += 1
    return None


def verdict(t_rec: float | None, observe_s: float = 360) -> str:
    if t_rec is None:
        return f"not_recovered_within_{int(observe_s)}s"
    return "fast" if t_rec <= FAST_S else "slow"


def refs_dict(refs: Refs) -> dict:
    return {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in asdict(refs).items()}
