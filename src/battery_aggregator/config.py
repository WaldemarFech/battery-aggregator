"""Configuration with hard safety bounds (FMEA F6).

All defaults follow docs/02_DESIGN.md section 10 and docs/04_SICHERHEIT_FMEA.md.
Values the owner must decide (hardware caps, OCP currents) are explicit fields.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields, replace
from typing import Any

# Owner battery policy 2026-10-04 (binding): LFP, CVL 3.55 V/cell is the ceiling. The
# configured CVL may only be lowered (M4). Charge temperature window 0..45 C (M5): the
# configured window may only be narrowed.
CVL_MAX_V_PER_CELL_LIMIT = 3.55
LFP_CHARGE_MIN_C = 0.0
LFP_CHARGE_MAX_C = 45.0


@dataclass(frozen=True)
class PackConfig:
    """Static identity of one pack. Identity is the BMS serial (SR-12)."""

    serial: str
    label: str
    capacity_ah: float
    cells: int = 16
    ocp_charge_a: float | None = None      # JK hardware cut-off current (charge)
    ocp_discharge_a: float | None = None   # JK hardware cut-off current (discharge)


@dataclass(frozen=True)
class Config:
    packs: tuple[PackConfig, ...] = ()
    auto_add_unknown: bool = False       # whitelist mode when packs are configured
    expected_pack_count: int | None = None
    cells: int = 16                      # cells in series per pack (bank voltage basis)

    # Owner decisions / hardware (docs/DECISIONS.md D1-D4)
    # COMMISSIONING INPUT: unknown until read on site. None = no extra cap; the bank is then
    # bounded by the sum of BMS limits, the SR-21 cap and N-1 on BMS limits (fallback).
    ccl_hw_a: float | None = None        # cables, fuses, busbar (charge)
    dcl_hw_a: float | None = None        # cables, fuses, busbar (discharge)
    # Owner fact 2026-10-04: 3 MultiPlus ~11 kW charge power -> ~210 A bank charge cap.
    # Each JK pack accepts 200 A. None = no inverter cap (design/test numbers).
    inverter_ccl_cap_a: float | None = 210.0
    n1_mode: str = "hw"                  # "hw" (JK OCP) | "soft" (BMS limits)
    dcl_emergency_a: float = 90.0        # FAILSAFE / INIT discharge limit
    failsafe_dcl_never_raise: bool = True  # SR-02: failsafe never raises DCL
    failsafe_dcl_ramp_s: float = 10.0

    # SR-21 commissioning cap
    measured_shares_verified: bool = False
    commissioning_ccl_cap_a: float = 210.0   # = inverter cap (owner fact 2026-10-04, D5)
    commissioning_dcl_cap_a: float = 59.0

    # Pack lifecycle
    stale_after_s: float = 10.0
    frozen_after_s: float = 60.0
    offline_after_s: float = 30.0
    gone_after_s: float = 600.0
    quarantine_s: float = 30.0
    quarantine_max_s: float = 600.0
    flap_window_s: float = 600.0
    init_timeout_s: float = 120.0
    # Restart hold (design 5.3a): after a service restart the last published CVL/CCL/DCL from the
    # state file are held (only ever lowered by live pack limits) while the packs are re-admitted,
    # instead of the INIT defaults (CCL 0, CVL failsafe). 0 disables. Must cover quarantine_s.
    restart_hold_s: float = 45.0
    restart_hold_max_age_s: float = 180.0  # persisted outputs older than this are not held

    # Share estimator
    i_est_min_a: float = 15.0
    i_rest_max_a: float = 3.0
    est_di_dt_max_a_s: float = 5.0
    est_max_dt_s: float = 1.5
    est_tau_up_s: float = 30.0
    est_tau_down_s: float = 600.0
    est_var_tau_s: float = 600.0
    est_min_samples: int = 30
    est_short_window_s: float = 60.0
    offset_tau_s: float = 600.0
    share_k: float = 2.0
    share_prior_factor: float = 1.5
    share_floor_prior_frac: float = 0.5
    share_floor_equal_frac: float = 0.8  # D2: floor 0.8/N (FMEA E10), decided 2026-10-04

    # Rate limiting (SR-09)
    ramp_up_a_s: float = 5.0
    ramp_up_pct_s: float = 1.0           # 1 %/s == +10 % per 10 s
    ramp_pct_base_a: float = 50.0
    hold_after_drop_s: float = 30.0
    limit_deadband_a: float = 1.0
    cvl_ramp_v_s: float = 0.005          # 0.05 V per 10 s
    cvl_hold_after_drop_s: float = 60.0
    cvl_deadband_v: float = 0.02
    soc_slew_pct_min: float = 2.0

    # Cell voltage controller and tapers (SR-06)
    v_cell_target: float = 3.45
    v_cell_soft: float = 3.55
    v_cell_hard: float = 3.60
    v_cell_release: float = 3.50
    cell_soft_frac: float = 0.30
    cell_reg_gain: float = 16.0
    cvl_max_v_per_cell: float = 3.55     # M4: only <= CVL_MAX_V_PER_CELL_LIMIT accepted
    cvl_min_v_per_cell: float = 3.00
    cvl_reg_floor_v_per_cell: float = 3.35  # M3: cell controller never pulls CVL below this
    cvl_reg_i_min_a: float = 2.0         # M3: integrate only while the bank really charges
    failsafe_cvl_v_per_cell: float = 3.35
    v_cell_low_soft: float = 3.00
    v_cell_low_hard: float = 2.90

    # Temperature (SR-07)
    # M5 (owner policy): 0 A at <= 0 C and >= 45 C, linear taper zones 0..10 C and 40..45 C.
    t_charge_min_c: float = 0.0
    t_charge_full_c: float = 10.0
    t_charge_hot_start_c: float = 40.0
    t_charge_hot_stop_c: float = 45.0
    t_dchg_min_c: float = -20.0
    t_dchg_full_c: float = -10.0
    t_dchg_hot_start_c: float = 55.0
    t_dchg_hot_stop_c: float = 65.0
    blind_charge_policy: str = "last_temp_margin"  # | "block"
    blind_temp_margin_k: float = 10.0    # D3: blind charging only if last temp >= 10 C

    # Freshness (M6): explicit /Connected = 0 makes a pack OFFLINE at once
    future_timestamp_tol_s: float = 1.0

    # Closed-loop per-pack limit protection (control.py, design 4.2a). "pi" = PI controller on
    # the worst pack's measured utilisation; "legacy" = old multiplicative feedback k (fb_*).
    limit_control: str = "pi"           # "sum" = owner rule 2026-10-08 (sum_mode.py)
    # limit_control = "sum": reactive overcurrent cut (pack |I| > L*(1+tol)+abs_tol for trip_s)
    react_tol: float = 0.02
    react_abs_tol_a: float = 1.0
    react_trip_s: float = 2.0
    react_down_interval_s: float = 3.0   # observe this long after each step down
    react_release_ratio: float = 0.98
    react_release_s: float = 2.0
    react_up_step_a: float = 20.0        # raise step (A) ...
    react_up_interval_s: float = 5.0     # ... every this many seconds while no pack is over
    ctl_setpoint: float = 0.95           # target utilisation I/L of the worst pack
    ctl_cut_ratio: float = 1.02          # median-of-3 above this -> drastic cut
    ctl_cut_now_ratio: float = 1.30      # a single sample above this (and real u > cut) cuts at once
    ctl_cut_gain: float = 1.0            # headroom factor 1 - g*(u-1): u=1.5 halves the headroom
    ctl_cut_min_factor: float = 0.25     # never keep less than 25 % of the headroom per cut
    ctl_kp: float = 0.05                 # velocity-form P on the utilisation error
    ctl_ki: float = 0.05                 # 1/s, I on the utilisation error (x loop dead time < 0.5)
    ctl_filter_s: float = 3.0            # low-pass of u for the PI (cuts use the raw median-of-3)
    ctl_raise_a_s: float = 2.0           # raise rate limits (A/s and %/s of max(out, ramp_pct_base_a))
    ctl_raise_pct_s: float = 2.0
    ctl_hold_s: float = 5.0              # no raise this long after a cut (> inverter + BMS lag)
    ctl_recover_s: float = 30.0          # idle drift back to the open-loop model after a cut
    ctl_forget_s: float = 300.0          # cut evidence expires (out >= model again)
    ctl_binding_frac: float = 0.90       # limit counts as binding at flow >= 90 % of it
    ctl_predict_frac: float = 0.50       # extrapolate u to the limit only at flow >= 50 % of it
    ctl_min_flow_a: float = 2.0          # below: no measurement in this direction
    ctl_max_age_s: float = 3.0           # every member updated within this, else no gain over the model
    ctl_flow_age_s: float = 1.5          # older member data: bank flow unreliable, no extrapolation

    # Measured-overcurrent feedback (limit_control = "legacy"; fb_trip_s also times the
    # circulating-current warning)
    fb_trip_ratio: float = 1.05
    fb_trip_s: float = 3.0
    fb_target: float = 0.95
    fb_recover_ratio: float = 0.90
    fb_recover_s: float = 30.0
    fb_recover_rate_s: float = 0.01
    fb_min: float = 0.05
    degraded_sum_factor: float = 0.9

    # Charge/discharge path evidence (design 4.6)
    fet_evidence_bank_a: float = 20.0
    fet_evidence_pack_a: float = 1.0
    fet_evidence_s: float = 10.0
    fet_close_a: float = 2.0
    allow_flag_is_fet: bool = False      # [VERIFY] serialbattery semantics
    pack_limit_nominal_a: float = 200.0
    disconnect_dv: float = 0.5

    # SoC (SR-22: never a protection input unless calibrated)
    soc_mode: str = "weighted"           # weighted | min | guarded
    soc_guard_d_pct: float = 10.0
    soc_rest_a: float = 2.0
    soc_rest_s: float = 1800.0
    soc_rel_dsoc_pct: float = 10.0
    soc_rel_dv: float = 0.015
    soc_abs_low_pct: float = 10.0
    soc_abs_low_v: float = 3.25
    soc_abs_high_pct: float = 90.0
    soc_abs_high_v: float = 3.30
    imbalance_dsoc_pct: float = 20.0
    dcl_soc_taper_start_pct: float | None = None  # only calibrated packs
    dcl_soc_taper_end_pct: float = 0.0
    calibration_interval_d: float = 14.0
    calibration_enabled: bool = False
    calibration_v_cell: float = 3.45
    calibration_tail_c: float = 0.02
    calibration_max_s: float = 7200.0

    # Fault handling
    fault_failsafe_after_s: float = 10.0
    fault_restart_after_s: float = 60.0

    # Validation ranges (C8)
    pack_limit_max_a: float = 500.0
    pack_current_max_a: float = 1000.0
    cell_v_min: float = 2.0
    cell_v_max: float = 4.0
    temp_min_c: float = -40.0
    temp_max_c: float = 90.0

    # Venus integration
    shadow: bool = True
    device_instance: int = 512
    product_name: str = "Battery Aggregator"   # shown in the Venus GUI battery list
    product_id: int = 0xBA78                   # own id; 0xBA77 is used by dbus-serialbattery
    reserved_instances: tuple[int, ...] = (0, 99) + tuple(range(4, 29))
    enforce_system_settings: bool = False
    debug_mask: int = 0

    # ------------------------------------------------------------------
    def pack_by_serial(self, serial: str) -> PackConfig | None:
        for p in self.packs:
            if p.serial == serial:
                return p
        return None

    def validate(self) -> list[str]:
        """Return a list of errors; an empty list means the config is usable."""
        err: list[str] = []

        def num(name: str, lo: float, hi: float, allow_none: bool = False) -> None:
            v = getattr(self, name)
            if v is None:
                if not allow_none:
                    err.append(f"{name} must be set")
                return
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                err.append(f"{name} must be a finite number")
            elif not lo <= v <= hi:
                err.append(f"{name}={v} outside [{lo}, {hi}]")

        num("ccl_hw_a", 0, 2000, allow_none=True)
        num("dcl_hw_a", 0, 2000, allow_none=True)
        num("share_floor_equal_frac", 0, 1)
        num("dcl_emergency_a", 0, 500)
        num("commissioning_ccl_cap_a", 0, 2000)
        num("commissioning_dcl_cap_a", 0, 2000)
        num("v_cell_target", 3.30, 3.55)
        num("v_cell_soft", 3.35, 3.60)
        num("v_cell_hard", 3.40, 3.65)
        num("cvl_max_v_per_cell", 3.30, CVL_MAX_V_PER_CELL_LIMIT)
        num("cvl_reg_floor_v_per_cell", 3.0, CVL_MAX_V_PER_CELL_LIMIT)
        num("inverter_ccl_cap_a", 0, 2000, allow_none=True)
        num("failsafe_cvl_v_per_cell", 3.0, 3.45)
        num("v_cell_low_hard", 2.5, 3.1)
        num("v_cell_low_soft", 2.6, 3.2)
        num("t_charge_min_c", LFP_CHARGE_MIN_C, 15)
        num("t_charge_hot_stop_c", 20, LFP_CHARGE_MAX_C)
        if not (self.t_charge_min_c <= self.t_charge_full_c <= self.t_charge_hot_start_c
                <= self.t_charge_hot_stop_c):
            err.append("need t_charge_min_c <= t_charge_full_c <= t_charge_hot_start_c <= t_charge_hot_stop_c")
        if not (self.t_dchg_min_c <= self.t_dchg_full_c < self.t_dchg_hot_start_c <= self.t_dchg_hot_stop_c):
            err.append("need t_dchg_min_c <= t_dchg_full_c < t_dchg_hot_start_c <= t_dchg_hot_stop_c")
        if isinstance(self.cvl_max_v_per_cell, (int, float)):
            if self.failsafe_cvl_v_per_cell > self.cvl_max_v_per_cell:
                err.append("failsafe_cvl_v_per_cell must be <= cvl_max_v_per_cell")
            if not (self.cvl_min_v_per_cell <= self.cvl_reg_floor_v_per_cell <= self.cvl_max_v_per_cell):
                err.append("need cvl_min_v_per_cell <= cvl_reg_floor_v_per_cell <= cvl_max_v_per_cell")
        num("stale_after_s", 1, 120)
        num("restart_hold_s", 0, 120)
        num("restart_hold_max_age_s", 0, 900)
        num("offline_after_s", 2, 600)
        num("share_k", 0, 10)
        num("share_prior_factor", 1, 3)
        num("ramp_up_a_s", 0.1, 100)
        num("cells", 4, 32)
        if not (self.v_cell_target < self.v_cell_soft <= self.v_cell_hard):
            err.append("need v_cell_target < v_cell_soft <= v_cell_hard")
        if not (self.v_cell_low_hard < self.v_cell_low_soft):
            err.append("need v_cell_low_hard < v_cell_low_soft")
        if self.offline_after_s <= self.stale_after_s:
            err.append("offline_after_s must be > stale_after_s")
        if self.limit_control not in ("pi", "legacy", "sum"):
            err.append("limit_control must be 'pi', 'legacy' or 'sum'")
        num("react_tol", 0.0, 0.2)
        num("react_abs_tol_a", 0.0, 20.0)
        num("react_trip_s", 0.0, 30.0)
        num("react_release_ratio", 0.5, 1.0)
        num("react_release_s", 0.0, 60.0)
        num("react_down_interval_s", 0.0, 60.0)
        num("react_up_step_a", 0.5, 200.0)
        num("react_up_interval_s", 0.5, 120.0)
        num("ctl_setpoint", 0.5, 1.0)
        num("ctl_cut_ratio", 1.0, 1.2)
        num("ctl_cut_now_ratio", 1.05, 3.0)
        num("ctl_cut_gain", 0.1, 5.0)
        num("ctl_cut_min_factor", 0.0, 0.9)
        num("ctl_kp", 0.0, 5.0)
        num("ctl_ki", 0.0, 2.0)
        num("ctl_raise_a_s", 0.0, 50.0)
        num("ctl_raise_pct_s", 0.0, 20.0)
        num("ctl_hold_s", 0.0, 120.0)
        num("ctl_binding_frac", 0.5, 1.0)
        num("ctl_predict_frac", 0.1, 1.0)
        if self.n1_mode not in ("hw", "soft"):
            err.append("n1_mode must be 'hw' or 'soft'")
        if self.soc_mode not in ("weighted", "min", "guarded"):
            err.append("soc_mode must be weighted|min|guarded")
        if self.blind_charge_policy not in ("block", "last_temp_margin"):
            err.append("blind_charge_policy must be block|last_temp_margin")
        if self.device_instance in self.reserved_instances:
            err.append(f"device_instance {self.device_instance} collides with reserved/orphaned instances")
        seen: set[str] = set()
        labels: set[str] = set()
        for p in self.packs:
            if not p.serial or p.serial in seen:
                err.append(f"duplicate or empty serial {p.serial!r}")
            if p.label in labels:
                err.append(f"duplicate label {p.label!r}")
            seen.add(p.serial)
            labels.add(p.label)
            if not (1 <= p.capacity_ah <= 2000):
                err.append(f"pack {p.label}: capacity {p.capacity_ah} out of range")
            if not (4 <= p.cells <= 32):
                err.append(f"pack {p.label}: cells {p.cells} out of range")
            elif p.cells != self.cells:
                # bank CVL = cvl_max_v_per_cell x cells (M4) needs one series count
                err.append(f"pack {p.label}: cells {p.cells} != bank cells {self.cells}")
            for name in ("ocp_charge_a", "ocp_discharge_a"):
                v = getattr(p, name)
                if v is not None and not (1 <= v <= 2000):
                    err.append(f"pack {p.label}: {name}={v} out of range")
        if not self.shadow:
            # leaving shadow mode requires the commissioning inputs (DECISIONS.md D4)
            err.extend(f"commissioning input missing: {m}" for m in self.commissioning_missing())
        return err

    def commissioning_missing(self) -> list[str]:
        """Inputs that must be read on site before the aggregator may control the bank."""
        miss = []
        if self.ccl_hw_a is None:
            miss.append("ccl_hw_a (cables/fuses/busbar, charge)")
        if self.dcl_hw_a is None:
            miss.append("dcl_hw_a (cables/fuses/busbar, discharge)")
        if not self.packs:
            miss.append("packs (serial -> label/capacity)")
        for p in self.packs:
            if p.ocp_charge_a is None or p.ocp_discharge_a is None:
                miss.append(f"pack {p.label}: JK over-current trip (ocp_charge_a/ocp_discharge_a)")
        return miss

    def hw_cap(self, charge: bool) -> float:
        v = self.ccl_hw_a if charge else self.dcl_hw_a
        cap = float("inf") if v is None else float(v)
        if charge and self.inverter_ccl_cap_a is not None:
            cap = min(cap, float(self.inverter_ccl_cap_a))
        return cap

    def cvl_max_v(self) -> float:
        """Bank CVL ceiling: 3.55 V/cell x cells, never higher (M4)."""
        return self.cells * min(self.cvl_max_v_per_cell, CVL_MAX_V_PER_CELL_LIMIT)

    def with_(self, **kw: Any) -> "Config":
        return replace(self, **kw)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Config":
        known = {f.name for f in fields(Config)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        kw = dict(d)
        if "packs" in kw:
            kw["packs"] = tuple(PackConfig(**p) for p in kw["packs"])
        if "reserved_instances" in kw:
            kw["reserved_instances"] = tuple(kw["reserved_instances"])
        return Config(**kw)

    @staticmethod
    def load_json(path: str) -> "Config":
        with open(path, encoding="utf-8") as fh:
            return Config.from_dict(json.load(fh))


def default_field_names() -> list[str]:
    return [f.name for f in fields(Config)]


__all__ = ["Config", "PackConfig", "default_field_names", "CVL_MAX_V_PER_CELL_LIMIT"]
