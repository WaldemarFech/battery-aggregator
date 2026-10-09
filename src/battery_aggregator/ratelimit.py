"""Rate limiting with hysteresis (SR-09) and measured-overcurrent feedback (design 4.2).

Limits drop immediately and rise slowly: +ramp_up_a_s A/s and at most +ramp_up_pct_s %/s
(of max(value, ramp_pct_base_a)). A rise only starts after the target has offered headroom
(>= deadband) for hold_s without interruption; any drop or binding step restarts the hold.
"""
from __future__ import annotations

import math


class Ramp:
    def __init__(self, value: float, abs_rate: float, pct_rate: float, pct_base: float,
                 hold_s: float, deadband: float) -> None:
        self.value = value
        self.abs_rate = abs_rate
        self.pct_rate = pct_rate
        self.pct_base = pct_base
        self.hold_s = hold_s
        self.deadband = deadband
        self.last_drop: float | None = None
        self.last_t: float | None = None
        self.rising = False

    def max_rise(self, dt: float) -> float:
        r = self.abs_rate * dt
        if self.pct_rate > 0:
            r = min(r, self.pct_rate / 100.0 * max(self.value, self.pct_base) * dt)
        return r

    def step(self, target: float, now: float, allow_up: bool = True) -> float:
        dt = 0.0 if self.last_t is None else max(0.0, now - self.last_t)
        self.last_t = now
        if not math.isfinite(target):
            target = 0.0
        if target < self.value:
            self.value = target
            self.last_drop = now
            self.rising = False
            return self.value
        gap = target - self.value
        if not allow_up or (gap < self.deadband and not self.rising):
            # binding (or rises blocked): the hold time restarts, so a limit that flaps
            # between low and high never creeps up (S40) - rise needs sustained headroom
            self.last_drop = now
            self.rising = False
            return self.value
        if self.last_drop is not None and now - self.last_drop < self.hold_s:
            return self.value
        self.value = min(target, self.value + self.max_rise(dt))
        self.rising = self.value < target
        return self.value

    def force(self, value: float, now: float) -> None:
        """Clamp externally (fault / failsafe published something lower)."""
        if value < self.value:
            self.value = value
            self.last_drop = now


class Feedback:
    """Per-direction factor k in [fb_min, 1] reacting to measured pack overcurrent."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.k = 1.0
        self.over_since: float | None = None
        self.ok_since: float | None = None
        self.last_t: float | None = None
        self.circ_since: float | None = None

    def update(self, now: float, ratios: list[tuple[str, float]], bank_in_direction: bool,
               events: list, direction: str) -> None:
        cfg = self.cfg
        dt = 0.0 if self.last_t is None else max(0.0, now - self.last_t)
        self.last_t = now
        worst_lbl, worst = max(ratios, key=lambda x: x[1]) if ratios else ("", 0.0)
        if worst > cfg.fb_trip_ratio:
            self.ok_since = None
            if bank_in_direction:
                self.circ_since = None
                if self.over_since is None:
                    self.over_since = now
                elif now - self.over_since >= cfg.fb_trip_s:
                    new_k = max(cfg.fb_min, self.k * cfg.fb_target / worst)
                    events.append(("feedback", worst_lbl,
                                   f"{direction}: {worst_lbl} at {worst:.2f}x limit, k {self.k:.3f}->{new_k:.3f}"))
                    self.k = new_k
                    self.over_since = None
            else:
                # Circulating current: lowering the bank limit cannot help (design S09).
                self.over_since = None
                if self.circ_since is None:
                    self.circ_since = now
                elif now - self.circ_since >= cfg.fb_trip_s:
                    events.append(("warn", worst_lbl,
                                   f"circulating current exceeds {direction} limit of {worst_lbl} ({worst:.2f}x)"))
                    self.circ_since = now + 60.0  # rate-limit the event
            return
        self.over_since = None
        self.circ_since = None
        if worst < cfg.fb_recover_ratio:
            if self.ok_since is None:
                self.ok_since = now
            elif now - self.ok_since >= cfg.fb_recover_s and self.k < 1.0:
                self.k = min(1.0, self.k + cfg.fb_recover_rate_s * dt)
        else:
            self.ok_since = None
