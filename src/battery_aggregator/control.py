"""Closed-loop per-pack limit protection (one controller per direction).

Why: the open-loop share model min_i (L_i - c_i)/s_i is only as good as the share estimate.
A pack near the top knee takes far LESS than its share (higher OCV), a low-resistance pack
takes MORE. The controller therefore measures what the weakest pack really takes and drives
the bank limit so that the worst pack sits at a utilisation setpoint:

    u_i = I_i / L_i                    measured, per pack, in this direction
    u   = max_i u_i                    worst pack
    u_p = u * out / I_bank             predicted at the commanded limit `out` (measured shares;
                                       corrects for the inverter/measurement lag after a cut)

Bounds:   lo = min(sum-safe floor min_i(L_i - c_i), anchor)  <=  out  <=  hi
          hi = ceiling (sum of limits, N-1, hardware/inverter cap, SR-21 cap, blind block)
               while every member is measured live; otherwise hi = anchor (no closed-loop gain
               without measurement)
          anchor = ramped open-loop model (SR-09 rate limits apply to it)

Asymmetric law:
  cut    median-of-3 u_p > cut_ratio:  out = lo + (out - lo) * max(f_min, 1 - g * (u_p - 1)),
         at least the proportional correction out * sp / u_p. 15 A at a 10 A limit (u = 1.5)
         halves the headroom above the floor at once (owner rule).
  trim   sp < u_p <= cut_ratio: velocity-form PI towards the setpoint (downwards, unlimited).
  raise  u_p < sp: velocity-form PI, ONLY while the limit binds (anti-windup: below the limit
         the measurement says nothing about higher currents), after hold_s since the last cut,
         rate-limited to raise_a_s and raise_pct_s.
Without a cut (no evidence against the model) `out` follows the anchor upwards. Membership
changes (pack joins/leaves) reset the controller state; `out` never jumps up on a reset.
"""
from __future__ import annotations


def _median3(xs: list[float]) -> float:
    s = sorted(xs[-3:])
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


