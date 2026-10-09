"""Current-share estimator per pack and direction (design 4.2, FMEA SR-03/E9/E10).

Model: I_i = s_i * I_bank + c_i. Offsets c_i (circulating current) are learned at rest,
shares from quasi-stationary samples with |I_bank| >= i_est_min. The published share is
conservative (high-biased): s_eff = clamp(max(s_hat + k*sigma, short-window max), floor, 1).
Without enough samples the capacity prior is used: s_eff = min(1, 1.5 * C_i / sum C).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from .model import CHARGE, DIRECTIONS, DISCHARGE


@dataclass
class DirEstimate:
    s_hat: float = 0.0
    var: float = 0.0
    n: int = 0
    short: deque = field(default_factory=deque)  # (t, sample)


@dataclass
class PackEstimate:
    est: dict = field(default_factory=lambda: {d: DirEstimate() for d in DIRECTIONS})
    offset: float = 0.0
    offset_n: int = 0


class ShareEstimator:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.packs: dict[str, PackEstimate] = {}
        self.prev_i_bank: float | None = None
        self.prev_t: float | None = None

    def _p(self, label: str) -> PackEstimate:
        if label not in self.packs:
            self.packs[label] = PackEstimate()
        return self.packs[label]

    # ------------------------------------------------------------------ learning
    def update(self, now: float, currents: dict[str, float], sample_ok: dict[str, bool]) -> None:
        """currents: pack label -> current of ALL reference packs (all ACTIVE).

        sample_ok[direction] is False when a reference pack has that path open or the
        topology is incomplete; the caller passes {} to skip learning entirely.
        """
        cfg = self.cfg
        i_bank = sum(currents.values())
        dt = None if self.prev_t is None else now - self.prev_t
        di = None if self.prev_i_bank is None else i_bank - self.prev_i_bank
        self.prev_t, self.prev_i_bank = now, i_bank
        if not currents or dt is None or dt <= 0 or dt > cfg.est_max_dt_s:
            return
        if abs(i_bank) < cfg.i_rest_max_a:
            a = min(1.0, dt / cfg.offset_tau_s)
            for lbl, i in currents.items():
                p = self._p(lbl)
                p.offset += a * (i - p.offset) if p.offset_n else (i - p.offset)
                p.offset_n += 1
            mean = sum(self._p(l).offset for l in currents) / len(currents)
            for lbl in currents:
                self._p(lbl).offset -= mean
            return
        if abs(i_bank) < cfg.i_est_min_a or abs(di) / dt > cfg.est_di_dt_max_a_s:
            return
        d = CHARGE if i_bank > 0 else DISCHARGE
        if not sample_ok.get(d, False):
            return
        for lbl, i in currents.items():
            p = self._p(lbl)
            x = (i - p.offset) / i_bank
            x = min(1.5, max(-0.5, x))
            e = p.est[d]
            if e.n == 0:
                e.s_hat = x
            else:
                tau = cfg.est_tau_up_s if x > e.s_hat else cfg.est_tau_down_s
                e.s_hat += min(1.0, dt / tau) * (x - e.s_hat)
            e.var += min(1.0, dt / cfg.est_var_tau_s) * ((x - e.s_hat) ** 2 - e.var) if e.n else 0.0
            e.n += 1
            e.short.append((now, x))
            while e.short and now - e.short[0][0] > cfg.est_short_window_s:
                e.short.popleft()
        # Kirchhoff normalisation only upwards (sum < 1): scaling down would make the
        # conservative bias disappear in one step; overestimation is merely inefficient.
        total = sum(self._p(l).est[d].s_hat for l in currents)
        if 0.5 < total < 1.0:
            for lbl in currents:
                self._p(lbl).est[d].s_hat /= total

    # ------------------------------------------------------------------ queries
    def offset(self, label: str) -> float:
        p = self.packs.get(label)
        return p.offset if p else 0.0

    def valid(self, label: str, d: str) -> bool:
        p = self.packs.get(label)
        return bool(p and p.est[d].n >= self.cfg.est_min_samples)

    def s_eff(self, label: str, d: str, prior: float, n_ref: int) -> float:
        cfg = self.cfg
        floor = max(cfg.share_floor_prior_frac * prior,
                    cfg.share_floor_equal_frac / max(1, n_ref), 0.01)
        if not self.valid(label, d):
            return min(1.0, max(floor, cfg.share_prior_factor * prior))
        e = self.packs[label].est[d]
        sigma = math.sqrt(max(0.0, e.var))
        s = e.s_hat + cfg.share_k * sigma
        if e.short:
            s = max(s, max(x for _, x in e.short))
        return min(1.0, max(floor, s))

    # ------------------------------------------------------------------ topology
    def set_estimate(self, label: str, d: str, s_hat: float, sigma: float = 0.0,
                     n: int | None = None, offset: float | None = None) -> None:
        e = self._p(label).est[d]
        e.s_hat = s_hat
        e.var = sigma * sigma
        e.n = self.cfg.est_min_samples if n is None else n
        e.short.clear()
        if offset is not None:
            self._p(label).offset = offset

    def on_removed(self, removed: str, remaining_seff: dict[str, dict[str, float]],
                   removed_seff: dict[str, float]) -> None:
        """A pack left the reference set (GONE/EXCLUDED): assume it is electrically gone and
        carry the renormalised conservative shares over, so limits stay continuous (SR-02)."""
        for d in DIRECTIONS:
            denom = max(0.05, 1.0 - removed_seff.get(d, 0.0))
            for lbl, by_dir in remaining_seff.items():
                p = self._p(lbl)
                e = p.est[d]
                sigma = math.sqrt(max(0.0, e.var)) if e.n else 0.0
                target = min(1.0, by_dir[d] / denom)
                e.s_hat = max(0.0, target - self.cfg.share_k * sigma)
                e.var = sigma * sigma
                e.n = max(e.n, self.cfg.est_min_samples)
                e.short.clear()
        self.packs.pop(removed, None)

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict:
        return {lbl: {"offset": p.offset,
                      **{d: {"s_hat": p.est[d].s_hat, "var": p.est[d].var, "n": p.est[d].n}
                         for d in DIRECTIONS}}
                for lbl, p in self.packs.items()}

    def load_dict(self, data: dict) -> None:
        for lbl, v in data.items():
            p = self._p(str(lbl))
            off = v.get("offset", 0.0)
            p.offset = float(off) if isinstance(off, (int, float)) and math.isfinite(off) else 0.0
            for d in DIRECTIONS:
                dv = v.get(d, {})
                s, var, n = dv.get("s_hat"), dv.get("var"), dv.get("n")
                ok = all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                         for x in (s, var, n))
                if ok and 0.0 <= s <= 1.0 and 0.0 <= var <= 1.0 and n >= 0:
                    p.est[d] = DirEstimate(s_hat=float(s), var=float(var), n=int(n))
