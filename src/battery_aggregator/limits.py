"""Bank current-limit engine (design 4.2, FMEA SR-01/02/06/07).

Bank limit = largest bank current at which every pack stays within its own limit:
    min_i (L_i - c_i) / s_i,  capped by sum(L_i), the hardware cap and the N-1 cascade check
    min_k min_{i!=k} ocp_i / min(1, s_i / (1 - s_k)).
Uncertain packs (stale/quarantine) are evaluated in two worlds (present / gone) and the
minimum is taken, so the result is safe whichever world is real.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .model import CHARGE, ValidPack

EPS = 0.05
INF = float("inf")


def _lin(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    if x1 == x0:
        return y1 if x >= x1 else y0
    f = min(1.0, max(0.0, (x - x0) / (x1 - x0)))
    return y0 + f * (y1 - y0)


def charge_factor(v: ValidPack, cfg, hv_latched: bool) -> tuple[float, str]:
    """Own tightening of a pack's CCL (never loosens): cells, temperature, alarms."""
    if v.allow_charge is False:
        return 0.0, "AllowToCharge=0"
    if hv_latched or v.cell_max >= cfg.v_cell_hard:
        return 0.0, f"cell {v.cell_max:.3f}V >= hard {cfg.v_cell_hard}V"
    for a in ("HighCellVoltage", "HighVoltage", "LowChargeTemperature", "HighChargeTemperature"):
        if v.alarm(a) >= 2:
            return 0.0, f"alarm {a}=2"
    f, why = 1.0, ""
    if v.cell_max > cfg.v_cell_target:
        if v.cell_max <= cfg.v_cell_soft:
            fc = _lin(v.cell_max, cfg.v_cell_target, cfg.v_cell_soft, 1.0, cfg.cell_soft_frac)
        else:
            fc = _lin(v.cell_max, cfg.v_cell_soft, cfg.v_cell_hard, cfg.cell_soft_frac, 0.0)
        if fc < f:
            f, why = fc, f"cell taper {v.cell_max:.3f}V"
    ft, twhy = charge_temp_factor(v.temp_min, v.temp_max, cfg)
    if ft < f:
        f, why = ft, twhy
    return f, why


def charge_temp_factor(t_min: float | None, t_max: float | None, cfg) -> tuple[float, str]:
    """M5: LFP charge derating of ONE pack from its coldest and hottest sensor.

    0 A at/below t_charge_min_c (0 C) and at/above t_charge_hot_stop_c (45 C); linear taper
    t_charge_min_c..t_charge_full_c and t_charge_hot_start_c..t_charge_hot_stop_c. The bank
    limit min_i (L_i - c_i)/s_i then follows the most restrictive pack.
    """
    if t_min is None:
        return 0.0, "temperature unknown"
    t_hi = t_max if t_max is not None else t_min
    if t_min < cfg.t_charge_min_c:
        return 0.0, f"cold {t_min:.1f}C (charge blocked < {cfg.t_charge_min_c:.0f}C)"
    if t_hi >= cfg.t_charge_hot_stop_c:
        return 0.0, f"hot {t_hi:.1f}C (charge blocked >= {cfg.t_charge_hot_stop_c:.0f}C)"
    f, why = 1.0, ""
    if cfg.t_charge_full_c > cfg.t_charge_min_c and t_min < cfg.t_charge_full_c:
        f, why = _lin(t_min, cfg.t_charge_min_c, cfg.t_charge_full_c, 0.0, 1.0), f"cold taper {t_min:.1f}C"
    if t_hi > cfg.t_charge_hot_start_c:
        fh = _lin(t_hi, cfg.t_charge_hot_start_c, cfg.t_charge_hot_stop_c, 1.0, 0.0)
        if fh < f:
            f, why = fh, f"hot taper {t_hi:.1f}C"
    return f, why


def discharge_factor(v: ValidPack, cfg) -> tuple[float, str]:
    if v.allow_discharge is False:
        return 0.0, "AllowToDischarge=0"
    for a in ("LowCellVoltage", "LowVoltage"):
        if v.alarm(a) >= 2:
            return 0.0, f"alarm {a}=2"
    f, why = 1.0, ""
    if v.cell_min < cfg.v_cell_low_soft:
        fc = _lin(v.cell_min, cfg.v_cell_low_hard, cfg.v_cell_low_soft, 0.0, 1.0)
        if fc < f:
            f, why = fc, f"low cell {v.cell_min:.3f}V"
    if v.temp_min is not None and v.temp_min < cfg.t_dchg_full_c:
        ft = _lin(v.temp_min, cfg.t_dchg_min_c, cfg.t_dchg_full_c, 0.0, 1.0)
        if ft < f:
            f, why = ft, f"cold {v.temp_min:.1f}C"
    if v.temp_max is not None and v.temp_max > cfg.t_dchg_hot_start_c:
        ft = _lin(v.temp_max, cfg.t_dchg_hot_start_c, cfg.t_dchg_hot_stop_c, 1.0, 0.0)
        if ft < f:
            f, why = ft, f"hot {v.temp_max:.1f}C"
    if cfg.dcl_soc_taper_start_pct is not None and v.soc_calibrated and v.soc is not None:
        # SR-22: SoC only acts as protection input when the pack SoC is calibrated.
        fs = _lin(v.soc, cfg.dcl_soc_taper_end_pct, cfg.dcl_soc_taper_start_pct, 0.0, 1.0)
        if fs < f:
            f, why = fs, f"soc taper {v.soc:.0f}%"
    return f, why


