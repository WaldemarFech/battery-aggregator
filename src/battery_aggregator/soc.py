"""SoC aggregation, drift detection and calibration tracking (design 4.4, SR-22).

SoC is a display/ESS quantity only. It is never used as a protection input unless the
pack's SoC is calibrated (see limits.discharge_factor); drift is flagged, not "repaired".
"""
from __future__ import annotations

from dataclasses import dataclass, field

DAY = 86400.0


@dataclass
class SocItem:
    label: str
    soc: float | None
    capacity: float
    suspect: bool = False


@dataclass
class SocResult:
    raw: float | None
    weighted: float | None
    min_soc: float | None
    min_label: str | None
    installed_ah: float
    remaining_ah: float
    spread: float = 0.0
    items: list = field(default_factory=list)


def aggregate(items: list[SocItem], cfg) -> SocResult:
    usable = [i for i in items if i.soc is not None and i.capacity > 0]
    installed = sum(i.capacity for i in items if i.capacity > 0)
    if not usable:
        return SocResult(None, None, None, None, installed, 0.0)
    cap = sum(i.capacity for i in usable)
    rem = sum(i.soc / 100.0 * i.capacity for i in usable)
    weighted = 100.0 * rem / cap
    lo = min(usable, key=lambda i: i.soc)
    spread = max(i.soc for i in usable) - lo.soc
    if cfg.soc_mode == "min":
        raw = lo.soc
    elif cfg.soc_mode == "guarded":
        trusted = [i for i in usable if not i.suspect]
        base = min(trusted, key=lambda i: i.soc).soc if trusted else lo.soc
        raw = min(weighted, base + cfg.soc_guard_d_pct)
    else:
        raw = weighted
    return SocResult(raw, weighted, lo.soc, lo.label, installed, rem, spread, usable)


class SocSlew:
    """Published SoC follows the raw value at most soc_slew_pct_min %/min (topology jumps)."""

    def __init__(self, cfg) -> None:
        self.rate = cfg.soc_slew_pct_min / 60.0
        self.value: float | None = None
        self.last_t: float | None = None

    def step(self, raw: float | None, now: float) -> float | None:
        dt = 0.0 if self.last_t is None else max(0.0, now - self.last_t)
        self.last_t = now
        if raw is None:
            return self.value
        if self.value is None:
            self.value = raw
        else:
            d = raw - self.value
            lim = self.rate * dt
            self.value += max(-lim, min(lim, d))
        return self.value


def update_plausibility(tracks: list, now: float, cfg, events: list) -> None:
    """Flag soc_suspect from rest-voltage checks (absolute and pack-to-pack)."""
    resting = []
    for t in tracks:
        v = t.valid
        if v is None or t.state.value != "active":
            t.rest_since = None
            continue
        if abs(v.current) < cfg.soc_rest_a:
            if t.rest_since is None:
                t.rest_since = now
        else:
            t.rest_since = None
        if t.rest_since is not None and now - t.rest_since >= cfg.soc_rest_s and v.soc is not None:
            resting.append(t)
    for t in resting:
        v = t.valid
        mean = v.voltage / t.cells
        if (v.soc < cfg.soc_abs_low_pct and mean > cfg.soc_abs_low_v) or (
                v.soc > cfg.soc_abs_high_pct and mean < cfg.soc_abs_high_v):
            _flag(t, events, f"SoC {v.soc:.0f}% implausible at rest cell mean {mean:.3f}V")
    if len(resting) >= 2:
        socs = [t.valid.soc for t in resting]
        means = [t.valid.voltage / t.cells for t in resting]
        if max(socs) - min(socs) > cfg.soc_rel_dsoc_pct and max(means) - min(means) < cfg.soc_rel_dv:
            for t in resting:
                _flag(t, events, f"SoC drift: dSoC {max(socs) - min(socs):.0f}% at dV "
                                 f"{1000 * (max(means) - min(means)):.0f}mV, calibration charge recommended")


def _flag(t, events: list, msg: str) -> None:
    if not t.soc_suspect:
        t.soc_suspect = True
        events.append(("warn", t.label, msg))


class Calibration:
    """Tracks full charges (t_last_full) and optionally runs a calibration charge (S20)."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.active_since: float | None = None
        self.tail_since: float | None = None
        self.last_end: float | None = None

    def due(self, tracks: list, wall: float | None) -> bool:
        lim = self.cfg.calibration_interval_d * DAY
        for t in tracks:
            if t.state.value in ("gone", "excluded"):
                continue
            if t.t_last_full is None or (wall is not None and wall - t.t_last_full > lim):
                return True
        return False

    def observe_full(self, tracks: list, wall: float | None, v_bus: float | None, cvl: float,
                     events: list) -> None:
        if wall is None:
            return
        for t in tracks:
            v = t.valid
            if v is None or t.state.value != "active" or v.soc is None:
                continue
            cap = t.capacity()
            tail = self.cfg.calibration_tail_c * cap
            near_cv = v_bus is not None and v_bus >= cvl - 0.3
            if v.soc >= 99.5 and 0.0 <= v.current < tail and near_cv:
                if t.t_last_full is None or wall - t.t_last_full > 3600:
                    events.append(("info", t.label, "full charge reached, SoC synchronised"))
                t.t_last_full = wall
                t.soc_suspect = False

    def cap(self, tracks: list, now: float, wall: float | None, i_bank: float,
            events: list) -> float | None:
        """CVL cap while a calibration charge runs, else None."""
        cfg = self.cfg
        if not cfg.calibration_enabled:
            self.active_since = None
            return None
        active = [t for t in tracks if t.state.value == "active" and t.valid is not None]
        if self.active_since is None:
            cooled = self.last_end is None or now - self.last_end >= DAY
            if cooled and self.due(tracks, wall) and i_bank > 5.0 and active:
                self.active_since = now
                self.tail_since = None
                events.append(("info", "bank", "calibration charge started"))
            else:
                return None
        done = all(0.0 <= t.valid.current < cfg.calibration_tail_c * t.capacity() and
                   (t.valid.soc or 0) >= 99.5 for t in active) if active else False
        if done:
            self.tail_since = self.tail_since if self.tail_since is not None else now
        else:
            self.tail_since = None
        if (self.tail_since is not None and now - self.tail_since >= 60.0) or \
                now - self.active_since >= cfg.calibration_max_s:
            events.append(("info", "bank", "calibration charge finished"))
            self.active_since = None
            self.last_end = now
            return None
        return cfg.cells * cfg.calibration_v_cell
