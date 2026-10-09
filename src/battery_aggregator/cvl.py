"""Charge voltage limit (design 4.3, FMEA SR-06/E4/C17; review M2/M3/M4).

CVL = min( min CVL_i over packs with closed charge path (+ held values of blind packs),
           cell regulator, static caps of blind packs, configured max, calibration cap ).

M4: the configured max is 3.55 V/cell x cells and can only be lowered (config.validate).

Cell regulator (M3): above v_cell_target the bus voltage is pulled down,
    u = V_bus - g * (Vcell_max - target),
which acts as an integrator because the chargers make V_bus follow the published CVL.
Anti-windup: u is only updated while the bank actually charges (I_bank >= cvl_reg_i_min_a);
without charge current lowering the CVL has no effect, so u is held instead of chasing a
relaxing bus voltage down. Floor: u >= cells x cvl_reg_floor_v_per_cell. Below target the
regulator releases (the published value then rises only through the slow CVL ramp).
Only LIVE cell data drives the regulator (M2).

Blind/stale packs (M2): their held CVL is used, and if their last live top cell was above
target a STATIC cap from that last snapshot (V_last - g * e_last, floored) is applied. It
does not integrate against the current bus voltage (no windup from stale data) and it ends
when the pack is GONE; the published CVL then rises only via the ramp.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .alarms import _id

INF = float("inf")


@dataclass
class CvlInput:
    label: str
    cvl: float
    cell_max: float | None   # None for held/blind packs (no live cell data)
    cell_max_id: str | None = None
    static_cap: float | None = None   # M2: frozen cell cap of a blind pack


@dataclass
class CvlResult:
    value: float
    reason: str
    follow: float
    cell: float


def reg_floor(cfg) -> float:
    return cfg.cells * max(cfg.cvl_min_v_per_cell, cfg.cvl_reg_floor_v_per_cell)


def static_cell_cap(voltage: float, cell_max: float | None, cfg) -> float | None:
    """Cap derived once from a pack's last live values (M2)."""
    if cell_max is None or cell_max <= cfg.v_cell_target:
        return None
    return max(reg_floor(cfg), voltage - cfg.cell_reg_gain * (cell_max - cfg.v_cell_target))


class CellRegulator:
    """Cell-voltage CVL regulator with floor and conditional-integration anti-windup (M3)."""

    def __init__(self) -> None:
        self.u: float | None = None

    def step(self, top_cell: float | None, v_bus: float | None, i_bank: float, cfg) -> float:
        if top_cell is None:
            return self.u if self.u is not None else INF   # no live cells: hold
        e = top_cell - cfg.v_cell_target
        if e <= 0:
            self.u = None
            return INF
        floor = reg_floor(cfg)
        if v_bus is None:
            return self.u if self.u is not None else INF
        cand = max(floor, v_bus - cfg.cell_reg_gain * e)
        if self.u is None:
            self.u = cand                         # engage: proportional jump, as designed
        elif i_bank >= cfg.cvl_reg_i_min_a:
            self.u = cand                         # integrating (bus follows the CVL)
        # else: no charge current -> lowering has no effect -> hold (anti-windup)
        self.u = min(max(self.u, floor), cfg.cvl_max_v())
        return self.u


def compute_cvl(inputs: list[CvlInput], v_bus: float | None, cfg, calibration_cap: float | None,
                regulator: CellRegulator | None = None, i_bank: float = 0.0) -> CvlResult:
    n = cfg.cells
    cfg_max = cfg.cvl_max_v()
    best, reason = cfg_max, f"configured max {cfg_max:.2f}V"
    follow = INF
    for x in inputs:
        if x.cvl < follow:
            follow = x.cvl
            if x.cvl < best:
                best, reason = x.cvl, f"CVL of {x.label} {x.cvl:.2f}V"
    for x in inputs:
        if x.static_cap is not None and x.static_cap < best:
            best, reason = x.static_cap, f"held cell cap of blind {x.label} {x.static_cap:.2f}V"
    cell = INF
    cells_live = [x for x in inputs if x.cell_max is not None]
    top = max(cells_live, key=lambda x: x.cell_max) if cells_live else None
    if regulator is None:
        regulator = CellRegulator()
    cell = regulator.step(top.cell_max if top else None, v_bus, i_bank, cfg)
    if cell < best:
        who = _id(top.label, top.cell_max_id, 'C') + f" {top.cell_max:.3f}V" if top else "(held)"
        best, reason = cell, f"cell controller {who}"
    if calibration_cap is not None and calibration_cap < best:
        best, reason = calibration_cap, f"calibration charge {calibration_cap:.2f}V"
    if not math.isfinite(best):
        best, reason = n * cfg.failsafe_cvl_v_per_cell, "no CVL source"
    return CvlResult(best, reason, follow, cell)