def pack_limit(v: ValidPack, direction: str, cfg, hv_latched: bool) -> tuple[float, str]:
    if direction == CHARGE:
        f, why = charge_factor(v, cfg, hv_latched)
        return v.ccl * f, why or f"BMS CCL {v.ccl:.1f}A"
    f, why = discharge_factor(v, cfg)
    return v.dcl * f, why or f"BMS DCL {v.dcl:.1f}A"


@dataclass
class Member:
    label: str
    limit: float          # tapered pack limit (A)
    share: float          # conservative share s_eff relative to the reference set
    offset: float         # positive circulating current in this direction (A)
    ocp: float | None     # hardware cut-off current
    role: str             # 'active' | 'uncertain'
    why: str = ""


@dataclass
class LimitResult:
    value: float
    reason: str
    model: float = INF
    total: float = INF
    n1: float = INF
    shares: dict = field(default_factory=dict)
    worlds: dict = field(default_factory=dict)
    ceiling: float = INF   # every bound except the share term (closed-loop upper bound)
    floor: float = 0.0     # sum-safe floor min_i (L_i - c_i): safe whatever the shares


def evaluate_world(members: list[Member], ref_shares: dict[str, float], n1_mode: str,
                   normalize: bool = False) -> LimitResult:
    """normalize: Kirchhoff-normalise the conservative shares (sum 1) - used only for the
    closed-loop ceiling, where the measured utilisation protects the packs and the
    deliberately high-biased shares (sum ~1.5) would otherwise count twice."""
    if not members:
        return LimitResult(0.0, "no pack can take current", 0.0, 0.0, 0.0)
    labels = {m.label for m in members}
    missing = sum(s for lbl, s in ref_shares.items() if lbl not in labels)
    denom = max(EPS, 1.0 - missing)
    if normalize:
        denom = max(EPS, sum(m.share for m in members), 1.0 - missing if missing else 1.0)
    shares = {m.label: min(1.0, m.share / denom) for m in members}
    best, reason = INF, ""
    for m in members:
        term = max(0.0, m.limit - m.offset) / shares[m.label]
        if term < best:
            best, reason = term, f"{m.label} {m.limit:.1f}A / s {shares[m.label]:.2f}" + (
                f" ({m.why})" if m.why else "")
    model = best
    total = sum(m.limit for m in members)
    if total < best:
        best, reason = total, "sum of pack limits"
    n1 = INF
    if len(members) >= 2:
        for k in members:
            rest = max(EPS, 1.0 - shares[k.label])
            for i in members:
                if i is k:
                    continue
                lim = i.ocp if (n1_mode == "hw" and i.ocp is not None) else i.limit
                after = min(1.0, shares[i.label] / rest)
                val = lim / after
                if val < n1:
                    n1 = val
                    if val < best:
                        best, reason = val, f"N-1: {i.label} if {k.label} trips"
    return LimitResult(best, reason, model, total, n1, shares)


def bank_limit(active: list[Member], uncertain: list[Member], blind_present: bool,
               ref_shares: dict[str, float], hw_cap: float, cfg) -> LimitResult:
    """Combine worlds, degradation and hardware cap. Pure function."""
    world_a = evaluate_world(active + uncertain, ref_shares, cfg.n1_mode)
    res = world_a
    worlds = {"A": world_a.value}
    if uncertain and active:
        world_b = evaluate_world(active, ref_shares, cfg.n1_mode)
        worlds["B"] = world_b.value
        if world_b.value < res.value:
            res = LimitResult(world_b.value, world_b.reason + " (uncertain pack assumed gone)",
                              world_b.model, world_b.total, world_b.n1, world_b.shares)
    if blind_present and active:
        deg = cfg.degraded_sum_factor * sum(m.limit for m in active)
        if deg < res.value:
            res = LimitResult(deg, f"degraded {cfg.degraded_sum_factor:.2f} x sum(active)",
                              res.model, res.total, res.n1, res.shares)
    if hw_cap < res.value:
        res = LimitResult(hw_cap, "hardware cap", res.model, res.total, res.n1, res.shares)
    if not math.isfinite(res.value) or res.value < 0:
        res = LimitResult(0.0, "invalid computation", res.model, res.total, res.n1, res.shares)
    res.worlds = worlds
    # closed-loop bounds (control.py): the ceiling drops only the share term of world A/B
    n1_ceil = world_a.n1
    if cfg.limit_control == "pi" and not uncertain:
        n1_ceil = evaluate_world(active, ref_shares, cfg.n1_mode, normalize=True).n1
    ceil = min(world_a.total, n1_ceil, hw_cap)
    if uncertain and active:
        ceil = min(ceil, world_b.total, world_b.n1)
    if blind_present and active:
        ceil = min(ceil, cfg.degraded_sum_factor * sum(m.limit for m in active))
    res.ceiling = max(res.value, ceil) if math.isfinite(ceil) and ceil >= 0 else res.value
    every = active + uncertain
    res.floor = min(res.value, min((max(0.0, m.limit - m.offset) for m in every), default=0.0))
    return res
