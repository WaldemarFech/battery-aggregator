"""Raw pack snapshot (adapter -> core) and validation at the system boundary.

Victron conventions: current positive = charging, CCL/DCL positive amps, CVL volts.
Invalid input never propagates: every number passes `_num` (finite, not bool).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

CHARGE = "charge"
DISCHARGE = "discharge"
DIRECTIONS = (CHARGE, DISCHARGE)


class PackState(str, Enum):
    QUARANTINE = "quarantine"
    ACTIVE = "active"
    STALE = "stale"
    OFFLINE = "offline"
    GONE = "gone"
    UNTRUSTED = "untrusted"
    EXCLUDED = "excluded"


class BankMode(str, Enum):
    INIT = "init"
    NORMAL = "normal"
    DEGRADED = "degraded"
    FAILSAFE = "failsafe"
    FAULT = "fault"


# Alarm paths understood by the core (subset of /Alarms/* of a Venus battery).
ALARM_PATHS = (
    "LowVoltage", "HighVoltage", "LowCellVoltage", "HighCellVoltage", "LowSoc",
    "HighChargeCurrent", "HighDischargeCurrent", "CellImbalance", "InternalFailure",
    "HighChargeTemperature", "LowChargeTemperature", "HighTemperature", "LowTemperature",
    "BmsCable",
)


@dataclass
class PackSnapshot:
    """Values of one serialbattery service as read by the adapter (untrusted)."""

    service: str
    serial: Any
    timestamp: float                 # monotonic time of last successful read
    voltage: Any = None
    current: Any = None
    temperature: Any = None
    temp_min: Any = None
    temp_max: Any = None
    temp_min_id: Any = None
    temp_max_id: Any = None
    soc: Any = None
    installed_capacity: Any = None
    cvl: Any = None
    ccl: Any = None
    dcl: Any = None
    allow_charge: Any = None
    allow_discharge: Any = None
    charge_fet: Any = None
    discharge_fet: Any = None
    cell_min: Any = None
    cell_max: Any = None
    cell_min_id: Any = None
    cell_max_id: Any = None
    cells: Any = None
    alarms: dict = field(default_factory=dict)
    soc_calibrated: Any = None
    connected: Any = None            # /Connected of the pack service (M6); None = not exported
    missing_paths: tuple = ()


@dataclass(frozen=True)
class ValidPack:
    """Validated, typed values. Safety fields are always finite floats."""

    voltage: float
    current: float
    cvl: float
    ccl: float
    dcl: float
    cell_min: float
    cell_max: float
    temp_min: float | None
    temp_max: float | None
    soc: float | None
    capacity_ah: float | None
    allow_charge: bool | None
    allow_discharge: bool | None
    charge_fet: bool | None
    discharge_fet: bool | None
    cell_min_id: str | None
    cell_max_id: str | None
    temp_min_id: str | None
    temp_max_id: str | None
    alarms: tuple  # tuple of (path, level)
    soc_calibrated: bool
    cells: int | None

    def alarm(self, path: str) -> int:
        for p, lvl in self.alarms:
            if p == path:
                return lvl
        return 0


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def _in(x: float | None, lo: float, hi: float) -> float | None:
    return x if x is not None and lo <= x <= hi else None


def _flag(x: Any) -> bool | None:
    if isinstance(x, bool):
        return x
    v = _num(x)
    if v is None:
        return None
    if v in (0.0, 1.0):
        return v == 1.0
    return None


def _ident(x: Any) -> str | None:
    if x is None:
        return None
    if isinstance(x, (str, int)) and not isinstance(x, bool):
        s = str(x).strip()
        return s[:32] if s else None
    return None


# Values some BMS/driver combinations report instead of a real serial (M1). Such a value
# is not an identity: two packs reporting it must never be merged into one track.
_SERIAL_PLACEHOLDERS = {"", "0", "-", "none", "null", "unknown", "n/a", "na", "nan", "serial",
                        "undefined", "default", "123456789", "1234567890"}


def serial_ident(x: Any) -> str | None:
    """Pack identity from a reported serial, or None when missing/placeholder (M1)."""
    s = _ident(x)
    if s is None:
        return None
    low = s.lower()
    if low in _SERIAL_PLACEHOLDERS or set(low) <= {"0"} or set(low) <= {"f"} or set(low) <= {"x"}:
        return None
    return s


SAFETY_FIELDS = ("voltage", "current", "cvl", "ccl", "dcl", "cell_min", "cell_max")


def validate(snap: PackSnapshot, cells_expected: int, cfg) -> tuple[ValidPack | None, list[str]]:
    """Validate a snapshot. Returns (ValidPack, problems).

    Any invalid safety-relevant field -> (None, problems) -> pack UNTRUSTED.
    Invalid display fields are dropped (problems lists them as warnings).
    """
    problems: list[str] = []
    n = cells_expected
    for p in snap.missing_paths:
        problems.append(f"missing path {p}")
    cells = _num(snap.cells)
    cells_i = int(cells) if cells is not None and cells == int(cells) and 4 <= cells <= 32 else None
    if cells_i is not None and cells_i != n:
        problems.append(f"cell count {cells_i} != configured {n}")

    vals = {
        "voltage": _in(_num(snap.voltage), cfg.cell_v_min * n, cfg.cell_v_max * n),
        "current": _in(_num(snap.current), -cfg.pack_current_max_a, cfg.pack_current_max_a),
        "cvl": _in(_num(snap.cvl), 2.5 * n, 3.75 * n),
        "ccl": _in(_num(snap.ccl), 0.0, cfg.pack_limit_max_a),
        "dcl": _in(_num(snap.dcl), 0.0, cfg.pack_limit_max_a),
        "cell_min": _in(_num(snap.cell_min), cfg.cell_v_min, cfg.cell_v_max),
        "cell_max": _in(_num(snap.cell_max), cfg.cell_v_min, cfg.cell_v_max),
    }
    for k, v in vals.items():
        if v is None:
            problems.append(f"invalid {k}={getattr(snap, k)!r}")
    if vals["cell_min"] is not None and vals["cell_max"] is not None and vals["cell_min"] > vals["cell_max"]:
        problems.append("cell_min > cell_max")
        vals["cell_min"] = None
    safety_bad = any(v is None for v in vals.values()) or bool(snap.missing_paths) or (
        cells_i is not None and cells_i != n)

    t_lo, t_hi = cfg.temp_min_c, cfg.temp_max_c
    temp_min = _in(_num(snap.temp_min), t_lo, t_hi)
    temp_max = _in(_num(snap.temp_max), t_lo, t_hi)
    t_main = _in(_num(snap.temperature), t_lo, t_hi)
    if temp_min is None:
        temp_min = t_main
    if temp_max is None:
        temp_max = t_main
    if temp_min is None:
        problems.append("temperature unknown")

    soc = _in(_num(snap.soc), 0.0, 100.0)
    if snap.soc is not None and soc is None:
        problems.append(f"ignored soc={snap.soc!r}")
    cap = _in(_num(snap.installed_capacity), 1.0, 2000.0)
    if snap.installed_capacity is not None and cap is None:
        problems.append(f"ignored capacity={snap.installed_capacity!r}")

    alarms = []
    if isinstance(snap.alarms, dict):
        for path, lvl in snap.alarms.items():
            v = _num(lvl)
            if isinstance(path, str) and v is not None and v in (0.0, 1.0, 2.0):
                alarms.append((path, int(v)))
            else:
                problems.append(f"ignored alarm {path!r}={lvl!r}")

    if safety_bad:
        return None, problems
    return ValidPack(
        voltage=vals["voltage"], current=vals["current"], cvl=vals["cvl"],
        ccl=vals["ccl"], dcl=vals["dcl"], cell_min=vals["cell_min"], cell_max=vals["cell_max"],
        temp_min=temp_min, temp_max=temp_max, soc=soc, capacity_ah=cap,
        allow_charge=_flag(snap.allow_charge), allow_discharge=_flag(snap.allow_discharge),
        charge_fet=_flag(snap.charge_fet), discharge_fet=_flag(snap.discharge_fet),
        cell_min_id=_ident(snap.cell_min_id), cell_max_id=_ident(snap.cell_max_id),
        temp_min_id=_ident(snap.temp_min_id), temp_max_id=_ident(snap.temp_max_id),
        alarms=tuple(sorted(alarms)), soc_calibrated=_flag(snap.soc_calibrated) is True,
        cells=cells_i,
    ), problems


def signature(snap: PackSnapshot) -> tuple:
    """Values that jitter in normal operation; bit-identical for long = frozen driver."""
    return (repr(snap.voltage), repr(snap.current), repr(snap.cell_min), repr(snap.cell_max),
            repr(snap.soc))
