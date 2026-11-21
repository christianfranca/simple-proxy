"""Local SQLite cache: Eflow fractions, refresh history, computed metrics and
coverage. This is the app's own file; nothing here ever touches tp_datalog.

The `fraction_metrics` table is the seam for purity data later: its natural
key is the lot number, so a future `fraction_quality(lot, purity, ...)` table
joins with a plain LEFT JOIN on lot and no existing table changes.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS fractions (
    id INTEGER PRIMARY KEY,
    col TEXT NOT NULL,
    distillation TEXT NOT NULL,
    fraction TEXT NOT NULL,          -- string on purpose: 'Reactor' exists
    lot TEXT,
    kg REAL,
    start TEXT,                      -- plant local time, ISO, no zone
    "end" TEXT,
    run_id TEXT,
    raw TEXT                         -- the original Eflow row, verbatim JSON
);
CREATE INDEX IF NOT EXISTS ix_fr_run ON fractions(col, distillation);
CREATE INDEX IF NOT EXISTS ix_fr_frac ON fractions(col, fraction);

CREATE TABLE IF NOT EXISTS refresh_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    rows INTEGER,
    added INTEGER, removed INTEGER, changed INTEGER,
    report TEXT                      -- JSON: details, warnings
);

CREATE TABLE IF NOT EXISTS fraction_metrics (
    col TEXT NOT NULL,
    distillation TEXT NOT NULL,
    fraction TEXT NOT NULL,
    lot TEXT,
    start TEXT NOT NULL,
    "end" TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    metrics TEXT NOT NULL,           -- JSON dict from analysis.fraction_metrics
    PRIMARY KEY (col, distillation, fraction, start, "end")
);
CREATE INDEX IF NOT EXISTS ix_fm_lot ON fraction_metrics(lot);

CREATE TABLE IF NOT EXISTS coverage_hourly (
    col TEXT NOT NULL,
    hour TEXT NOT NULL,
    live_min INTEGER NOT NULL,
    backfilled_min INTEGER NOT NULL,
    PRIMARY KEY (col, hour)
);
CREATE TABLE IF NOT EXISTS coverage_meta (
    col TEXT PRIMARY KEY,
    scanned_from TEXT, scanned_to TEXT, computed_at TEXT
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        with self._lock:
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            try:
                yield c
                c.commit()
            finally:
                c.close()

    # --- fractions -----------------------------------------------------------
    def replace_fractions(self, rows: list[dict]) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM fractions")
            c.executemany(
                'INSERT INTO fractions (col, distillation, fraction, lot, kg, start, "end", run_id, raw) '
                "VALUES (:col, :distillation, :fraction, :lot, :kg, :start, :end, :run_id, :raw)",
                rows,
            )

    def all_fractions(self) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM fractions")]

    def fractions(self, col: str, distillation: str | None = None) -> list[dict]:
        q = "SELECT * FROM fractions WHERE col = ?"
        args: list = [col]
        if distillation is not None:
            q += " AND distillation = ?"
            args.append(distillation)
        with self.conn() as c:
            rows = [dict(r) for r in c.execute(q + " ORDER BY start, fraction", args)]
        return rows

    def fraction_by_id(self, fid: int) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM fractions WHERE id = ?", [fid]).fetchone()
            return dict(r) if r else None

    def log_refresh(self, report: dict) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO refresh_log (at, rows, added, removed, changed, report) VALUES (?,?,?,?,?,?)",
                [report["at"], report["rows"], len(report["added"]), len(report["removed"]),
                 len(report["changed"]), json.dumps(report, default=str)],
            )

    def last_refresh(self) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT report FROM refresh_log ORDER BY id DESC LIMIT 1").fetchone()
            return json.loads(r["report"]) if r else None

    # --- metrics -------------------------------------------------------------
    def get_metrics(self, col, distillation, fraction, start, end) -> dict | None:
        with self.conn() as c:
            r = c.execute(
                'SELECT metrics FROM fraction_metrics WHERE col=? AND distillation=? AND fraction=? AND start=? AND "end"=?',
                [col, distillation, fraction, start, end],
            ).fetchone()
            return json.loads(r["metrics"]) if r else None

    def put_metrics(self, col, distillation, fraction, lot, start, end, metrics: dict) -> None:
        with self.conn() as c:
            c.execute(
                'INSERT OR REPLACE INTO fraction_metrics (col, distillation, fraction, lot, start, "end", computed_at, metrics) '
                "VALUES (?,?,?,?,?,?,?,?)",
                [col, distillation, fraction, lot, start, end, datetime.now().isoformat(timespec="seconds"),
                 json.dumps(metrics)],
            )

    def clear_metrics(self, col: str | None = None) -> None:
        with self.conn() as c:
            if col:
                c.execute("DELETE FROM fraction_metrics WHERE col = ?", [col])
            else:
                c.execute("DELETE FROM fraction_metrics")

    # --- coverage --------------------------------------------------------------
    def put_coverage(self, col: str, rows: list[tuple[str, int, int]], scanned_from: str, scanned_to: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM coverage_hourly WHERE col = ? AND hour >= ? AND hour < ?",
                      [col, scanned_from, scanned_to])
            c.executemany("INSERT OR REPLACE INTO coverage_hourly VALUES (?,?,?,?)",
                          [(col, h, a, b) for h, a, b in rows])
            prev = c.execute("SELECT scanned_from, scanned_to FROM coverage_meta WHERE col = ?", [col]).fetchone()
            lo, hi = scanned_from, scanned_to
            if prev:
                lo, hi = min(lo, prev["scanned_from"]), max(hi, prev["scanned_to"])
            c.execute("INSERT OR REPLACE INTO coverage_meta VALUES (?,?,?,?)",
                      [col, lo, hi, datetime.now().isoformat(timespec="seconds")])

    def clear_coverage(self, col: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM coverage_hourly WHERE col = ?", [col])
            c.execute("DELETE FROM coverage_meta WHERE col = ?", [col])

    def coverage(self, col: str) -> tuple[list[dict], dict | None]:
        with self.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT hour, live_min, backfilled_min FROM coverage_hourly WHERE col = ? ORDER BY hour", [col])]
            meta = c.execute("SELECT * FROM coverage_meta WHERE col = ?", [col]).fetchone()
            return rows, (dict(meta) if meta else None)
