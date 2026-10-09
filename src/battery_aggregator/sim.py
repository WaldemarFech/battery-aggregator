"""Closed-loop simulator: N parallel LiFePO4 packs + JK-like protection + a Multi that
applies the published limits with a delay. Used by tests only; no Venus code involved.

Currents: positive = charging. For pack i: I_i = (V_bus - OCV_i) / R_i, sum I_i = I_bank.
Packs whose FET blocks the required direction are removed and the system is re-solved
(a pack with open charge FET can still discharge through the body diode and vice versa).
"""
from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .model import PackSnapshot

# LiFePO4 OCV per cell (SoC %, V) - coarse but with the characteristic flat plateau
OCV_TABLE = [(0, 2.80), (5, 3.05), (10, 3.17), (20, 3.24), (40, 3.27), (60, 3.30),
             (80, 3.33), (90, 3.35), (97, 3.40), (100, 3.55)]


def ocv_cell(soc: float) -> float:
    soc = max(0.0, min(100.0, soc))
    for (s0, v0), (s1, v1) in zip(OCV_TABLE, OCV_TABLE[1:]):
        if soc <= s1:
            return v0 + (v1 - v0) * (soc - s0) / (s1 - s0)
    return OCV_TABLE[-1][1]


@dataclass
class SimPack:
    label: str
    serial: str
    capacity_ah: float
    r_ohm: float = 0.015
    soc: float = 50.0
    cells: int = 16
    temp: float = 20.0
    imbalance_v: float = 0.010       # cell_max - mean (and mean - cell_min)
    ccl: Callable[["SimPack", float], float] | float = 200.0
    dcl: Callable[["SimPack", float], float] | float = 200.0
    cvl: float = 56.8
    ocp_a: float = 250.0
    ocp_delay_s: float = 1.0
    charge_fet: bool = True
    discharge_fet: bool = True
    report_fet: bool = False
    online: bool = True
    frozen: bool = False
    shunt_dead: bool = False
    soc_reported: float | None = None   # drifted counter value, None = true SoC
    alarms: dict = field(default_factory=dict)
    current: float = 0.0
    over_since: float | None = None
    tripped_until: float | None = None
    _frozen_snap: PackSnapshot | None = None
    _jit: int = 0

    def limit(self, which: str, t: float) -> float:
        f = self.ccl if which == "ccl" else self.dcl
        return f(self, t) if callable(f) else float(f)

    def ocv(self) -> float:
        return ocv_cell(self.soc) * self.cells


class Plant:
    """delay_s: inverter response (published limits act after delay_s - 1 steps).
    meas_delay_s: BMS/D-Bus measurement lag of the reported pack currents (steps).
    noise_a: Gaussian noise (sigma, A) on reported pack currents, seeded (reproducible)."""

    def __init__(self, packs: list[SimPack], delay_s: int = 2, meas_delay_s: int = 0,
                 noise_a: float = 0.0, seed: int = 1) -> None:
        self.packs = packs
        self.delay = deque(maxlen=max(1, delay_s))
        self.v_bus = sum(p.ocv() for p in packs) / len(packs)
        self.i_bank = 0.0
        self.trips: list = []
        self.meas = deque(maxlen=max(0, meas_delay_s) + 1)
        self.noise_a = noise_a
        self.rng = random.Random(seed)

    def solve(self, i_bank: float) -> None:
        live = [p for p in self.packs if p.charge_fet or p.discharge_fet]
        for _ in range(len(self.packs) + 1):
            if not live:
                for p in self.packs:
                    p.current = 0.0
                self.i_bank = 0.0
                return
            g = sum(1.0 / p.r_ohm for p in live)
            v = (i_bank + sum(p.ocv() / p.r_ohm for p in live)) / g
            bad = [p for p in live if ((v - p.ocv()) / p.r_ohm > 0 and not p.charge_fet) or
                   ((v - p.ocv()) / p.r_ohm < 0 and not p.discharge_fet)]
            if not bad:
                break
            live = [p for p in live if p not in bad]
        for p in self.packs:
            p.current = (v - p.ocv()) / p.r_ohm if p in live else 0.0
        self.v_bus = v
        self.i_bank = i_bank

    def step(self, t: float, dt: float, demand: float, published) -> None:
        """demand: requested bank current (+charge). published: BankOutputs or None."""
        self.delay.append(published)
        applied = self.delay[0]
        i = demand
        if applied is not None:
            i = min(i, applied.ccl) if i > 0 else max(i, -applied.dcl)
            if i > 0 and self.v_bus >= applied.cvl:
                i = 0.0
        self.solve(i)
        for p in self.packs:
            p.soc = max(0.0, min(100.0, p.soc + p.current * dt / 3600.0 / p.capacity_ah * 100.0))
            # JK hardware over-current protection
            if p.tripped_until is not None and t >= p.tripped_until:
                p.tripped_until = None
                p.charge_fet = p.discharge_fet = True
            if abs(p.current) > p.ocp_a:
                p.over_since = t if p.over_since is None else p.over_since
                if t - p.over_since >= p.ocp_delay_s:
                    self.trips.append((t, p.label, p.current))
                    p.charge_fet = p.discharge_fet = False
                    p.tripped_until = t + 60.0
                    p.over_since = None
            else:
                p.over_since = None
        self.meas.append({p.label: p.current for p in self.packs})

    def snapshot(self, p: SimPack, t: float) -> PackSnapshot | None:
        if not p.online:
            return None
        if p.frozen and p._frozen_snap is not None:
            s = p._frozen_snap
            s.timestamp = t  # driver still answers, but values do not change
            return s
        p._jit += 1
        jitter = 0.001 * (p._jit % 2)
        mean = self.v_bus / p.cells
        cur = self.meas[0].get(p.label, p.current) if self.meas else p.current
        if self.noise_a > 0:
            cur += self.rng.gauss(0.0, self.noise_a)
        cur = 0.0 if p.shunt_dead else cur
        s = PackSnapshot(
            service=f"com.victronenergy.battery.{p.label}", serial=p.serial, timestamp=t,
            voltage=round(self.v_bus + jitter, 3), current=round(cur, 2), temperature=p.temp,
            temp_min=p.temp, temp_max=p.temp, soc=round(p.soc if p.soc_reported is None else p.soc_reported, 1),
            installed_capacity=p.capacity_ah, cvl=p.cvl, ccl=p.limit("ccl", t), dcl=p.limit("dcl", t),
            allow_charge=1, allow_discharge=1,
            charge_fet=(int(p.charge_fet) if p.report_fet else None),
            discharge_fet=(int(p.discharge_fet) if p.report_fet else None),
            cell_min=round(mean - p.imbalance_v, 3), cell_max=round(mean + p.imbalance_v + jitter, 3),
            cell_min_id=1, cell_max_id=7, cells=p.cells, alarms=dict(p.alarms))
        if p.frozen:
            p._frozen_snap = s
        return s

    def snapshots(self, t: float) -> list[PackSnapshot]:
        return [s for s in (self.snapshot(p, t) for p in self.packs) if s is not None]


def run(core, plant: Plant, seconds: int, demand: Callable[[float], float], t0: float = 0.0,
        hook: Callable | None = None, wall0: float = 1.8e9) -> list:
    """Closed loop for `seconds` steps of 1 s; returns [(t, outputs, pack currents)]."""
    hist = []
    out = None
    for k in range(seconds):
        t = t0 + k
        if hook:
            hook(t, plant)
        out = core.step(plant.snapshots(t), t, wall0 + t)
        plant.step(t, 1.0, demand(t), out)
        hist.append((t, out, {p.label: p.current for p in plant.packs}))
    return hist
