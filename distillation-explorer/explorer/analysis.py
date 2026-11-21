"""Turning raw datalog rows into per-fraction numbers and chart series.

Design choices worth knowing about:

* **Clean first, aggregate second.** `clean()` blanks every value outside the
  signal's plausibility range (and exact 0.0 dropouts) before anything else
  runs. Otherwise every minimum is 0.

* **Time-weighted, not sample-counted.** Live rows are 20 s apart and
  backfilled rows 10 s apart, so a plain mean over-weights backfilled
  stretches. Every statistic here weights a sample by how long it was the
  latest reading (the gap to the next sample), capped at `max_gap_s` so a hole
  in the data does not let one sample speak for an hour.

* **Intervals are half-open.** A fraction owns `start <= t < end`, so a scan on
  a boundary between back-to-back fractions lands in exactly one of them.

* Times inside this module are float seconds since the fraction start; the
  web layer converts back to timestamps.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import Column, Signal


# --------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------
def plausible_mask(vals: np.ndarray, sig: Signal) -> np.ndarray:
    ok = np.isfinite(vals) & (vals >= sig.lo) & (vals <= sig.hi)
    if sig.drop_zero:
        ok &= vals != 0.0
    return ok


def clean(df: pd.DataFrame, column: Column) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (plausible rows, removed rows).

    The removed rows are kept (time and tag only) so each fraction can report
    how many dropouts were taken out of it. Tags not in the column's config
    are ignored: we only analyse what the config describes.
    """
    if df.empty:
        return df.copy(), df[["ts", "tag"]].copy() if "ts" in df else pd.DataFrame(columns=["ts", "tag"])
    keep = np.zeros(len(df), dtype=bool)
    known = np.zeros(len(df), dtype=bool)
    tags = df["tag"].to_numpy()
    vals = df["val"].to_numpy(dtype=float)
    for sig in column.signals:
        sel = tags == sig.tag
        known |= sel
        ok = plausible_mask(vals[sel], sig)
        keep[np.flatnonzero(sel)[ok]] = True
    removed = df.loc[known & ~keep, ["ts", "tag"]].reset_index(drop=True)
    return df.loc[keep].reset_index(drop=True), removed


# --------------------------------------------------------------------------
# Time weighting
# --------------------------------------------------------------------------
def weights(t: np.ndarray, end_s: float, max_gap: float) -> np.ndarray:
    """Seconds each sample represents: gap to the next sample (or to the
    window end for the last one), clipped to [0, max_gap]."""
    if len(t) == 0:
        return np.zeros(0)
    nxt = np.append(t[1:], end_s)
    return np.clip(nxt - t, 0.0, max_gap)


@dataclass
class Stats:
    n: int
    min: float
    max: float
    mean: float
    std: float
    covered_s: float
    first: float
    last: float

    def as_dict(self) -> dict:
        return {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in self.__dict__.items()}


def tw_stats(t: np.ndarray, v: np.ndarray, end_s: float, max_gap: float) -> Stats:
    if len(v) == 0:
        nan = float("nan")
        return Stats(0, nan, nan, nan, nan, 0.0, nan, nan)
    w = weights(t, end_s, max_gap)
    tot = w.sum()
    if tot <= 0:
        mean = float(np.mean(v)); std = float(np.std(v))
    else:
        mean = float(np.sum(w * v) / tot)
        std = float(math.sqrt(max(np.sum(w * (v - mean) ** 2) / tot, 0.0)))
    return Stats(len(v), float(v.min()), float(v.max()), mean, std, float(tot), float(v[0]), float(v[-1]))


def tw_mean_window(t, v, a, b, max_gap):
    sel = (t >= a) & (t < b)
    if not sel.any():
        return float("nan")
    return tw_stats(t[sel], v[sel], b, max_gap).mean


def tw_slope_per_h(t, v, w) -> float:
    """Weighted least-squares slope, in units per hour."""
    if len(v) < 3 or w.sum() <= 0:
        return float("nan")
    tm = np.sum(w * t) / w.sum()
    vm = np.sum(w * v) / w.sum()
    den = np.sum(w * (t - tm) ** 2)
    return float(np.sum(w * (t - tm) * (v - vm)) / den * 3600) if den > 0 else float("nan")


