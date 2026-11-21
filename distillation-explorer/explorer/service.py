"""What the web layer calls. Joins Eflow fractions (from the local cache) with
process data (from the datalog) and returns plain dicts / DataFrames.

Time on the wire: charts get "plant clock seconds", i.e. the naive plant-local
timestamp read as if it were UTC. The browser then formats it in UTC, so a
chart shows exactly the wall-clock time the datalog stored, whatever time zone
the browser thinks it is in. No zone conversion happens anywhere.
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from . import analysis as an
from .config import Column, Config
from .process_db import ProcessDB
from .store import Store

EPOCH = pd.Timestamp("1970-01-01")


def clock_s(ts) -> float:
    return (pd.Timestamp(ts) - EPOCH).total_seconds()


def natural_key(s: str):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s or "")]


def is_reactor(fr: dict) -> bool:
    return fr["fraction"].strip().lower() == "reactor"


def _nan_to_none(a):
    return [None if (x is None or (isinstance(x, float) and not np.isfinite(x))) else float(x) for x in a]


class _LRU(OrderedDict):
    def __init__(self, size=24):
        super().__init__()
        self.size = size
        self.lock = threading.Lock()

    def get_or(self, key, fn):
        with self.lock:
            if key in self:
                self.move_to_end(key)
                return self[key]
        val = fn()
        with self.lock:
            self[key] = val
            while len(self) > self.size:
                self.popitem(last=False)
        return val


class Service:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.path(cfg.app.get("cache_db", "data/cache.sqlite")))
        self.db = ProcessDB(cfg)
        self._frames = _LRU(24)
        self._profiles = _LRU(4000)

    @property
    def demo(self) -> bool:
        return self.db.backend == "demo"

    def column(self, code: str) -> Column:
        c = self.cfg.columns.get(code)
        if c is None:
            # A column Eflow knows but config does not: show fractions only.
            c = Column(code=code, label=code, signals=())
        return c

    def column_codes(self) -> list[str]:
        codes = list(self.cfg.columns)
        for r in self.store.all_fractions():
            if r["col"] and r["col"] not in codes:
                codes.append(r["col"])
        return codes

    # --- process data with a small in-memory cache ---------------------------
    def frame(self, col: Column, start, end, roles: tuple[str, ...] | None = None):
        sigs = [s for s in col.signals if roles is None or s.role in roles]
        tags = tuple(sorted(s.tag for s in sigs))
        start, end = pd.Timestamp(start), pd.Timestamp(end)

        def load():
            raw = self.db.fetch(list(tags), start.to_pydatetime(), end.to_pydatetime())
            sub = Column(col.code, col.label, tuple(sigs), col.vacuum_band_abs, col.vacuum_band_rel,
                         col.pot_temp_threshold)
            return an.clean(raw, sub)

        return self._frames.get_or((col.code, start, end, tags), load)

    # --- runs ------------------------------------------------------------------
    def runs(self, code: str) -> list[dict]:
        groups: dict[str, list[dict]] = {}
        for f in self.store.fractions(code):
            groups.setdefault(f["distillation"], []).append(f)
        out = []
        for dist, frs in groups.items():
            starts = [f["start"] for f in frs if f["start"]]
            ends = [f["end"] for f in frs if f["end"]]
            numbered = [f for f in frs if not is_reactor(f)]
            out.append({
                "distillation": dist,
                "start": min(starts) if starts else None,
                "end": max(ends) if ends else None,
                "fractions": len(numbered),
                "kg": sum(f["kg"] or 0 for f in numbered),
                "has_reactor": any(is_reactor(f) for f in frs),
            })
        out.sort(key=lambda r: (r["start"] or "", natural_key(r["distillation"])), reverse=True)
        return out

    def run_fractions(self, code: str, dist: str) -> list[dict]:
        frs = self.store.fractions(code, dist)
        # Sort by start time; the source order means nothing.
        frs.sort(key=lambda f: (f["start"] or "9999", natural_key(f["fraction"])))
        return frs

    def run_window(self, frs: list[dict]):
        starts = [f["start"] for f in frs if f["start"]]
        ends = [f["end"] for f in frs if f["end"]]
        if not starts or not ends:
            return None, None
        return pd.Timestamp(min(starts)), pd.Timestamp(max(ends))

    def metrics_for(self, col: Column, fr: dict, df_clean=None, removed=None, force=False) -> dict:
        if not col.has_process_data or not fr["start"] or not fr["end"] or fr["end"] <= fr["start"]:
            return {}
        if not force:
            hit = self.store.get_metrics(col.code, fr["distillation"], fr["fraction"], fr["start"], fr["end"])
            if hit is not None:
                return hit
        if df_clean is None:
            df_clean, removed = self.frame(col, fr["start"], fr["end"])
        win = an.Window(df_clean, col, fr["start"], fr["end"], self.cfg.max_gap_s, removed)
        m = an.fraction_metrics(win, self.cfg.metrics)
        # A fraction that has not finished yet, or whose data is still being
        # backfilled, should not be frozen in the cache.
        if pd.Timestamp(fr["end"]) < pd.Timestamp.now() - timedelta(hours=1) and m.get("coverage_pct", 0) > 0:
            self.store.put_metrics(col.code, fr["distillation"], fr["fraction"], fr["lot"], fr["start"], fr["end"], m)
        return m

    def run_detail(self, code: str, dist: str, recompute=False) -> dict:
        col = self.column(code)
        frs = self.run_fractions(code, dist)
        start, end = self.run_window(frs)
        out = {"column": code, "distillation": dist, "has_process_data": col.has_process_data,
               "start": str(start) if start is not None else None, "end": str(end) if end is not None else None,
               "fractions": [], "overview": None}
        need_full = recompute or any(
            self.store.get_metrics(code, f["distillation"], f["fraction"], f["start"], f["end"]) is None
            for f in frs if not is_reactor(f) and f["start"] and f["end"])
        full = self.frame(col, start, end) if (col.has_process_data and start is not None and need_full) else (None, None)
        for f in frs:
            row = {k: f[k] for k in ("id", "fraction", "lot", "kg", "start", "end", "run_id")}
            row["reactor"] = is_reactor(f)
            row["duration_h"] = ((pd.Timestamp(f["end"]) - pd.Timestamp(f["start"])).total_seconds() / 3600
                                 if f["start"] and f["end"] else None)
            row["metrics"] = ({} if row["reactor"] else
                              self.metrics_for(col, f, full[0], full[1], force=recompute))
            out["fractions"].append(row)
        if col.has_process_data and start is not None:
            out["overview"] = self.overview(col, start, end)
        return out

    def overview(self, col: Column, start, end, max_points=2500) -> dict:
        roles = ("pot_temp", "temp_sp", "condenser", "vacuum")
        df, _ = self.frame(col, start, end, roles)
        win = an.Window(df, col, start, end, self.cfg.max_gap_s)
        return self._series_payload(win, [col.by_role(r) for r in roles], max_points)

    def _series_payload(self, win: an.Window, sigs, max_points) -> dict:
        t0 = clock_s(win.start)
        series = []
        for s in sigs:
            if s is None:
                continue
            t, v, _ = win.series(s)
            t, v = an.with_gap_breaks(t, v, self.cfg.max_gap_s)   # on raw spacing
            t, v = an.downsample_minmax(t, v, max_points)
            series.append({"tag": s.tag, "label": s.label, "role": s.role, "unit": s.unit,
                           "x": [round(t0 + x, 1) for x in t], "y": _nan_to_none(np.round(v, 3))})
        pot = win.column.by_role("pot_temp")
        cov, bf = win.coverage(pot)
        return {"t0": t0, "t1": t0 + win.duration_s, "series": series,
                "coverage_pct": round(cov * 100, 1), "backfilled_pct": round(bf * 100, 1),
                "gaps": [[t0 + a, t0 + b] for a, b in win.gaps(pot)]}

    # --- one fraction ------------------------------------------------------------
    def fraction_detail(self, fid: int) -> dict | None:
        fr = self.store.fraction_by_id(fid)
        if not fr:
            return None
        col = self.column(fr["col"])
        out = {"fraction": fr, "column": col.code, "has_process_data": col.has_process_data,
               "reactor": is_reactor(fr)}
        if not col.has_process_data or not fr["start"] or not fr["end"]:
            return out
        df, removed = self.frame(col, fr["start"], fr["end"])
        win = an.Window(df, col, fr["start"], fr["end"], self.cfg.max_gap_s, removed)
        stats = []
        for s in col.signals:
            st = win.stats(s)
            cov, bf = win.coverage(s)
            stats.append({"tag": s.tag, "name": s.name, "label": s.label, "role": s.role, "unit": s.unit,
                          **st.as_dict(), "coverage_pct": round(cov * 100, 1), "backfilled_pct": round(bf * 100, 1),
                          "dropouts_removed": win.dropped.get(s.tag, 0)})
        payload = self._series_payload(win, list(col.signals), 6000)
        t, d = an.delta_t_series(win)
        t, d = an.with_gap_breaks(t, d, self.cfg.max_gap_s)
        payload["delta_t"] = {"x": [round(payload["t0"] + x, 1) for x in t], "y": _nan_to_none(np.round(d, 3))}
        out.update(stats=stats, chart=payload, metrics=self.metrics_for(col, fr, df, removed),
                   band={"vacuum": [col.vacuum_band_abs, col.vacuum_band_rel]})
        return out

    def fraction_frame(self, fid: int) -> tuple[dict, pd.DataFrame]:
        """Cleaned samples of one fraction, wide (one column per label), for export."""
        fr = self.store.fraction_by_id(fid)
        col = self.column(fr["col"])
        if not col.has_process_data:
            return fr, pd.DataFrame()
        df, _ = self.frame(col, fr["start"], fr["end"])
        df = df[(df["ts"] >= pd.Timestamp(fr["start"])) & (df["ts"] < pd.Timestamp(fr["end"]))]
        names = {s.tag: f"{s.label} [{s.unit}]" if s.unit else s.label for s in col.signals}
        wide = df.pivot_table(index="ts", columns="tag", values="val", aggfunc="first")
        wide = wide.reindex(columns=[s.tag for s in col.signals if s.tag in wide.columns]).rename(columns=names)
        bf = df.groupby("ts")["backfilled"].max()
        wide.insert(0, "backfilled", bf)
        wide.index.name = "time (plant local)"
        return fr, wide.reset_index()

    # --- cross-run comparison ---------------------------------------------------
    def fraction_labels(self, code: str) -> list[str]:
        labels = {f["fraction"] for f in self.store.fractions(code) if not is_reactor(f)}
        return sorted(labels, key=natural_key)

    def compare(self, code: str, fraction: str, role: str, dists: list[str] | None, limit=30) -> dict:
        col = self.column(code)
        frs = [f for f in self.store.fractions(code) if f["fraction"] == fraction and f["start"] and f["end"]
               and f["end"] > f["start"]]
        frs.sort(key=lambda f: f["start"], reverse=True)
        available = [{"distillation": f["distillation"], "start": f["start"], "lot": f["lot"]} for f in frs]
        if dists:
            frs = [f for f in frs if f["distillation"] in set(dists)]
        else:
            frs = frs[:limit]
        sig = col.by_role(role) if role != "delta_t" else None
        bins = int(self.cfg.metrics.get("profile_bins", 200))
        x = [round((i + 0.5) * 100 / bins, 3) for i in range(bins)]
        runs = []
        if col.has_process_data:
            for f in frs:
                key = (code, f["distillation"], f["fraction"], f["start"], f["end"], role, bins)

                def build(f=f):
                    df, _ = self.frame(col, f["start"], f["end"],
                                             ("pot_temp", "condenser", "vacuum"))
                    win = an.Window(df, col, f["start"], f["end"], self.cfg.max_gap_s)
                    if role == "delta_t":
                        t, v = an.delta_t_series(win)
                    else:
                        t, v, _ = win.series(sig)
                    prof = an.profile(t, v, win.duration_s, bins, self.cfg.max_gap_s)
                    cov, _ = win.coverage(col.by_role("pot_temp"))
                    return {"y": _nan_to_none(np.round(prof, 3)), "coverage_pct": round(cov * 100, 1)}

                p = self._profiles.get_or(key, build)
                runs.append({"distillation": f["distillation"], "lot": f["lot"], "kg": f["kg"],
                             "start": f["start"], "end": f["end"],
                             "duration_h": (pd.Timestamp(f["end"]) - pd.Timestamp(f["start"])).total_seconds() / 3600,
                             "metrics": self.metrics_for(col, f), **p})
        unit = "°C" if role == "delta_t" else (sig.unit if sig else "")
        label = "Pot − condenser ΔT" if role == "delta_t" else (sig.label if sig else role)
        return {"column": code, "fraction": fraction, "role": role, "label": label, "unit": unit,
                "x": x, "runs": runs, "available": available, "has_process_data": col.has_process_data}

    # --- coverage -----------------------------------------------------------------
    def coverage(self, code: str) -> dict:
        col = self.column(code)
        rows, meta = self.store.coverage(code)
        return {"column": code, "has_process_data": col.has_process_data, "meta": meta,
                "hours": rows, "gaps": self._gaps_from_hours(rows, meta),
                "basis": (col.by_role("pot_temp").label + f" (tag {col.by_role('pot_temp').tag})")
                if col.by_role("pot_temp") else None}

    def scan_coverage(self, code: str, since: datetime | None = None) -> dict:
        """Ask the datalog for per-hour covered minutes. Scans from `since`, or
        from the last scan's end minus two days (backfill can land late), or
        from the first sample. Chunked by 30 days to keep each query modest."""
        col = self.column(code)
        sig = col.by_role("pot_temp")
        if sig is None:
            return self.coverage(code)
        _, meta = self.store.coverage(code)
        lo, hi = self.db.extent(sig.tag)
        if lo is None:
            return self.coverage(code)
        if since is None:
            since = (pd.Timestamp(meta["scanned_to"]) - timedelta(days=2)).to_pydatetime() if meta else lo
        else:
            self.store.clear_coverage(code)  # a full rescan starts clean
        since = max(pd.Timestamp(since), pd.Timestamp(lo)).floor("h").to_pydatetime()
        stop = pd.Timestamp(hi).floor("h").to_pydatetime() + timedelta(hours=1)
        cur = since
        while cur < stop:
            nxt = min(cur + timedelta(days=30), stop)
            df = self.db.coverage_hourly(sig, cur, nxt)
            rows = []
            if not df.empty:
                piv = df.pivot_table(index="hour", columns="backfilled", values="minutes", aggfunc="sum").fillna(0)
                for h, r in piv.iterrows():
                    rows.append((h.strftime("%Y-%m-%d %H:00"), int(r.get(False, 0)), int(r.get(True, 0))))
            self.store.put_coverage(code, rows, cur.strftime("%Y-%m-%d %H:00"), nxt.strftime("%Y-%m-%d %H:00"))
            cur = nxt
        return self.coverage(code)

    @staticmethod
    def _gaps_from_hours(rows, meta, min_hours=2):
        """Stretches of whole hours with under 5 covered minutes."""
        if not meta:
            return []
        have = {r["hour"]: r["live_min"] + r["backfilled_min"] for r in rows}
        t = pd.Timestamp(meta["scanned_from"])
        end = pd.Timestamp(meta["scanned_to"])
        gaps, run_start = [], None
        while t < end:
            empty = have.get(t.strftime("%Y-%m-%d %H:00"), 0) < 5
            if empty and run_start is None:
                run_start = t
            if not empty and run_start is not None:
                gaps.append((run_start, t))
                run_start = None
            t += timedelta(hours=1)
        if run_start is not None:
            gaps.append((run_start, end))
        return [{"from": str(a), "to": str(b), "hours": (b - a).total_seconds() / 3600}
                for a, b in gaps if (b - a).total_seconds() / 3600 >= min_hours]
