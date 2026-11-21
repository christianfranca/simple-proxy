"""Read-only access to the FactoryTalk datalog (SQL Server) or the demo copy.

Two promises this module keeps:

1. **Read-only.** Every statement goes through `_guard`, which refuses anything
   that is not a single SELECT. On SQL Server the session is also opened with
   ApplicationIntent=ReadOnly and pyodbc's readonly flag, and autocommit means
   there is never an open transaction to commit. The demo SQLite file is
   opened in mode=ro. If you want a third layer, have IT give the Windows
   account db_datareader only on tp_datalog: that is the only layer that does
   not depend on this code being right.

2. **Always filtered.** Every query has a DateAndTime range and a TagIndex
   filter in the WHERE clause, so SQL Server never ships the 23M-row table.

Timestamps are plant local time (America/New_York) as stored. Nothing here
converts time zones; the DataFrames carry naive datetimes on purpose.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import Config, Signal

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute|grant|revoke|deny|into|backup|restore|dbcc|bulk|openrowset|sp_\w+|xp_\w+)\b",
    re.IGNORECASE,
)


class ReadOnlyViolation(RuntimeError):
    pass


def _guard(sql: str) -> str:
    """Allow exactly one SELECT statement, nothing else."""
    body = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S).strip()
    if not re.match(r"(?is)^select\b", body):
        raise ReadOnlyViolation("only SELECT statements may be sent to the datalog")
    if ";" in body.rstrip(";"):
        raise ReadOnlyViolation("multiple statements are not allowed")
    if _FORBIDDEN.search(body):
        raise ReadOnlyViolation("statement contains a write or DDL keyword")
    return sql


FETCH_COLUMNS = ["ts", "tag", "val", "backfilled"]


class ProcessDB:
    """Thin wrapper over pyodbc (SQL Server) or sqlite3 (demo)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.backend = cfg.process.get("backend", "mssql")
        self.view = cfg.process.get("view", "FloatTableAll")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.view):
            raise ValueError(f"bad view name {self.view!r}")

    # --- connection --------------------------------------------------------
    def _connect(self):
        if self.backend == "demo":
            p = self.cfg.path(self.cfg.process["demo"]["path"])
            if not p.exists():
                raise FileNotFoundError(f"demo datalog missing: {p}. Run: python run.py --make-demo")
            return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        import pyodbc  # imported lazily so the demo runs where pyodbc is absent

        m = self.cfg.process["mssql"]
        conn_str = (
            f"DRIVER={{{m['driver']}}};SERVER={m['server']};DATABASE={m['database']};{m.get('extra', '')}"
        )
        return pyodbc.connect(conn_str, autocommit=True, readonly=True, timeout=int(m.get("timeout_s", 60)))

    def _query(self, sql: str, params: list) -> list[tuple]:
        _guard(sql)
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            return [tuple(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def ping(self) -> tuple[bool, str]:
        try:
            self._query(f"SELECT 1 AS ok FROM {self.view} WHERE 1 = 0", [])
            return True, "connected"
        except Exception as e:  # noqa: BLE001 - surface any driver error to the UI
            return False, f"{type(e).__name__}: {e}"

    # --- raw samples ---------------------------------------------------------
    def fetch(self, tags: list[int], start: datetime, end: datetime) -> pd.DataFrame:
        """Samples for `tags` with start <= ts < end. Unfiltered: dropouts included.

        Plausibility filtering happens in analysis.clean(), so the raw dropout
        count can still be reported.
        """
        if not tags:
            return pd.DataFrame(columns=FETCH_COLUMNS)
        marks = ",".join("?" * len(tags))
        if self.backend == "demo":
            sql = (
                f"SELECT DateAndTime, TagIndex, Val, IsBackfilled FROM {self.view} "
                f"WHERE DateAndTime >= ? AND DateAndTime < ? AND TagIndex IN ({marks})"
            )
            params = [start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"), *tags]
        else:
            # Millitm adds the sub-second part FactoryTalk stores separately.
            # Filtering on the bare DateAndTime column keeps the index usable.
            sql = (
                f"SELECT DATEADD(millisecond, Millitm, DateAndTime) AS ts, TagIndex, Val, "
                f"CAST(IsBackfilled AS int) FROM {self.view} "
                f"WHERE DateAndTime >= ? AND DateAndTime < ? AND TagIndex IN ({marks})"
            )
            params = [start, end, *tags]
        rows = self._query(sql, params)
        df = pd.DataFrame.from_records(rows, columns=FETCH_COLUMNS)
        if df.empty:
            return pd.DataFrame(columns=FETCH_COLUMNS)
        df["ts"] = pd.to_datetime(df["ts"])
        df["tag"] = df["tag"].astype(int)
        df["val"] = pd.to_numeric(df["val"], errors="coerce").astype(float)
        df["backfilled"] = df["backfilled"].astype(bool)
        return df.sort_values(["tag", "ts"], kind="stable").reset_index(drop=True)

    # --- coverage ------------------------------------------------------------
    def coverage_hourly(self, sigs: list[Signal], start: datetime, end: datetime) -> pd.DataFrame:
        """Minutes per hour in which at least one of `sigs` logged a plausible
        value, split by live vs backfilled.

        Pass several instruments to measure "was the logger running": a single
        tag can legitimately read 0 for a day (the induction-heater pot temp
        does whenever the heater is off), which says nothing about the logger.

        Counting distinct minutes rather than rows keeps it rate independent:
        a 20 s live minute and a 10 s backfilled minute both count once.
        """
        if not sigs:
            return pd.DataFrame(columns=["hour", "backfilled", "minutes"])
        conds, cparams = [], []
        for sg in sigs:
            conds.append("(TagIndex = ? AND Val >= ? AND Val <= ?" + (" AND Val <> 0)" if sg.drop_zero else ")"))
            cparams += [sg.tag, sg.lo, sg.hi]
        tags = ",".join("?" * len(sigs))
        plausible = " OR ".join(conds)
        if self.backend == "demo":
            sql = (
                "SELECT substr(DateAndTime,1,13) AS hr, IsBackfilled, "
                "COUNT(DISTINCT substr(DateAndTime,1,16)) "
                f"FROM {self.view} WHERE TagIndex IN ({tags}) AND DateAndTime >= ? AND DateAndTime < ? "
                f"AND ({plausible}) GROUP BY hr, IsBackfilled"
            )
            params = [*(sg.tag for sg in sigs), start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"), *cparams]
        else:
            sql = (
                "SELECT DATEADD(hour, DATEDIFF(hour, 0, DateAndTime), 0) AS hr, CAST(IsBackfilled AS int), "
                "COUNT(DISTINCT DATEDIFF(minute, '2000-01-01', DateAndTime)) "
                f"FROM {self.view} WHERE TagIndex IN ({tags}) AND DateAndTime >= ? AND DateAndTime < ? "
                f"AND ({plausible}) "
                "GROUP BY DATEADD(hour, DATEDIFF(hour, 0, DateAndTime), 0), CAST(IsBackfilled AS int)"
            )
            params = [*(sg.tag for sg in sigs), start, end, *cparams]
        rows = self._query(sql, params)
        df = pd.DataFrame.from_records(rows, columns=["hour", "backfilled", "minutes"])
        if df.empty:
            return df
        df["hour"] = pd.to_datetime(df["hour"].astype(str).str.slice(0, 13), format="%Y-%m-%d %H")
        df["backfilled"] = df["backfilled"].astype(bool)
        df["minutes"] = df["minutes"].astype(int)
        return df

    def extent(self, tag: int) -> tuple[datetime | None, datetime | None]:
        rows = self._query(
            f"SELECT MIN(DateAndTime), MAX(DateAndTime) FROM {self.view} WHERE TagIndex = ? "
            "AND DateAndTime >= ? AND DateAndTime < ?",
            [tag, *self._wide_range()],
        )
        lo, hi = rows[0] if rows else (None, None)
        return (pd.to_datetime(lo) if lo else None, pd.to_datetime(hi) if hi else None)

    def _wide_range(self):
        # Still a range filter (rule: always filter by DateAndTime), just wide.
        a, b = datetime(2000, 1, 1), datetime(2100, 1, 1)
        if self.backend == "demo":
            return [a.strftime("%Y-%m-%d %H:%M:%S"), b.strftime("%Y-%m-%d %H:%M:%S")]
        return [a, b]


def demo_path_exists(cfg: Config) -> bool:
    return Path(cfg.path(cfg.process["demo"]["path"])).exists()