# --------------------------------------------------------------------------
# Per-fraction view of the data
# --------------------------------------------------------------------------
class Window:
    """Cleaned samples of one column inside one [start, end) window."""

    def __init__(self, df_clean: pd.DataFrame, column: Column, start: pd.Timestamp, end: pd.Timestamp,
                 max_gap: float, removed: pd.DataFrame | None = None):
        self.column = column
        self.start = pd.Timestamp(start)
        self.end = pd.Timestamp(end)
        self.duration_s = (self.end - self.start).total_seconds()
        self.max_gap = max_gap
        self.dropped: dict[int, int] = {}
        if removed is not None and len(removed):
            r = removed[(removed["ts"] >= self.start) & (removed["ts"] < self.end)]
            self.dropped = {int(k): int(v) for k, v in r.groupby("tag").size().items()}
        sel = (df_clean["ts"] >= self.start) & (df_clean["ts"] < self.end)
        self.df = df_clean.loc[sel]
        self._series: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        for tag, g in self.df.groupby("tag", sort=False):
            t = (g["ts"] - self.start).dt.total_seconds().to_numpy()
            self._series[int(tag)] = (t, g["val"].to_numpy(dtype=float), g["backfilled"].to_numpy(dtype=bool))

    def series(self, sig: Signal | None):
        if sig is None or sig.tag not in self._series:
            return np.zeros(0), np.zeros(0), np.zeros(0, dtype=bool)
        return self._series[sig.tag]

    def stats(self, sig: Signal) -> Stats:
        t, v, _ = self.series(sig)
        return tw_stats(t, v, self.duration_s, self.max_gap)

    def coverage(self, sig: Signal | None) -> tuple[float, float]:
        """(fraction of the window covered by plausible data, share of that
        coverage that came from backfilled rows)."""
        t, _, bf = self.series(sig)
        if len(t) == 0 or self.duration_s <= 0:
            return 0.0, 0.0
        w = weights(t, self.duration_s, self.max_gap)
        tot = w.sum()
        return float(min(tot / self.duration_s, 1.0)), float(w[bf].sum() / tot) if tot else 0.0

    def gaps(self, sig: Signal | None) -> list[tuple[float, float]]:
        """Holes longer than max_gap, as (from_s, to_s), including a missing
        head or tail of the window."""
        t, _, _ = self.series(sig)
        if len(t) == 0:
            return [(0.0, self.duration_s)]
        edges = np.concatenate([[0.0], t, [self.duration_s]])
        d = np.diff(edges)
        idx = np.flatnonzero(d > self.max_gap)
        return [(float(edges[i]), float(edges[i + 1])) for i in idx]


# --------------------------------------------------------------------------
# Derived metrics
# --------------------------------------------------------------------------
def _align(t1, v1, t2, v2, tol_s=30.0):
    """Pair each sample of series 1 with the nearest sample of series 2
    within tol_s. Returns (t, v1, v2)."""
    if len(t1) == 0 or len(t2) == 0:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    a = pd.DataFrame({"t": t1, "a": v1})
    b = pd.DataFrame({"t": t2, "b": v2})
    m = pd.merge_asof(a, b, on="t", direction="nearest", tolerance=tol_s).dropna()
    return m["t"].to_numpy(), m["a"].to_numpy(), m["b"].to_numpy()


def delta_t_series(win: Window):
    pot = win.column.by_role("pot_temp")
    cond = win.column.by_role("condenser")
    tp, vp, _ = win.series(pot)
    tc, vc, _ = win.series(cond)
    t, a, b = _align(tp, vp, tc, vc)
    return t, a - b


def steady_state_s(t, v, band, window_s) -> float | None:
    """First time (s from start) after which v stays within +/-band of its own
    median for at least window_s. None if it never settles."""
    n = len(t)
    if n < 3:
        return None
    ends = np.searchsorted(t, t + window_s, side="left")
    for i in range(n):
        j = ends[i]
        if j >= n and t[-1] - t[i] < 0.9 * window_s:
            return None  # not enough time left to prove it
        seg = v[i:j]
        if len(seg) >= 3:
            med = np.median(seg)
            if np.all(np.abs(seg - med) <= band):
                return float(t[i])
    return None


def max_ramp_per_h(t, v, window_s) -> float:
    """Steepest absolute change over any window_s span, signed, degC/h.
    The series is first smoothed with a 2-minute median to stop a single noisy
    scan from looking like a ramp."""
    if len(t) < 5:
        return float("nan")
    s = pd.Series(v, index=pd.to_timedelta(t, unit="s")).rolling("120s").median().to_numpy()
    ends = np.searchsorted(t, t + window_s, side="left") - 1
    best = 0.0
    for i in range(len(t)):
        j = ends[i]
        span = t[j] - t[i]
        if span >= 0.8 * window_s:
            r = (s[j] - s[i]) / span * 3600
            if abs(r) > abs(best):
                best = r
    return float(best)


