import numpy as np
import pandas as pd

from analysis.recovery import Refs, healthy_series, recovery_time, reference_values, verdict


def healthy_map(bad: set[int], end=600) -> dict[int, bool]:
    return {t: t not in bad for t in range(0, end)}


def test_recovery_time_counts_from_t_off_and_needs_60_s_hold():
    # Unhealthy through 199, healthy from 200 on; t_off = 190.
    h = healthy_map(set(range(180, 200)))
    assert recovery_time(h, 190.0) == 10


def test_short_healthy_stretch_does_not_count():
    bad = set(range(180, 200)) | {230}  # healthy 200-229 (30 s), unhealthy at 230, healthy after
    assert recovery_time(healthy_map(bad), 190.0) == 41


def test_fractional_t_off_measures_from_t_off():
    h = healthy_map(set(range(180, 195)))
    assert recovery_time(h, 190.4) == 4.6  # first whole healthy second 195


def test_not_recovered_when_no_stretch_fits_in_window():
    h = healthy_map(set(range(180, 520)))  # healthy from 520; 520..579 ends after 190+360=550
    assert recovery_time(h, 190.0, observe_s=360) is None
    assert verdict(None) == "not_recovered_within_360s"


def test_stretch_ending_exactly_at_window_end_counts():
    h = healthy_map(set(range(180, 491)))  # healthy 491..550 inclusive = 60 s
    assert recovery_time(h, 190.0, observe_s=360) == 301


def test_verdict_classes():
    assert verdict(30) == "fast" and verdict(30.5) == "slow"


def frames(fault=(180, 190)):
    t = np.arange(0, 300, 0.01)
    lat = np.where((t >= fault[0]) & (t < fault[1]), 1500.0, 5.0)
    bg = pd.DataFrame({"t": t, "latency_ms": lat, "ok": lat < 2000})
    st = np.arange(0, 300, 1.0)
    inf = np.where((st >= fault[0]) & (st < fault[1]), 50, 0)
    samples = pd.DataFrame({"t": st, "in_flight": inf, "pool_waiters": inf})
    return bg, samples


def test_reference_values_and_healthy_series_end_to_end():
    bg, samples = frames()
    refs = reference_values(bg, samples, (60, 180))
    assert refs.b_p95 == 5.0 and refs.b_s == 1.0 and refs.b_inf == 0 and refs.b_wait == 0
    series = healthy_series(bg, samples, refs, 299)
    h = dict(zip(series.t, series.healthy, strict=True))
    assert h[179] and not h[181] and not h[199]  # trailing 10 s window still holds fault seconds at 199
    assert h[200]
    assert recovery_time(h, 190.0, observe_s=100) == 10


def test_p95_condition_has_20ms_floor():
    refs = Refs(b_p95=5.0, b_s=1.0, b_inf=0, b_wait=0, n_bg=100)
    t = np.arange(0, 50, 0.01)
    bg = pd.DataFrame({"t": t, "latency_ms": np.full(t.size, 24.0), "ok": True})  # 24 ms <= 5 + 20 ms
    samples = pd.DataFrame({"t": np.arange(0, 50, 1.0), "in_flight": 0, "pool_waiters": 0})
    s = healthy_series(bg, samples, refs, 40)
    assert s.c_p95.all()
