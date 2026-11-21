"""One test per gotcha in the brief. Run: python -m pytest -q"""
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from explorer import analysis as an
from explorer import config as config_mod
from explorer.eflow import TRUNCATION_TELLS, normalise, parse_date, refresh
from explorer.process_db import ReadOnlyViolation, _guard
from explorer.store import Store

CFG = config_mod.load()
CD02 = CFG.columns["CD02"]
POT = CD02.by_role("pot_temp")


def frame(rows):
    df = pd.DataFrame(rows, columns=["ts", "tag", "val", "backfilled"])
    df["ts"] = pd.to_datetime(df["ts"])
    return df


# --- read-only ----------------------------------------------------------------
@pytest.mark.parametrize("sql", [
    "DELETE FROM FloatTable", "SELECT 1; DROP TABLE x", "UPDATE t SET a=1",
    "SELECT * INTO new_t FROM FloatTableAll", "EXEC sp_who", "  insert into x values (1)",
    "WITH a AS (SELECT 1) DELETE FROM t",
])
def test_guard_rejects_writes(sql):
    with pytest.raises(ReadOnlyViolation):
        _guard(sql)


def test_guard_allows_select():
    _guard("SELECT DATEADD(millisecond, Millitm, DateAndTime) AS ts, TagIndex FROM FloatTableAll WHERE TagIndex IN (?)")


# --- dropouts -------------------------------------------------------------------
def test_zero_dropouts_removed_before_min():
    df = frame([("2026-07-28 01:00:00", POT.tag, 95.0, 0), ("2026-07-28 01:00:20", POT.tag, 0.0, 0),
                ("2026-07-28 01:00:40", POT.tag, 96.0, 0)])
    clean, removed = an.clean(df, CD02)
    assert clean["val"].min() == 95.0
    assert len(removed) == 1


def test_status_zero_is_kept():
    st = CD02.by_role("status")
    df = frame([("2026-07-28 01:00:00", st.tag, 0.0, 0)])
    clean, _ = an.clean(df, CD02)
    assert len(clean) == 1


# --- mixed sample rate ------------------------------------------------------------
def test_time_weighted_mean_ignores_sample_rate():
    # first hour at 50 sampled every 20 s, second hour at 100 sampled every 10 s
    t1 = np.arange(0, 3600, 20.0)
    t2 = np.arange(3600, 7200, 10.0)
    t = np.concatenate([t1, t2])
    v = np.concatenate([np.full(len(t1), 50.0), np.full(len(t2), 100.0)])
    st = an.tw_stats(t, v, 7200, max_gap=120)
    assert st.mean == pytest.approx(75.0, abs=0.01)   # a plain mean would say ~83.3
    assert np.mean(v) > 83


def test_gap_does_not_get_weight():
    t = np.array([0.0, 20.0, 3600.0, 3620.0])
    w = an.weights(t, 3640.0, 120)
    assert w.max() == 120


# --- half-open fraction boundaries --------------------------------------------------
def test_boundary_sample_lands_in_one_fraction():
    df = frame([("2026-07-28 10:50:00", POT.tag, 100.0, 0)])
    a = an.Window(df, CD02, pd.Timestamp("2026-07-28 01:00"), pd.Timestamp("2026-07-28 10:50"), 120)
    b = an.Window(df, CD02, pd.Timestamp("2026-07-28 10:50"), pd.Timestamp("2026-07-28 15:50"), 120)
    assert len(a.df) == 0 and len(b.df) == 1


# --- vacuum excursions ------------------------------------------------------------
def test_excursion_events():
    t = np.arange(0, 7200, 10.0)
    v = np.full(len(t), 12.73)
    v[100:130] = 25.0      # 5 min leak
    v[131:140] = 25.0      # dips back for one scan: same event
    v[400:402] = 25.0      # 20 s blip: noise, dropped
    v[500:520] = 20.0      # 200 s pump trip: second event
    ev = an.excursion_events(t, v, 12.73, 2.0, 120, min_s=60, merge_s=120)
    assert len(ev) == 2
    assert ev[0]["start_s"] == 1000 and ev[0]["minutes"] == pytest.approx(400 / 60)
    assert ev[0]["peak"] == 25.0


def test_excursion_ends_at_data_hole():
    t = np.concatenate([np.arange(0, 300, 10.0), np.arange(3000, 3300, 10.0)])
    v = np.full(len(t), 25.0)
    ev = an.excursion_events(t, v, 12.73, 2.0, 120, min_s=60, merge_s=120)
    assert len(ev) == 2


