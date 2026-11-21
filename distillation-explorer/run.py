"""Distillation run explorer: one command to run it.

    python run.py                      serve against the real datalog (http://127.0.0.1:5080)
    python run.py --demo               serve against synthetic demo data
    python run.py --refresh-eflow      pull the Eflow list into the local cache and report changes
    python run.py --check-run CD02 190 print the fraction / process-data join for one run (no UI)
    python run.py --scan-coverage      scan the datalog for coverage, all logged columns

Add --demo to any of these to use the synthetic data instead.
"""
from __future__ import annotations

import argparse
import sys

from explorer import config as config_mod


def demo_overrides() -> dict:
    return {
        "process": {"backend": "demo"},
        "app": {"cache_db": "data/demo_cache.sqlite"},
        "eflow": {"url": "data/demo_eflow.json"},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="use synthetic demo data")
    ap.add_argument("--make-demo", action="store_true", help="(re)generate the demo datalog and Eflow file")
    ap.add_argument("--refresh-eflow", action="store_true")
    ap.add_argument("--check-run", nargs=2, metavar=("COLUMN", "DISTILLATION"))
    ap.add_argument("--scan-coverage", action="store_true")
    ap.add_argument("--port", type=int)
    a = ap.parse_args(argv)

    cfg = config_mod.load(overrides=demo_overrides() if (a.demo or a.make_demo) else None)

    from explorer import demo, eflow
    from explorer.process_db import demo_path_exists
    from explorer.service import Service

    if a.make_demo or (a.demo and not demo_path_exists(cfg)):
        print("Generating demo data (synthetic, about a minute)...")
        db, ef = demo.make(cfg)
        print(f"  {db}\n  {ef}")
        if a.make_demo and not (a.refresh_eflow or a.check_run or a.scan_coverage):
            return 0

    svc = Service(cfg)
    if a.demo and not svc.store.all_fractions():
        a.refresh_eflow = True

    if a.refresh_eflow:
        rep = eflow.refresh(cfg, svc.store)
        print(f"Eflow: {rep['rows']} rows, {len(rep['added'])} added, {len(rep['removed'])} removed, "
              f"{len(rep['changed'])} changed. Columns: {', '.join(rep['columns'])}")
        for w in rep["warnings"]:
            print("  WARNING:", w)

    if a.scan_coverage:
        for code, col in cfg.columns.items():
            if col.has_process_data:
                cov = svc.scan_coverage(code)
                print(f"{code}: {len(cov['hours'])} hours with data, {len(cov['gaps'])} gaps of 2 h or more")
                for g in cov["gaps"]:
                    print(f"   gap {g['from']} -> {g['to']}  ({g['hours']:.0f} h)")

    if a.check_run:
        return check_run(svc, *a.check_run)

    if a.refresh_eflow or a.scan_coverage:
        return 0

    from explorer.web import create_app

    app = create_app(cfg, svc)
    host = cfg.app.get("host", "127.0.0.1")
    port = a.port or int(cfg.app.get("port", 5080))
    print(f"Distillation run explorer on http://{host}:{port}  ({'DEMO DATA' if svc.demo else 'tp_datalog'})")
    try:
        from waitress import serve  # production-grade server if installed
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, debug=False, threaded=True)
    return 0


def check_run(svc, col: str, dist: str) -> int:
    """The 'prove the interval join' step: fractions of one run with the
    time-weighted stats per fraction, as text, for comparing against
    fraction_stats.py."""
    import pandas as pd

    d = svc.run_detail(col.upper(), dist, recompute=True)
    if not d["fractions"]:
        print(f"No fractions for {col} {dist} in the Eflow cache. Run --refresh-eflow first.")
        return 1
    print(f"{col} distillation {dist}: {d['start']} -> {d['end']}"
          f"{'' if d['has_process_data'] else '  (no process data logged)'}")
    rows = []
    for f in d["fractions"]:
        m = f["metrics"]
        rows.append({
            "fraction": f["fraction"], "lot": f["lot"], "kg": f["kg"], "start": f["start"], "end": f["end"],
            "h": round(f["duration_h"] or 0, 2),
            "cov%": m.get("coverage_pct"), "bf%": m.get("backfilled_pct"), "drop": m.get("dropouts_removed"),
            "pot avg": m.get("pot_mean"), "pot min": m.get("pot_min"), "pot max": m.get("pot_max"),
            "vac avg": m.get("vac_mean"), "vac std": m.get("vac_std"), "vac exc": m.get("vac_excursions"),
            "dT start": m.get("dT_start"), "dT end": m.get("dT_end"),
        })
    with pd.option_context("display.width", 220, "display.max_columns", 30, "display.float_format", "{:.2f}".format):
        print(pd.DataFrame(rows).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
