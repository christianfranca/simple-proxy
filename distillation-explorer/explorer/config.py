"""Load config.toml, overlay config.local.toml, and expose typed accessors.

Everything that describes the plant (tag numbers, labels, plausibility ranges,
thresholds) comes from the TOML file so it can change without touching code.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Signal:
    tag: int
    name: str
    label: str
    role: str
    unit: str
    lo: float
    hi: float
    drop_zero: bool = True


@dataclass(frozen=True)
class Column:
    code: str
    label: str
    signals: tuple[Signal, ...]
    vacuum_band_abs: float = 0.0
    vacuum_band_rel: float = 0.10
    pot_temp_threshold: float | None = None

    @property
    def has_process_data(self) -> bool:
        return bool(self.signals)

    def by_role(self, role: str) -> Signal | None:
        return next((s for s in self.signals if s.role == role), None)

    def by_tag(self, tag: int) -> Signal | None:
        return next((s for s in self.signals if s.tag == tag), None)


@dataclass
class Config:
    raw: dict
    columns: dict[str, Column] = field(default_factory=dict)

    # --- convenience accessors -------------------------------------------
    @property
    def app(self) -> dict:
        return self.raw.get("app", {})

    @property
    def process(self) -> dict:
        return self.raw.get("process", {})

    @property
    def metrics(self) -> dict:
        return self.raw.get("metrics", {})

    @property
    def eflow(self) -> dict:
        return self.raw.get("eflow", {})

    @property
    def max_gap_s(self) -> float:
        return float(self.process.get("gaps", {}).get("max_gap_s", 120))

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: Path | None = None, overrides: dict | None = None) -> Config:
    path = path or ROOT / "config.toml"
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    local = path.with_name("config.local.toml")
    if local.exists():
        with open(local, "rb") as f:
            raw = _deep_merge(raw, tomllib.load(f))
    if overrides:
        raw = _deep_merge(raw, overrides)

    cols: dict[str, Column] = {}
    for code, c in raw.get("columns", {}).items():
        sigs = tuple(
            Signal(
                tag=int(s["tag"]), name=s["name"], label=s["label"], role=s["role"],
                unit=s.get("unit", ""), lo=float(s["lo"]), hi=float(s["hi"]),
                drop_zero=bool(s.get("drop_zero", True)),
            )
            for s in c.get("signals", [])
        )
        cols[code] = Column(
            code=code, label=c.get("label", code), signals=sigs,
            vacuum_band_abs=float(c.get("vacuum_band_abs", 0.0)),
            vacuum_band_rel=float(c.get("vacuum_band_rel", 0.10)),
            pot_temp_threshold=c.get("pot_temp_threshold"),
        )
    return Config(raw=raw, columns=cols)
