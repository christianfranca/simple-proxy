"""Pull the Eflow fraction list from the Power Automate flow, normalise it,
check it is complete, and swap it into the local cache with a change report.

The app never reads Eflow per request: pages are served from the SQLite cache,
and only the "Refresh from Eflow" button (or `python run.py --refresh-eflow`)
calls the flow. Read-only towards Eflow: a GET, or a POST that only carries
the query body the flow expects.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from .config import Config
from .store import Store

# Row counts that usually mean a paging limit cut the list short, not that the
# list really is that long: SharePoint Get items defaults (100), its paging
# threshold (5000), and common page sizes.
TRUNCATION_TELLS = {100, 256, 500, 512, 1000, 2000, 2048, 5000}

DATE_FORMATS = [
    "%m/%d/%y %I:%M %p",      # 7/28/26 1:00 AM  (the documented format)
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%y %I:%M:%S %p",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%y %H:%M",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
]


class EflowError(RuntimeError):
    pass


def parse_date(s, warnings: list[str]) -> datetime | None:
    """Parse an Eflow date into a naive plant-local datetime.

    The brief says Eflow dates are plant local (EST/EDT) like the datalog. If a
    value arrives with a UTC 'Z' or an offset, we convert it to
    America/New_York explicitly and say so, rather than guess.
    """
    if s is None or s == "":
        return None
    s = str(s).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise EflowError(f"unrecognised date {s!r}") from None
    if dt.tzinfo is not None:
        from zoneinfo import ZoneInfo  # needs the tzdata package on Windows
        dt = dt.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None)
        msg = "NOTE: Eflow dates arrive in UTC and were converted to plant time (America/New_York)."
        if msg not in warnings:
            warnings.append(msg)
    return dt


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def normalise(rows: list[dict], fields: dict) -> tuple[list[dict], list[str]]:
    out, warnings, bad = [], [], 0
    f = fields
    for r in rows:
        try:
            start = parse_date(r.get(f["start"]), warnings)
            end = parse_date(r.get(f["end"]), warnings)
        except EflowError as e:
            bad += 1
            if bad <= 5:
                warnings.append(f"Row skipped: {e}")
            continue
        col = str(r.get(f["column"]) or "").strip().upper()
        dist = r.get(f["distillation"])
        dist = "" if dist is None else str(dist).strip()
        if dist.endswith(".0"):
            dist = dist[:-2]
        frac = r.get(f["fraction"])
        out.append({
            "col": col,
            "distillation": dist,
            "fraction": "" if frac is None else str(frac).strip(),   # never cast: 'Reactor'
            "lot": (str(r.get(f["lot"])).strip() if r.get(f["lot"]) is not None else None),
            "kg": _num(r.get(f["kg"])),
            "start": start.isoformat(sep=" ") if start else None,
            "end": end.isoformat(sep=" ") if end else None,
            "run_id": None if r.get(f["run_id"]) is None else str(r.get(f["run_id"])),
            "raw": json.dumps(r, default=str, sort_keys=True),
        })
    if bad > 5:
        warnings.append(f"{bad} rows skipped in total for unreadable dates.")
    n_missing = sum(1 for r in out if not r["start"] or not r["end"])
    if n_missing:
        warnings.append(f"{n_missing} rows have no start or end date; they show in lists but not on timelines.")
    n_inv = sum(1 for r in out if r["start"] and r["end"] and r["end"] <= r["start"])
    if n_inv:
        warnings.append(f"{n_inv} rows end at or before they start.")
    return out, warnings


def fetch_raw(cfg: Config) -> dict:
    e = cfg.eflow
    url = e.get("url", "")
    if not url:
        raise EflowError("No Eflow URL configured. Put it in config.local.toml under [eflow] url = \"...\".")
    if url.startswith("file:") or Path(url).suffix == ".json":
        p = Path(url.removeprefix("file:"))
        p = p if p.is_absolute() else cfg.path(str(p))
        return json.loads(p.read_text(encoding="utf-8"))
    method = e.get("method", "GET").upper()
    rows_key = e.get("rows_key", "eflowData")
    body = e.get("body") or {}
    # A body that mentions {column} means one call per column (the real flow
    # takes {"columnId": "CD02", "action": "get"}); otherwise a single call.
    per_column = "{column}" in json.dumps(body)
    columns = e.get("columns") or [None] if per_column else [None]
    merged, counts = [], {}
    for col in columns:
        payload = _call(url, method, _fill(body, col), e)
        if not isinstance(payload, dict):
            raise EflowError(f"Eflow response for {col or 'the list'} is not a JSON object")
        if payload.get("success") is False:
            raise EflowError(f"Eflow flow reported success: false for {col or 'the list'}")
        rows = payload.get(rows_key)
        if not isinstance(rows, list):
            raise EflowError(f"Eflow response for {col or 'the list'} has no list under {rows_key!r}")
        merged += rows
        if col:
            counts[col] = len(rows)
    out = {rows_key: merged, "success": True}
    if counts:
        out["_per_column"] = counts
    return out


def _fill(body, col):
    """Replace "{column}" in any string of the body with the column code."""
    if col is None:
        return body
    if isinstance(body, dict):
        return {k: _fill(v, col) for k, v in body.items()}
    if isinstance(body, list):
        return [_fill(v, col) for v in body]
    if isinstance(body, str):
        return body.replace("{column}", col)
    return body


def _call(url: str, method: str, body, e: dict) -> dict:
    data = json.dumps(body).encode() if method == "POST" else None
    headers = {"Accept": "application/json", **(e.get("headers") or {})}
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=float(e.get("timeout_s", 120))) as resp:
            return json.loads(resp.read().decode("utf-8-sig"))
    except urllib.error.HTTPError as err:
        detail = err.read()[:400].decode("utf-8", "replace")
        raise EflowError(f"Eflow flow answered HTTP {err.code}: {detail}") from None


def looks_truncated(n: int) -> bool:
    """A count that smells like a paging or Top Count limit rather than the
    real length of the list: the classic values, or any round hundred."""
    return n in TRUNCATION_TELLS or (n >= 100 and n % 100 == 0)


def _key(r):
    return (r["col"], r["distillation"], r["fraction"], r["lot"] or "")


def refresh(cfg: Config, store: Store, payload: dict | None = None) -> dict:
    """Fetch (or take `payload`), validate, diff against the cache, replace it.

    Returns a report dict that the UI shows verbatim.
    """
    e = cfg.eflow
    payload = payload if payload is not None else fetch_raw(cfg)
    if not isinstance(payload, dict):
        raise EflowError("Eflow response is not a JSON object")
    if payload.get("success") is False:
        raise EflowError("Eflow flow reported success: false")
    rows = payload.get(e.get("rows_key", "eflowData"))
    if not isinstance(rows, list):
        raise EflowError(f"Eflow response has no list under {e.get('rows_key', 'eflowData')!r}")

    norm, warnings = normalise(rows, e["fields"])
    notes = [w for w in warnings if w.startswith("NOTE: ")]
    warnings = [w for w in warnings if not w.startswith("NOTE: ")]
    n = len(rows)

    # --- completeness checks -------------------------------------------------
    count_key = e.get("count_key")
    if count_key and count_key in payload:
        reported = int(payload[count_key])
        if reported != n:
            warnings.insert(0, f"INCOMPLETE: the flow says {reported} rows but sent {n}.")
    per_col = payload.get("_per_column") or {}
    capped = [f"{c} {k}" for c, k in per_col.items() if looks_truncated(k)]
    if capped:
        warnings.insert(0, "SUSPICIOUS ROW COUNTS: " + ", ".join(capped) + " rows. A round number like this "
                           "is almost always a Top Count or paging limit in the flow's Get items action, so the "
                           "oldest runs are probably missing. Raise Top Count (max 5000) or turn pagination on.")
    elif looks_truncated(n):
        warnings.insert(0, f"SUSPICIOUS ROW COUNT: exactly {n} rows looks like a paging limit. "
                           "Check pagination is on in the flow's Get items action.")
    prev = store.last_refresh()
    if prev and n < prev.get("rows", 0):
        warnings.insert(0, f"ROW COUNT DROPPED from {prev['rows']} to {n}. The list should only grow; "
                           "check the flow before trusting this refresh.")
    dup = {}
    for r in norm:
        dup[_key(r)] = dup.get(_key(r), 0) + 1
    dups = [k for k, c in dup.items() if c > 1]
    if dups:
        warnings.append(f"{len(dups)} duplicate (column, distillation, fraction, lot) keys in Eflow, e.g. {dups[0]}.")

    # --- diff -------------------------------------------------------------------
    old = {_key(r): r for r in store.all_fractions()}
    new = {_key(r): r for r in norm}
    cmp = ("kg", "start", "end", "run_id")
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = []
    for k in sorted(set(new) & set(old)):
        diffs = {c: [old[k][c], new[k][c]] for c in cmp if old[k][c] != new[k][c]}
        if diffs:
            changed.append({"key": k, "diff": diffs})

    store.replace_fractions(norm)
    # A fraction whose dates moved has stale cached metrics; the cache key
    # includes start and end, so stale rows are simply never looked up again.

    report = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "rows": n,
        "columns": sorted({r["col"] for r in norm}),
        "added": [list(k) for k in added],
        "removed": [list(k) for k in removed],
        "changed": changed,
        "warnings": warnings,
        "notes": [n.removeprefix("NOTE: ") for n in notes],
        "per_column": payload.get("_per_column") or {},
    }
    store.log_refresh(report)
    return report