def test_profile_percent_axis():
    t = np.arange(0, 1000, 10.0)
    p = an.profile(t, t, 1000, 10, 120)
    assert len(p) == 10 and p[0] < p[-1]


# --- Eflow ---------------------------------------------------------------------------
FIELDS = CFG.eflow["fields"]


def row(frac, start, end, dist=190, col="CD02", lot="VBAR005198"):
    f = FIELDS
    return {f["run_id"]: "20260727232016", f["column"]: col, f["distillation"]: str(dist), f["fraction"]: frac,
            f["lot"]: lot, f["kg"]: 26.0, f["start"]: start, f["end"]: end}


def test_eflow_date_format():
    assert parse_date("7/28/26 1:00 AM", []) == datetime(2026, 7, 28, 1, 0)
    assert parse_date("7/28/26 10:50 PM", []) == datetime(2026, 7, 28, 22, 50)


def test_eflow_utc_becomes_plant_time():
    # the real flow sends UTC: run 190 fraction 1 starts 05:00Z = 01:00 EDT
    assert parse_date("2026-07-28T05:00:00Z", []) == datetime(2026, 7, 28, 1, 0)
    assert parse_date("2026-01-15T06:00:00Z", []) == datetime(2026, 1, 15, 1, 0)   # EST in winter


def test_round_hundred_counts_flagged():
    from explorer.eflow import looks_truncated
    assert looks_truncated(600) and looks_truncated(5000) and not looks_truncated(119)


def test_body_template_per_column():
    from explorer.eflow import _fill
    assert _fill({"columnId": "{column}", "action": "get"}, "CD02") == {"columnId": "CD02", "action": "get"}


def test_fraction_stays_string():
    out, _ = normalise([row("Reactor", "7/27/26 11:20 PM", "7/31/26 12:27 PM"), row(1, "7/28/26 1:00 AM", "7/28/26 10:50 AM")], FIELDS)
    assert [r["fraction"] for r in out] == ["Reactor", "1"]
    assert out[1]["distillation"] == "190"


def test_truncation_warning(tmp_path):
    store = Store(tmp_path / "c.sqlite")
    rows = [row(str(i), "7/28/26 1:00 AM", "7/28/26 2:00 AM", lot=f"L{i}") for i in range(100)]
    rep = refresh(CFG, store, {"eflowData": rows, "success": True})
    assert 100 in TRUNCATION_TELLS
    assert any("SUSPICIOUS" in w for w in rep["warnings"])


def test_refresh_reports_changes(tmp_path):
    store = Store(tmp_path / "c.sqlite")
    a = [row("1", "7/28/26 1:00 AM", "7/28/26 10:50 AM", lot="A"), row("2", "7/28/26 10:50 AM", "7/28/26 3:50 PM", lot="B")]
    refresh(CFG, store, {"eflowData": a, "success": True})
    b = [row("1", "7/28/26 1:00 AM", "7/28/26 11:00 AM", lot="A"), row("3", "7/28/26 3:50 PM", "7/28/26 8:00 PM", lot="C")]
    rep = refresh(CFG, store, {"eflowData": b, "success": True})
    assert len(rep["added"]) == 1 and len(rep["removed"]) == 1 and len(rep["changed"]) == 1
    assert any("DROPPED" in w for w in rep["warnings"]) is False  # 2 -> 2 rows


def test_venting_in_last_fraction_is_not_an_excursion():
    import pandas as pd
    vac = CD02.by_role("vacuum")
    ts = pd.date_range("2026-07-31 03:45", periods=720, freq="10s")       # 2 h
    v = np.full(len(ts), 12.73)
    v[300:320] = 30.0                                                   # a real leak mid-cut
    v[-60:] = np.linspace(100, 1015, 60)                                # vented in the last 10 min
    df = pd.DataFrame({"ts": ts, "tag": vac.tag, "val": v, "backfilled": False})
    win = an.Window(df, CD02, ts[0], ts[-1] + pd.Timedelta(seconds=10), 120)
    last = an.fraction_metrics(win, CFG.metrics, is_last=True)
    mid = an.fraction_metrics(win, CFG.metrics, is_last=False)
    assert last["vac_excursions"] == 1 and last["vac_vented_at_min"] == pytest.approx(110)
    assert last["vac_max"] < 100                    # stats stop where the venting starts
    assert mid["vac_excursions"] == 2 and mid["vac_vented_at_min"] is None
