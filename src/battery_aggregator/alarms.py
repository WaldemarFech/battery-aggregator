"""Cell/temperature extremes with pack ids and alarm aggregation (design 4.5, FMEA C16)."""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import ALARM_PATHS, ValidPack


@dataclass
class Extremes:
    cell_min: float | None = None
    cell_min_id: str | None = None
    cell_max: float | None = None
    cell_max_id: str | None = None
    temp_min: float | None = None
    temp_min_id: str | None = None
    temp_max: float | None = None
    temp_max_id: str | None = None


def _id(label: str, raw: str | None, prefix: str) -> str:
    if raw is None:
        return label
    r = raw.strip()
    if r[:1].upper() == prefix:
        r = r[1:]
    return f"{label}.{prefix}{r.zfill(2) if r.isdigit() else r}"


def extremes(items: list[tuple[str, ValidPack]]) -> Extremes:
    e = Extremes()
    for lbl, v in items:
        if e.cell_min is None or v.cell_min < e.cell_min:
            e.cell_min, e.cell_min_id = v.cell_min, _id(lbl, v.cell_min_id, "C")
        if e.cell_max is None or v.cell_max > e.cell_max:
            e.cell_max, e.cell_max_id = v.cell_max, _id(lbl, v.cell_max_id, "C")
        if v.temp_min is not None and (e.temp_min is None or v.temp_min < e.temp_min):
            e.temp_min, e.temp_min_id = v.temp_min, _id(lbl, v.temp_min_id, "T")
        if v.temp_max is not None and (e.temp_max is None or v.temp_max > e.temp_max):
            e.temp_max, e.temp_max_id = v.temp_max, _id(lbl, v.temp_max_id, "T")
    return e


@dataclass
class AlarmState:
    levels: dict = field(default_factory=lambda: {p: 0 for p in ALARM_PATHS})
    sources: dict = field(default_factory=dict)  # path -> pack labels raising it

    def raise_(self, path: str, level: int, source: str) -> None:
        if level <= 0:
            return
        if level > self.levels.get(path, 0):
            self.levels[path] = level
        self.sources.setdefault(path, []).append(source)


def aggregate(items: list[tuple[str, ValidPack]]) -> AlarmState:
    """Max level over packs per path (each pack alarm becomes a bank alarm)."""
    st = AlarmState()
    for lbl, v in items:
        for path, lvl in v.alarms:
            if path in st.levels:
                st.raise_(path, lvl, lbl)
    return st