class LimitController:
    def __init__(self, cfg, direction: str) -> None:
        self.cfg = cfg
        self.direction = direction
        self.out: float | None = None
        self.r = 1.0                   # out / anchor: the closed-loop correction of the model
        self.idle_since: float | None = None
        self.hold_until = 0.0          # no raise before this time (after a cut or a reset)
        self.members: frozenset = frozenset()
        self.cut_t: float | None = None
        self.last_t: float | None = None
        self.e_prev: float | None = None
        self.hist: list[float] = []
        self.circ_since: float | None = None
        self.u = 0.0
        self.u_pred = 0.0
        self.u_f: float | None = None  # low-pass filtered u_p for the PI (noise)
        self.state = "init"

    # ------------------------------------------------------------------ helpers
    def reset(self, now: float, why: str, events: list) -> None:
        self.hist.clear()
        self.e_prev = None
        self.u_f = None
        self.hold_until = max(self.hold_until, now + self.cfg.hold_after_drop_s)
        if self.out is not None:
            events.append(("info", "bank", f"{self.direction} controller reset: {why}"))

    def force(self, value: float, now: float) -> None:
        """Clamp from outside (fault wrapper / output validation); the ramp is clamped too."""
        if self.out is not None and value < self.out:
            self.r = min(self.r, 1.0)

    def _max_rise(self, dt: float) -> float:
        c = self.cfg
        return min(c.ctl_raise_a_s * dt, c.ctl_raise_pct_s / 100.0 * max(self.out, c.ramp_pct_base_a) * dt)

    # ------------------------------------------------------------------ step
    def update(self, now: float, *, anchor: float, ceiling: float, floor: float, live: bool,
               members, ratios: list[tuple[str, float]], flow: float, events: list,
               flow_ok: bool = True) -> float:
        """anchor: ramped open-loop limit; ceiling: hard upper bound; floor: sum-safe floor;
        live: every member is measured live; ratios: (label, I/L) of members carrying current in
        this direction; flow: bank current in this direction (A, > 0 when flowing);
        flow_ok: every member reported this cycle (else flow is partly held data)."""
        c = self.cfg
        dt = 0.0 if self.last_t is None else max(0.0, min(5.0, now - self.last_t))
        self.last_t = now
        anchor = max(0.0, anchor)
        hi = max(0.0, min(ceiling, anchor) if not live else max(ceiling, 0.0))
        anchor = min(anchor, hi)
        lo = max(0.0, min(floor, anchor, hi))
        members = frozenset(members)
        if members != self.members:
            if self.members or self.out is not None:
                self.reset(now, f"members {sorted(self.members)} -> {sorted(members)}", events)
                self.r = min(self.r, 1.0)               # never jump up on a reset
            self.members = members
        # feedforward: the correction scales with the (ramped) open-loop model, so limit
        # changes of any pack act at once; the loop then trims what the model got wrong
        if self.cut_t is None:
            self.r = max(self.r, 1.0)                   # no evidence against the model
        elif now - self.cut_t >= c.ctl_forget_s and self.r >= 1.0:
            self.cut_t = None
        self.out = anchor * self.r
        worst_lbl, u = max(ratios, key=lambda x: x[1]) if ratios else ("", 0.0)
        self.u = u

        if flow < c.ctl_min_flow_a:
            self.idle_since = now if self.idle_since is None else self.idle_since
            if now - self.idle_since >= c.ctl_recover_s:
                self.out = min(self.out, anchor)        # gain above the model needs live evidence
            # no bank current in this direction: nothing to measure. A pack over its limit is
            # circulating current - lowering the bank limit cannot help (design S09).
            self.hist.clear()
            self.e_prev = None
            self.u_f = None
            if u > c.ctl_cut_ratio and members:
                if self.circ_since is None:
                    self.circ_since = now
                elif now - self.circ_since >= c.fb_trip_s:
                    events.append(("warn", worst_lbl, f"circulating current exceeds {self.direction} "
                                                      f"limit of {worst_lbl} ({u:.2f}x)"))
                    self.circ_since = now + 60.0
            else:
                self.circ_since = None
            self._recover(now, dt, anchor)
            return self._clamp(lo, hi, "idle", anchor)
        self.circ_since = None
        self.idle_since = None

        extrapolate = flow_ok and flow >= c.ctl_predict_frac * self.out and self.out > 0
        base = self.out if extrapolate or not flow_ok else min(self.out, flow)
        u_p = u * self.out / flow if extrapolate else u
        self.hist.append(u_p)
        del self.hist[:-3]
        um = _median3(self.hist)
        a = 1.0 if self.u_f is None or c.ctl_filter_s <= 0 else min(1.0, dt / c.ctl_filter_s)
        self.u_f = um if self.u_f is None else self.u_f + a * (um - self.u_f)
        self.u_pred = um
        sp = c.ctl_setpoint

        if um > c.ctl_cut_ratio or (u_p >= c.ctl_cut_now_ratio and u >= c.ctl_cut_ratio):
            um = max(um, u_p) if u_p >= c.ctl_cut_now_ratio else um
            f = max(c.ctl_cut_min_factor, 1.0 - c.ctl_cut_gain * (um - 1.0))
            new = max(lo, min(lo + (base - lo) * f, base * sp / um))
            if new < self.out:
                events.append(("feedback", worst_lbl,
                               f"{self.direction}: {worst_lbl} at {um:.2f}x limit, bank limit "
                               f"{self.out:.1f} -> {new:.1f}A"))
                self.out = new
            self.cut_t = now
            self.hold_until = max(self.hold_until, now + c.ctl_hold_s)
            self.hist.clear()
            self.e_prev = None
            self.u_f = None
            return self._clamp(lo, hi, "cut", anchor)

        uf = max(self.u_f, um) if um > sp else self.u_f   # trims react to the raw median
        e = sp - uf
        gain = self.out / max(uf, 0.05)                 # A per unit utilisation (measured shares)
        de = 0.0 if self.e_prev is None else e - self.e_prev
        self.e_prev = e
        d = gain * (c.ctl_kp * de + c.ctl_ki * e * dt)
        if e < 0:
            d = min(d, 0.0)
            self.out = max(lo, self.out + d)
            if self.out < anchor:
                self.cut_t = now
            return self._clamp(lo, hi, "trim", anchor)
        # binding = the limit is reached AND obeyed; beyond it (lag, overrun S39) no raise
        binding = c.ctl_binding_frac * self.out <= flow <= 1.05 * self.out + 1.0
        held = now < self.hold_until
        if binding and not held and d > 0:
            self.out = min(hi, self.out + min(d, self._max_rise(dt)))
            return self._clamp(lo, hi, "raise", anchor)
        self._recover(now, dt, anchor)
        return self._clamp(lo, hi, "hold", anchor)

    def _recover(self, now: float, dt: float, anchor: float) -> None:
        """After a cut, drift back to the open-loop anchor while there is no new evidence."""
        c = self.cfg
        if self.cut_t is not None and self.out < anchor and now - self.cut_t >= c.ctl_recover_s:
            self.out = min(anchor, self.out + self._max_rise(dt))

    def _clamp(self, lo: float, hi: float, state: str, anchor: float) -> float:
        self.state = state
        self.out = min(hi, max(lo, self.out))
        if anchor > 0.05:
            self.r = self.out / anchor
        return self.out
