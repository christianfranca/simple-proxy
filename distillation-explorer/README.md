# Fraction explorer

A local web app for fractional distillation runs on CD01, CD02, CD03 and CDMOL.
Pick a run, see its fractions on a timeline, open one fraction to see how pot
temp, vacuum and the column temperatures moved, compare the same fraction
across runs by percent complete, and see where the datalog has holes.

Read-only toward both sources. It sends only SELECTs to `tp_datalog` and only
reads from the Eflow flow.

## Run it

```bash
pip install -r requirements.txt
```

```bash
python run.py
```

Open http://127.0.0.1:5080. The first time, click **Refresh from Eflow** to
fill the local cache, and on the Coverage view click **Scan the datalog**.

To try it without the plant network, use synthetic data:

```bash
python run.py --demo
```

Other commands:

| Command | What it does |
|---|---|
| `python run.py --refresh-eflow` | Pull the Eflow list into the cache and print what changed |
| `python run.py --check-run CD02 190` | Print one run's fraction/process join as a table, no UI. Use this to check against `fraction_stats.py` |
| `python run.py --scan-coverage` | Scan the datalog for coverage and list outages |
| `python -m pytest -q` | Tests (the web tests need `python run.py --make-demo` first) |

To keep it running on the 24/7 machine, a Task Scheduler task "At startup"
that runs `python run.py` in this folder is enough. Install `waitress` and
the app uses it instead of Flask's development server.

## Configure it

- `config.toml`: tags, labels, plausibility ranges, vacuum bands and
  metric thresholds. Every tag-to-label mapping is here and nowhere else.
- `config.local.toml` (git-ignored, copy from `config.local.example.toml`):
  the Eflow URL with its SAS signature, and any local overrides.

Restart after editing either file.

## How it is built

```
run.py                 the one command
config.toml            plant description (tags, ranges, thresholds)
explorer/
  process_db.py        read-only SQL: SELECT guard, always filtered by time and tag
  eflow.py             fetch, parse, truncation checks, change report
  store.py             local SQLite cache (fractions, metrics, coverage)
  analysis.py          cleaning, time-weighted stats, derived metrics, profiles
  service.py           joins Eflow fractions with process data
  web.py               Flask routes and CSV/Excel export
  demo.py              synthetic datalog + Eflow file for --demo
  templates/, static/  the page; uPlot (canvas) for charts
tests/                 one test per gotcha in the brief
```

Decisions worth knowing:

- **Dropouts are removed before anything is counted.** A value outside the
  configured range, or an exact 0.0 on an analog tag, is dropped. Each
  fraction reports how many were removed.
- **Statistics are time-weighted.** Each sample counts for the time until the
  next one, capped at `max_gap_s` (120 s). This makes 20 s live rows and 10 s
  backfilled rows count equally per minute, and stops one sample from
  standing in for an outage.
- **Fractions own `start <= t < end`.** A scan on a boundary lands in one
  fraction only. The Reactor row is drawn on its own lane and never treated as
  a fraction.
- **Times are never converted.** Both sources are plant local time. Charts
  print the stored wall-clock time, whatever zone the browser is in.
- **Gaps are shown.** Lines break across missing data, missing stretches are
  shaded red, and every run, fraction and comparison row shows its pot-temp
  coverage and backfilled share.
- **Pressures are never shared across columns.** Each vacuum chart carries
  its own column's unit, and comparisons stay within one column.
- **Seam for purity.** `fraction_metrics` in `data/cache.sqlite` has one row
  per fraction with the lot number. A future `fraction_quality(lot, purity…)`
  table joins on `lot`. Exports carry the lot on every row for the same
  reason.

## Assumptions about the process (check these)

These come from the brief or my reading of it, not from your plant:

1. Pot temp = `IH_TT_101` / `IH_TT_001`, condenser = `TT_107` / `TT_007` (the
   coldest). The brief marks these as not yet checked against the HMI.
2. Plausibility ranges: temperatures 1 to 300 °C, CD02 vacuum above 0 and up
   to 30, CD03 vacuum above 0 and up to 800, levels 0.01 to 100 %. Vacuum
   excludes only the exact-zero dropout, so a deep real vacuum is kept.
3. Vacuum excursion = more than max(abs band, 10 % of median) away from the
   fraction's own median. Bands are 1.0 (CD02) and 20 (CD03). These are
   guesses.
4. Steady state = pot temp stays within ±1 °C of its local median for 20
   minutes. Steepest ramp is measured over 15-minute windows.
5. ΔT start and end are means over the first and last 15 minutes (or 10 % of
   the fraction if shorter).
6. "Minutes above threshold" uses 120 °C for both columns, a placeholder.

## Still open

- The Eflow endpoint, method, auth and one real row (the brief's FILL IN
  blocks). Field names are configurable in `[eflow.fields]`, and the date
  parser accepts the documented `7/28/26 1:00 AM` plus ISO forms.
- Nothing here has run against the real `tp_datalog` yet. Run
  `python run.py --check-run CD02 190` and compare it with `fraction_stats.py`.