def excursion_events(t, v, centre, band, max_gap, min_s=60.0, merge_s=120.0) -> list[dict]:
    """Vacuum excursions as events, not as threshold crossings.

    An event is a stretch where |v - centre| > band. Stretches separated by
    less than merge_s are one event (a pressure that wobbles back across the
    line for a scan or two is still the same leak). Events shorter than min_s
    are noise and dropped. A data hole longer than max_gap ends an event: we
    cannot know what the pressure did while the logger was down.

    Returns [{start_s, end_s, minutes, peak_dev, peak}] in time order.
    """
    if len(v) == 0:
        return []
    out = np.abs(v - centre) > band
    if not out.any():
        return []
    raw = []          # (i_first, i_last) runs of consecutive out-of-band samples
    i, n = 0, len(v)
    while i < n:
        if not out[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and out[j + 1] and t[j + 1] - t[j] <= max_gap:
            j += 1
        raw.append([i, j])
        i = j + 1
    merged = [raw[0]]
    for i, j in raw[1:]:
        pi, pj = merged[-1]
        if t[i] - t[pj] <= merge_s and not np.any(np.diff(t[pj:i + 1]) > max_gap):
            merged[-1][1] = j
        else:
            merged.append([i, j])
    events = []
    for i, j in merged:
        end = t[j] + min(t[j + 1] - t[j], max_gap) if j + 1 < n else t[j]
        if end - t[i] < min_s:
            continue
        seg = v[i:j + 1]
        k = int(np.argmax(np.abs(seg - centre)))
        events.append({"start_s": float(t[i]), "end_s": float(end), "minutes": float((end - t[i]) / 60),
                       "peak_dev": float(seg[k] - centre), "peak": float(seg[k])})
    return events


def robust_sd(v) -> float:
    """Spread that ignores the excursions themselves: 1.4826 x median absolute
    deviation, which equals the standard deviation for normal noise."""
    if len(v) == 0:
        return float("nan")
    return float(1.4826 * np.median(np.abs(v - np.median(v))))


METRICS_VERSION = 2   # bump when a metric's definition changes; cached rows recompute


def fraction_metrics(win: Window, metric_cfg: dict) -> dict:
    """Every per-fraction number the app reports. Flat dict, one row per
    fraction, so it can be a table column set and later LEFT JOIN purity on
    lot number without restructuring."""
    col = win.column
    out: dict = {}
    if not col.has_process_data:
        return out
    dur = win.duration_s
    pot, vac, cond = col.by_role("pot_temp"), col.by_role("vacuum"), col.by_role("condenser")

    cov, bf_share = win.coverage(pot)
    out["coverage_pct"] = round(cov * 100, 1)
    out["backfilled_pct"] = round(bf_share * 100, 1)
    out["dropouts_removed"] = int(sum(win.dropped.values()))

    # Pot temperature
    if pot:
        t, v, _ = win.series(pot)
        st = tw_stats(t, v, dur, win.max_gap)
        out.update(pot_mean=st.mean, pot_min=st.min, pot_max=st.max, pot_std=st.std)
        head = min(900.0, dur / 10)
        out["pot_ramp_overall_c_per_h"] = (
            (tw_mean_window(t, v, dur - head, dur, win.max_gap) - tw_mean_window(t, v, 0, head, win.max_gap))
            / max(dur - head, 1) * 3600
        )
        out["pot_ramp_max_c_per_h"] = max_ramp_per_h(t, v, float(metric_cfg.get("ramp_window_min", 15)) * 60)
        ss = steady_state_s(t, v, float(metric_cfg.get("steady_band_c", 1.0)),
                            float(metric_cfg.get("steady_window_min", 20)) * 60)
        out["time_to_steady_min"] = None if ss is None else ss / 60
        thr = col.pot_temp_threshold
        if thr is not None and len(v):
            w = weights(t, dur, win.max_gap)
            out["pot_min_above_threshold"] = float(w[v > thr].sum() / 60)
            out["pot_threshold_c"] = float(thr)

    # Condenser and pot-to-condenser delta T
    if cond:
        st = win.stats(cond)
        out.update(cond_mean=st.mean, cond_min=st.min, cond_max=st.max)
    if pot and cond:
        t, d = delta_t_series(win)
        if len(d):
            w = weights(t, dur, win.max_gap)
            head = min(900.0, dur / 10)
            out["dT_mean"] = float(np.sum(w * d) / w.sum()) if w.sum() else float("nan")
            out["dT_start"] = tw_mean_window(t, d, 0, head, win.max_gap)
            out["dT_end"] = tw_mean_window(t, d, dur - head, dur, win.max_gap)
            out["dT_min"] = float(d.min())
            out["dT_change"] = out["dT_end"] - out["dT_start"]
            out["dT_slope_c_per_h"] = tw_slope_per_h(t, d, w)

    # Vacuum stability
    if vac:
        t, v, _ = win.series(vac)
        st = tw_stats(t, v, dur, win.max_gap)
        out.update(vac_mean=st.mean, vac_min=st.min, vac_max=st.max, vac_std=st.std)
        if len(v):
            med = float(np.median(v))
            rsd = robust_sd(v)
            # The band is whichever is wider: a fixed floor (sensor resolution
            # and normal wander) or k x the fraction's own robust noise.
            band = max(col.vacuum_band_abs, col.vacuum_band_rel * abs(med),
                       float(metric_cfg.get("vacuum_band_k", 4)) * rsd)
            ev = excursion_events(t, v, med, band, win.max_gap,
                                  float(metric_cfg.get("excursion_min_s", 60)),
                                  float(metric_cfg.get("excursion_merge_s", 120)))
            worst = max(ev, key=lambda e: abs(e["peak_dev"])) if ev else None
            out.update(vac_median=med, vac_noise_sd=rsd, vac_band=band, vac_excursions=len(ev),
                       vac_min_outside_band=sum(e["minutes"] for e in ev),
                       vac_max_deviation=float(np.max(np.abs(v - med))),
                       vac_worst_peak=worst["peak"] if worst else None,
                       vac_worst_at_min=worst["start_s"] / 60 if worst else None)
    out["_version"] = METRICS_VERSION
    return {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in out.items()}


# --------------------------------------------------------------------------
# Chart series
# --------------------------------------------------------------------------
def downsample_minmax(t: np.ndarray, v: np.ndarray, max_points: int):
    """Keep the min and max of each bucket, in time order, so spikes survive
    the reduction. A bucket holding a gap marker (NaN) keeps it, so line
    breaks survive too. Run this AFTER with_gap_breaks, on the raw spacing.
    Under the limit the data passes through untouched."""
    n = len(t)
    if n <= max_points:
        return t, v
    buckets = max_points // 3
    edges = np.linspace(0, n, buckets + 1).astype(int)
    keep = []
    isnan = np.isnan(v)
    for a, b in zip(edges[:-1], edges[1:]):
        if b <= a:
            continue
        seg = v[a:b]
        idx = set()
        if not isnan[a:b].all():
            idx |= {a + int(np.nanargmin(seg)), a + int(np.nanargmax(seg))}
        if isnan[a:b].any():
            idx.add(a + int(np.flatnonzero(isnan[a:b])[0]))
        keep.extend(sorted(idx))
    keep = np.array(keep)
    return t[keep], v[keep]


def with_gap_breaks(t: np.ndarray, v: np.ndarray, max_gap: float):
    """Insert a NaN after any hole longer than max_gap so the chart line
    breaks instead of drawing a straight bridge over missing data."""
    if len(t) < 2:
        return t, v
    holes = np.flatnonzero(np.diff(t) > max_gap)
    if not len(holes):
        return t, v
    t2 = np.insert(t, holes + 1, t[holes] + max_gap / 2)
    v2 = np.insert(v.astype(float), holes + 1, np.nan)
    return t2, v2


def profile(t: np.ndarray, v: np.ndarray, duration_s: float, bins: int, max_gap: float) -> np.ndarray:
    """Time-weighted mean of v in each of `bins` equal slices of the fraction:
    x becomes percent complete. Empty slices are NaN."""
    out = np.full(bins, np.nan)
    if len(t) == 0 or duration_s <= 0:
        return out
    w = weights(t, duration_s, max_gap)
    idx = np.clip((t / duration_s * bins).astype(int), 0, bins - 1)
    num = np.bincount(idx, weights=w * v, minlength=bins)
    den = np.bincount(idx, weights=w, minlength=bins)
    ok = den > 0
    out[ok] = num[ok] / den[ok]
    return out
