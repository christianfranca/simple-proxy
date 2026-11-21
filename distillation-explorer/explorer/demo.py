"""Synthetic stand-in for tp_datalog and the Eflow list.

Used only when config says backend = "demo" (python run.py --demo). It exists
so the app can be built and tested away from the plant network. The UI shows a
"demo data" banner whenever it is active. Nothing in here is a real reading.

It deliberately reproduces the gotchas in the brief: 0.0 dropouts on every
analog tag, a backfilled stretch at 10 s, a logger outage, vacuum spikes,
Eflow fractions listed out of order, a Reactor row spanning each run, and
CD01 / CDMOL runs with no process data at all.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config

T0 = datetime(2026, 7, 10)
T1 = datetime(2026, 8, 13)
STEP = 10  # seconds; live rows keep every second sample (20 s)

# CD02: Aug 1-6 only exists in the archive (backfilled, 10 s).
# CD03: Jul 19 06:00 - Jul 23 18:00 nothing at all (logger fault).
BACKFILL = {"CD02": (datetime(2026, 8, 1), datetime(2026, 8, 6, 12))}
OUTAGE = {"CD03": (datetime(2026, 7, 19, 6), datetime(2026, 7, 23, 18))}


def _schedule(rng, col: str, first_dist: int, start: datetime, end: datetime, first_fraction=None):
    """Back-to-back runs with 5-9 fractions each, 4-14 h per fraction.
    Only runs that finish before `end` are kept. `first_fraction` pins the
    first run's start and its fraction 1 window."""
    runs, t, d = [], start, first_dist
    while True:
        rs = t
        cur = rs + timedelta(minutes=int(rng.integers(60, 140)))
        fr = []
        for k in range(int(rng.integers(5, 10))):
            dur = timedelta(minutes=int(rng.integers(4 * 60, 14 * 60) // 10 * 10))
            if first_fraction and not runs and k == 0:
                rs, cur, dur = first_fraction[0], first_fraction[1], first_fraction[2] - first_fraction[1]
            fr.append((str(k + 1), cur, cur + dur, float(int(rng.integers(8, 70)))))
            cur += dur
        re = cur + timedelta(minutes=int(rng.integers(20, 90)))
        if re >= end:
            return runs
        runs.append({"col": col, "dist": d, "start": rs, "end": re, "fractions": fr})
        t = re + timedelta(hours=float(rng.integers(8, 36)))
        d += 1


def _signals(rng, runs, grid: pd.DatetimeIndex, col: str):
    n = len(grid)
    atm = 24.0 if col == "CD02" else 677.0
    run_vac = 4.5 if col == "CD02" else 140.0
    sp = np.full(n, 25.0)
    vac = np.full(n, atm)
    dT = np.full(n, 2.0)
    status = np.zeros(n)
    sec = (grid - grid[0]).total_seconds().to_numpy()
    for r in runs:
        a, b = grid.searchsorted(r["start"]), grid.searchsorted(r["end"])
        status[a:b] = 1
        vac[a:b] = run_vac * (1 + 0.04 * rng.standard_normal())
        sp[a:b] = 85.0
        for k, (_, fs, fe, _) in enumerate(r["fractions"]):
            i, j = grid.searchsorted(fs), grid.searchsorted(fe)
            sp[i:j] = 92 + 7.5 * k + rng.normal(0, 1.5)
            x = np.linspace(0, 1, max(j - i, 1))
            # pot-to-condenser dT closes through the cut as the next
            # component starts coming over
            dT[i:j] = (34 - 2 * k) * np.exp(-1.6 * x) + 6 + rng.normal(0, 0.8)
            status[i:j] = 2
            # a couple of vacuum excursions in some fractions
            for _ in range(int(rng.poisson(0.9))):
                c = int(rng.integers(i, max(j - 1, i + 1)))
                w = int(rng.integers(20, 120))
                bump = np.exp(-0.5 * ((np.arange(n)[max(c - 4 * w, 0):c + 4 * w] - c) / w) ** 2)
                vac[max(c - 4 * w, 0):c + 4 * w] += bump * run_vac * rng.uniform(0.3, 0.9)
    # first-order lag on the pot (it tracks setpoint over ~25 min)
    pot = pd.Series(sp).ewm(span=150).mean().to_numpy() + rng.normal(0, 0.25, n)
    vac = pd.Series(vac).ewm(span=12).mean().to_numpy() * (1 + rng.normal(0, 0.008, n))
    cond = pot - pd.Series(dT).ewm(span=60).mean().to_numpy() + rng.normal(0, 0.2, n)
    cond = np.where(status > 0, cond, 24 + rng.normal(0, 0.2, n))
    level = np.clip(80 - np.cumsum(status == 2) * 0.0009 % 70, 5, 95)
    mid = lambda f: cond + (pot - cond) * f + rng.normal(0, 0.3, n)
    out = {
        "pot_temp": pot, "temp_sp": sp, "vacuum": vac, "vacuum_2": vac * 1.02 + rng.normal(0, 0.01 * vac.mean(), n),
        "condenser": cond, "level": level, "status": status,
    }
    return out, mid, sec


def make(cfg: Config) -> tuple[Path, Path]:
    rng = np.random.default_rng(190)
    db_path = cfg.path(cfg.process["demo"]["path"])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.executescript(
        "CREATE TABLE TagTable (TagIndex INTEGER, TagName TEXT, TagType INTEGER, TagDataType INTEGER);"
        "CREATE TABLE FloatTableAll (DateAndTime TEXT, Millitm INTEGER, TagIndex INTEGER, Val REAL, "
        "Status TEXT, Marker TEXT, IsBackfilled INTEGER);"
    )
    grid = pd.date_range(T0, T1, freq=f"{STEP}s", inclusive="left")
    all_runs = []
    # CD02 distillation 190 is pinned to the brief's example: run start
    # 7/27 23:20:16 (its Eflow ID), fraction 1 from 7/28 01:00 to 10:50.
    pin = (datetime(2026, 7, 27, 23, 20, 16), datetime(2026, 7, 28, 1, 0), datetime(2026, 7, 28, 10, 50))
    before = _schedule(rng, "CD02", 0, T0 + timedelta(hours=7), pin[0] - timedelta(hours=8))
    for i, r in enumerate(before):
        r["dist"] = 190 - len(before) + i
    cd02 = before + _schedule(rng, "CD02", 190, pin[0], T1, pin)
    for col, runs in (("CD02", cd02), ("CD03", _schedule(rng, "CD03", 412, T0 + timedelta(hours=3), T1))):
        all_runs += runs
        sig, mid, _ = _signals(rng, runs, grid, col)
        live = np.zeros(len(grid), bool)
        live[::2] = True
        bf = np.zeros(len(grid), bool)
        if col in BACKFILL:
            a, b = grid.searchsorted(BACKFILL[col][0]), grid.searchsorted(BACKFILL[col][1])
            live[a:b] = False
            bf[a:b] = True
        if col in OUTAGE:
            a, b = grid.searchsorted(OUTAGE[col][0]), grid.searchsorted(OUTAGE[col][1])
            live[a:b] = False
        keep = live | bf
        ts = grid[keep].strftime("%Y-%m-%d %H:%M:%S").to_numpy()
        bfk = bf[keep].astype(int)
        colcfg = cfg.columns[col]
        col_temps = [s for s in colcfg.signals if s.role == "column"]
        for s in colcfg.signals:
            con.execute("INSERT INTO TagTable VALUES (?,?,?,?)", [s.tag, s.name, 1, 1])
            if s.role == "column":
                v = mid(0.25 + 0.5 * col_temps.index(s) / max(len(col_temps) - 1, 1))
            else:
                v = sig[s.role]
            v = v[keep].astype(float).copy()
            if s.drop_zero:  # comms dropouts: isolated zeros plus short bursts
                v[rng.random(len(v)) < 0.003] = 0.0
                for c in rng.integers(0, len(v), 6):
                    v[c:c + int(rng.integers(5, 60))] = 0.0
            con.executemany(
                "INSERT INTO FloatTableAll VALUES (?,0,?,?,' ','',?)",
                zip(ts, [s.tag] * len(ts), np.round(v, 3).tolist(), bfk.tolist()),
            )
    con.execute("CREATE INDEX ix ON FloatTableAll (TagIndex, DateAndTime)")
    con.commit()
    con.close()

    # Runs on the two columns without logging
    for col, first in (("CD01", 77), ("CDMOL", 31)):
        all_runs += _schedule(rng, col, first, T0, T1)

    fmt = lambda d: f"{d.month}/{d.day}/{d.strftime('%y')} {d.strftime('%I:%M %p').lstrip('0')}"
    rows = []
    lot = 5100
    for r in all_runs:
        rid = r["start"].strftime("%Y%m%d%H%M%S")
        rows.append({"ID - fraction": int(rid), "Coluna": r["col"], "Distillation": r["dist"], "Fraction": "Reactor",
                     "Lote": None, "SumWeighing (Kg)": None,
                     "Start Date (EST)": fmt(r["start"]), "End Date (EST)": fmt(r["end"])})
        for f, fs, fe, kg in r["fractions"]:
            lot += 1
            rows.append({"ID - fraction": int(rid), "Coluna": r["col"], "Distillation": r["dist"], "Fraction": f,
                         "Lote": f"VBAR00{lot}", "SumWeighing (Kg)": kg,
                         "Start Date (EST)": fmt(fs), "End Date (EST)": fmt(fe)})
    rng.shuffle(rows)  # the source is not ordered; the app must sort
    ef = cfg.path("data/demo_eflow.json")
    ef.write_text(json.dumps({"eflowData": rows, "success": True}, indent=1))
    return db_path, ef
