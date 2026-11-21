"""Flask app: one HTML shell plus a small JSON API and export endpoints.

The page keeps its state (column, run, fraction, view) in the URL query, so a
browser bookmark or a pasted link reopens exactly the same view.
"""
from __future__ import annotations

import io
import json
import math
import traceback
from datetime import datetime

import pandas as pd
from flask import Flask, Response, jsonify, render_template, request

from . import eflow
from .config import Config
from .service import Service, is_reactor

ROLES_COMPARE = [("pot_temp", "Pot temp"), ("vacuum", "Vacuum"), ("condenser", "Condenser"),
                 ("delta_t", "Pot − condenser ΔT")]


def _clean_json(o):
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, dict):
        return {k: _clean_json(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean_json(v) for v in o]
    return o


def ok(o):
    return Response(json.dumps(_clean_json(o), default=str, allow_nan=False), mimetype="application/json")


def create_app(cfg: Config, svc: Service | None = None) -> Flask:
    app = Flask(__name__)
    svc = svc or Service(cfg)
    app.config["svc"] = svc

    @app.errorhandler(Exception)
    def on_error(e):
        traceback.print_exc()
        code = getattr(e, "code", 500)
        return jsonify({"error": f"{type(e).__name__}: {e}"}), code if isinstance(code, int) else 500

    @app.get("/")
    def index():
        return render_template("index.html", demo=svc.demo)

    # --- API -----------------------------------------------------------------
    @app.get("/api/meta")
    def meta():
        cols = []
        for code in svc.column_codes():
            c = svc.column(code)
            cols.append({"code": code, "label": c.label, "has_process_data": c.has_process_data,
                         "signals": [{"tag": s.tag, "name": s.name, "label": s.label, "role": s.role,
                                      "unit": s.unit, "lo": s.lo, "hi": s.hi} for s in c.signals],
                         "runs": len({f["distillation"] for f in svc.store.fractions(code)})})
        return ok({"columns": cols, "demo": svc.demo, "eflow": svc.store.last_refresh(),
                   "eflow_configured": bool(cfg.eflow.get("url")), "compare_roles": ROLES_COMPARE,
                   "max_gap_s": cfg.max_gap_s, "timezone": cfg.app.get("timezone_note")})

    @app.get("/api/db")
    def db_status():
        good, msg = svc.db.ping()
        return ok({"ok": good, "message": msg, "backend": svc.db.backend})

    @app.get("/api/runs")
    def runs():
        return ok({"column": request.args["col"], "runs": svc.runs(request.args["col"])})

    @app.get("/api/run")
    def run():
        return ok(svc.run_detail(request.args["col"], request.args["dist"],
                                 recompute=request.args.get("recompute") == "1"))

    @app.get("/api/fraction/<int:fid>")
    def fraction(fid):
        d = svc.fraction_detail(fid)
        return ok(d) if d else (jsonify({"error": "no such fraction"}), 404)

    @app.get("/api/fraction-labels")
    def fraction_labels():
        return ok({"labels": svc.fraction_labels(request.args["col"])})

    @app.get("/api/compare")
    def compare():
        runs_arg = request.args.get("runs")
        return ok(svc.compare(request.args["col"], request.args["frac"], request.args.get("role", "pot_temp"),
                              runs_arg.split(",") if runs_arg else None))

    @app.get("/api/coverage")
    def coverage():
        return ok(svc.coverage(request.args["col"]))

    @app.post("/api/coverage/scan")
    def coverage_scan():
        full = request.args.get("full") == "1"
        since = datetime(2000, 1, 1) if full else None
        return ok(svc.scan_coverage(request.args["col"], since))

    @app.post("/api/eflow/refresh")
    def eflow_refresh():
        try:
            return ok(eflow.refresh(cfg, svc.store))
        except eflow.EflowError as e:
            return jsonify({"error": str(e)}), 400

    # --- export ------------------------------------------------------------------
    @app.get("/export/<kind>.<fmt>")
    def export(kind, fmt):
        if fmt not in ("csv", "xlsx"):
            return jsonify({"error": "format must be csv or xlsx"}), 400
        sheets, name = _export_frames(svc, kind, request.args)
        return _send(sheets, name, fmt)

    return app


def _metrics_rows(col, dist, fractions):
    rows = []
    for f in fractions:
        base = {"column": col, "distillation": dist, "fraction": f["fraction"], "lot": f["lot"], "kg": f["kg"],
                "start": f["start"], "end": f["end"], "duration_h": f["duration_h"]}
        base.update(f.get("metrics") or {})
        rows.append(base)
    return pd.DataFrame(rows)


def _export_frames(svc: Service, kind: str, args) -> tuple[dict[str, pd.DataFrame], str]:
    col = args.get("col", "")
    if kind == "runs":
        return {"runs": pd.DataFrame(svc.runs(col))}, f"{col}_runs"
    if kind == "run":
        d = svc.run_detail(col, args["dist"])
        return {"fractions": _metrics_rows(col, d["distillation"], d["fractions"])}, f"{col}_{d['distillation']}_fractions"
    if kind == "fraction":
        fid = int(args["id"])
        d = svc.fraction_detail(fid)
        fr, wide = svc.fraction_frame(fid)
        stats = pd.DataFrame(d.get("stats", []))
        metrics = pd.DataFrame([{"lot": fr["lot"], **(d.get("metrics") or {})}])
        name = f"{fr['col']}_{fr['distillation']}_F{fr['fraction']}"
        return {"data": wide, "stats": stats, "metrics": metrics}, name
    if kind == "compare":
        runs = args.get("runs")
        c = svc.compare(col, args["frac"], args.get("role", "pot_temp"), runs.split(",") if runs else None)
        prof = pd.DataFrame({"percent_complete": c["x"]})
        for r in c["runs"]:
            prof[f"{r['distillation']} ({r['lot']})"] = r["y"]
        met = pd.DataFrame([{"distillation": r["distillation"], "lot": r["lot"], "kg": r["kg"], "start": r["start"],
                             "end": r["end"], "duration_h": r["duration_h"], **(r["metrics"] or {})} for r in c["runs"]])
        return {"profiles": prof, "metrics": met}, f"{col}_F{args['frac']}_{c['role']}_compare"
    if kind == "coverage":
        c = svc.coverage(col)
        return {"hours": pd.DataFrame(c["hours"]), "gaps": pd.DataFrame(c["gaps"])}, f"{col}_coverage"
    raise ValueError(f"unknown export {kind!r}")


def _send(sheets: dict[str, pd.DataFrame], name: str, fmt: str) -> Response:
    if fmt == "csv":
        # CSV holds one table: the first sheet, which is always the main one.
        first = next(iter(sheets.values()))
        body = first.to_csv(index=False).encode("utf-8-sig")  # BOM so Excel reads the units right
        return Response(body, mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for sheet, df in sheets.items():
            df.to_excel(xw, sheet_name=sheet[:31], index=False)
    return Response(buf.getvalue(),
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})
