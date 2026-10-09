"""limit_control = "sum" (owner rule 2026-10-08): the bank limit is the SUM of the live pack limits.

Single source of truth are the packs themselves: CCL_i/DCL_i as dbus-serialbattery reads them
from the JK-BMS (it already reduces them for cell voltage / temperature / SoC). The aggregator
adds no own tapers, no share model, no N-1 reserve, no smoothing window. Only packs that are
ACTIVE (live data) count; a stale/offline/quarantined pack drops out of the sum.

Pack values are taken over unchanged (no own voltage/temperature/SoC thresholds; last instance
is the BMS). Supervision only: a gentle loop (ReactiveCut) lowers the bank limit step by step
while a pack really carries more than its reported limit and raises it again in steps; without
overload the full sum applies immediately.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .model import CHARGE, DIRECTIONS, DISCHARGE, ValidPack


def source_limit(v: ValidPack, direction: str, cfg, hv_latched: bool) -> tuple[float, str]:
    """Pack limit exactly as serialbattery reports it (owner 2026-10-08: the aggregator decides
    nothing about voltages/temperatures/SoC). Allow flag 0 from the source -> the pack adds 0."""
    if direction == CHARGE:
        if v.allow_charge is False:
            return 0.0, "AllowToCharge=0"
        return v.ccl, f"BMS CCL {v.ccl:.1f}A"
    if v.allow_discharge is False:
        return 0.0, "AllowToDischarge=0"
    return v.dcl, f"BMS DCL {v.dcl:.1f}A"


def sum_reason(members) -> str:
    """'sum 3 packs: P1 200A, P2 50A (BMS CCL 50.0A), P3 200A'. The lowest pack carries its why."""
    if not members:
        return "sum 0 packs: no pack can take current"
    low = min(members, key=lambda m: m.limit)
    parts = []
    for m in members:
        txt = f"{m.label} {m.limit:.0f}A"
        if m is low and low.limit < max(x.limit for x in members):
            txt += f" ({m.why})"
        parts.append(txt)
    return f"sum {len(members)} packs: " + ", ".join(parts)


@dataclass
class ReactiveCut:
    """Gentle per-direction overload loop. cut is None while no pack is overloaded (= full sum).

    Pack i over its limit (flow > L*(1+tol)+abs_tol for trip_s): one step down to
    I_bank * L_i/I_i (e.g. 120/100 A at 300 A -> 250 A), then wait down_interval_s and look
    again. All packs <= release_ratio * L for release_s: raise by up_step_a every
    up_interval_s (250 -> 270 -> ...) until the sum is reached again.
    """

    tol: float = 0.02
    abs_tol_a: float = 1.0
    trip_s: float = 2.0
    down_interval_s: float = 3.0
    release_ratio: float = 0.98
    release_s: float = 2.0
    up_step_a: float = 20.0
    up_interval_s: float = 5.0

    def __post_init__(self) -> None:
        self.cut: float | None = None
        self.over_since: float | None = None
        self.ok_since: float | None = None
        self.last_down: float | None = None
        self.last_up: float | None = None
        self.worst: tuple[str, float, float] | None = None   # (label, flow, limit)

    @classmethod
    def from_cfg(cls, cfg) -> "ReactiveCut":
        return cls(cfg.react_tol, cfg.react_abs_tol_a, cfg.react_trip_s, cfg.react_down_interval_s,
                   cfg.react_release_ratio, cfg.react_release_s, cfg.react_up_step_a,
                   cfg.react_up_interval_s)

    def force(self, value: float) -> None:
        if self.cut is not None and value < self.cut:
            self.cut = value

    def update(self, now: float, flows: list[tuple[str, float, float]], bank_flow: float,
               total: float, events: list | None = None, direction: str = "") -> float:
        """flows: (label, measured flow in this direction (>0), pack limit). Returns the limit."""
        over = [(lbl, f, lim) for lbl, f, lim in flows if f > lim * (1.0 + self.tol) + self.abs_tol_a]
        if over:
            self.ok_since = None
            if self.over_since is None:
                self.over_since = now
            due = self.last_down is None or now - self.last_down >= self.down_interval_s
            if now - self.over_since >= self.trip_s and due:
                lbl, f, lim = min(over, key=lambda x: x[2] / x[1])
                base = min(bank_flow, self.cut if self.cut is not None else total)
                target = max(0.0, base * lim / f) if f > 0 else 0.0
                if self.cut is None or target < self.cut:
                    self.cut = target
                    self.last_down = now
                    self.worst = (lbl, f, lim)
                    if events is not None:
                        events.append(("warn", lbl, f"{direction} overload {f:.0f}/{lim:.0f}A -> "
                                                    f"bank {target:.0f}A"))
        else:
            self.over_since = None
            if self.cut is not None:
                if all(f <= lim * self.release_ratio for _, f, lim in flows):
                    if self.ok_since is None:
                        self.ok_since = now
                    if now - self.ok_since >= self.release_s and (
                            self.last_up is None or now - self.last_up >= self.up_interval_s):
                        self.cut += self.up_step_a
                        self.last_up = now
                else:
                    self.ok_since = None
        if self.cut is not None and (self.cut >= total or not math.isfinite(self.cut)):
            self.cut, self.worst, self.ok_since, self.last_down, self.last_up = None, None, None, None, None
        return total if self.cut is None else min(total, self.cut)


def sum_apply(core, tgt, why, results, now, events) -> tuple[float, float]:
    """Sum mode output stage: no ramp, no PI. Reactive cut only on a measured pack overcurrent."""
    out = {}
    for d in DIRECTIONS:
        sign = 1.0 if d == CHARGE else -1.0
        flows = []
        for m in results[d][1]:
            t = core.tracks_by_label(m.label)
            if t.valid is not None:
                flows.append((m.label, sign * t.valid.current, m.limit))
        bank_flow = sum(f for _, f, _ in flows)
        react = core.react[d]
        v = react.update(now, flows, bank_flow, tgt[d], events, d)
        if v < tgt[d] - 0.05 and react.worst is not None:
            lbl, f, lim = react.worst
            why[d] = f"overload: {lbl} {f:.0f}/{lim:.0f}A -> bank {v:.0f}A ({why[d]})"
        core.ramps[d].value = v            # keep the ramps consistent for INIT/FAILSAFE
        core.ramps[d].last_t = now
        out[d] = v
    return out[CHARGE], out[DISCHARGE]
