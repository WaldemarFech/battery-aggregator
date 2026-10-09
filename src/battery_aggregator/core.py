"""Core: step(snapshots, now) -> BankOutputs. Pure Python, no D-Bus, no clock access.

`now` is a monotonic time in seconds (SR-13); `wall` (optional) is only used for the
calibration age. The core never writes anything anywhere (SR-14).
"""
from __future__ import annotations


from . import alarms as alarm_mod
from . import soc as soc_mod
from .config import Config
from .control import LimitController
from .cvl import CellRegulator, CvlInput, compute_cvl, static_cell_cap
from .evidence import update_paths
from .limits import LimitResult, Member, bank_limit, pack_limit
from .model import CHARGE, DIRECTIONS, DISCHARGE, BankMode, PackSnapshot, PackState
from .outputs import BankOutputs, PackStatus
from .pack_state import PackTrack, data_for, ingest, role, route_snapshots, update_state
from .ratelimit import Feedback, Ramp
from .restart_hold import RestartHold, hold_cancel_reason
from .shares import ShareEstimator
from .sum_mode import ReactiveCut, source_limit, sum_apply, sum_reason


def _median(values):
    """Median without the stdlib `statistics` module (not shipped on Venus OS)."""
    data = sorted(values)
    n = len(data)
    if n == 0:
        raise ValueError("median of empty data")
    mid = n // 2
    return data[mid] if n % 2 else (data[mid - 1] + data[mid]) / 2


CVL_SEED = "cvl"


class Core:
    def __init__(self, cfg: Config, persisted: dict | None = None) -> None:
        errors = cfg.validate()
        if errors:
            raise ValueError("invalid config: " + "; ".join(errors))
        self.cfg = cfg
        self.tracks: dict[str, PackTrack] = {}
        for p in cfg.packs:
            self.tracks[p.serial] = PackTrack(p.serial, p.label, p.capacity_ah, p.cells,
                                              p.ocp_charge_a, p.ocp_discharge_a,
                                              quarantine_s=cfg.quarantine_s)
        self.est = ShareEstimator(cfg)
        self.fb = {d: Feedback(cfg) for d in DIRECTIONS}            # limit_control = "legacy"
        self.ctl = {d: LimitController(cfg, d) for d in DIRECTIONS}  # limit_control = "pi"
        self.react = {d: ReactiveCut.from_cfg(cfg) for d in DIRECTIONS}  # limit_control = "sum"
        self.meas: dict = {}
        self.seeded: set = set()             # ramps seeded from live values after a restart
        fs_dcl = self._failsafe_dcl_base()
        self.ramps = {
            CHARGE: Ramp(0.0, cfg.ramp_up_a_s, cfg.ramp_up_pct_s, cfg.ramp_pct_base_a,
                         cfg.hold_after_drop_s, cfg.limit_deadband_a),
            DISCHARGE: Ramp(fs_dcl, cfg.ramp_up_a_s, cfg.ramp_up_pct_s, cfg.ramp_pct_base_a,
                            cfg.hold_after_drop_s, cfg.limit_deadband_a),
        }
        self.cvl_ramp = Ramp(cfg.cells * cfg.failsafe_cvl_v_per_cell, cfg.cvl_ramp_v_s, 0.0, 0.0,
                             cfg.cvl_hold_after_drop_s, cfg.cvl_deadband_v)
        self.cell_reg = CellRegulator()
        self.svc_serial: dict[str, str] = {}   # M1: sticky service -> serial binding
        self.conflicts: dict[str, str] = {}    # serial -> identity-conflict reason (this step)
        self.soc_slew = soc_mod.SocSlew(cfg)
        self.calib = soc_mod.Calibration(cfg)
        self.start_t: float | None = None
        self.ever_active = False
        self.prev_ref: set[str] = set()
        self.prev_seff: dict[str, dict[str, float]] = {}
        self.fs_t0: float | None = None
        self.fs_dcl0 = fs_dcl
        self.warned: set[str] = set()
        self.last: BankOutputs | None = None
        self.overrun_since: float | None = None
        self.hold = RestartHold(cfg)         # design 5.3a: no charge stop across a restart
        self.out_rec: dict | None = None     # last NORMAL/DEGRADED outputs + wall time (persisted)
        if persisted:
            self.load_state(persisted)

    # ------------------------------------------------------------------ persistence
    def export_state(self) -> dict:
        data = {"version": 1, "estimator": self.est.to_dict(),
                "t_last_full": {t.serial: t.t_last_full for t in self.tracks.values() if t.t_last_full}}
        if self.out_rec is not None:
            data["outputs"] = dict(self.out_rec)
        return data

    def load_state(self, data: dict) -> None:
        """Corrupt or foreign data is ignored (persistence is not safety relevant)."""
        try:
            if not isinstance(data, dict) or data.get("version") != 1:
                return
            self.hold = RestartHold(self.cfg, data.get("outputs"))
            self.out_rec = self.hold.rec
            self.est.load_dict(data.get("estimator", {}))
            for serial, t in (data.get("t_last_full") or {}).items():
                if serial in self.tracks and isinstance(t, (int, float)):
                    self.tracks[serial].t_last_full = float(t)
        except (AttributeError, TypeError, ValueError):
            self.est = ShareEstimator(self.cfg)

    def set_excluded(self, serial: str, excluded: bool) -> None:
        if serial in self.tracks:
            self.tracks[serial].excluded = excluded

    def clamp_published(self, ccl: float, dcl: float, cvl: float, now: float) -> None:
        """Called by the fault wrapper: never jump above what was actually published."""
        self.ramps[CHARGE].force(ccl, now)
        self.ramps[DISCHARGE].force(dcl, now)
        self.cvl_ramp.force(cvl, now)
        self.ctl[CHARGE].force(ccl, now)
        self.ctl[DISCHARGE].force(dcl, now)
        self.react[CHARGE].force(ccl)
        self.react[DISCHARGE].force(dcl)

    # ------------------------------------------------------------------ helpers
    def _failsafe_dcl_base(self) -> float:
        v = self.cfg.dcl_emergency_a
        if not self.cfg.measured_shares_verified:
            v = min(v, self.cfg.commissioning_dcl_cap_a)
        return min(v, self.cfg.hw_cap(False))

    def _route(self, snapshots, now: float, events: list) -> dict[str, PackSnapshot]:
        return route_snapshots(self, snapshots, now, events)

    @staticmethod
    def _median_excluding(volts: dict[str, float], label: str) -> float | None:
        others = [v for k, v in volts.items() if k != label]
        return _median(others) if others else None

    # ------------------------------------------------------------------ step
    def step(self, snapshots, now: float, wall: float | None = None) -> BankOutputs:
        cfg = self.cfg
        events: list = []
        if self.start_t is None:
            self.start_t = now
            self.hold.begin(now, wall, events)
        routed = self._route(snapshots, now, events)
        tracks = list(self.tracks.values())
        for t in tracks:
            ingest(t, routed.get(t.serial), now, cfg, self.conflicts.get(t.serial))

        live_v = {t.label: t.valid.voltage for t in tracks
                  if t.state == PackState.ACTIVE and t.valid is not None}
        for t in tracks:
            update_state(t, now, cfg, self._median_excluding(live_v, t.label), events)

        roles = {t.label: role(t, now, cfg) for t in tracks}
        for t in tracks:
            if roles[t.label] != "active":
                # B1: without live explicit FET data the path state is unknown -> closed,
                # i.e. the pack keeps constraining the bank with its (held) limit
                t.chg_open = t.dchg_open = False
        active = [t for t in tracks if roles[t.label] == "active"]
        if active:
            self.ever_active = True
        i_bank = sum(t.valid.current for t in active)
        live_v = {t.label: t.valid.voltage for t in active}
        update_paths(active, i_bank, {t.label: self._median_excluding(live_v, t.label) for t in active},
                     now, cfg, events)

        # reference set: admitted packs that still belong to the bank
        ref = [t for t in tracks if t.admitted and t.state not in (PackState.GONE, PackState.EXCLUDED)]
        ref_labels = {t.label for t in ref}
        for gone in self.prev_ref - ref_labels:
            if gone in self.prev_seff:
                remaining = {l: self.prev_seff[l] for l in ref_labels if l in self.prev_seff}
                self.est.on_removed(gone, remaining, self.prev_seff[gone])
                events.append(("info", gone, "left reference set, shares renormalised"))
        self.prev_ref = ref_labels

        # share learning only with complete topology
        complete = bool(ref) and all(roles[t.label] == "active" and not t.disconnected for t in ref)
        if complete:
            self.est.update(now, {t.label: t.valid.current for t in ref},
                            {CHARGE: not any(t.chg_open for t in ref),
                             DISCHARGE: not any(t.dchg_open for t in ref)})
        else:
            self.est.prev_t = None

        # priors and conservative shares
        base = ref if ref else [t for t in tracks if roles[t.label] != "none"]
        base_labels = {t.label for t in base}
        cap_base = sum(t.capacity() for t in base)
        seff: dict[str, dict[str, float]] = {}
        for t in tracks:
            if roles[t.label] == "none":
                continue
            c = t.capacity()
            denom = cap_base + (0.0 if t.label in base_labels else c)
            prior = c / denom if denom > 0 and c > 0 else 1.0
            seff[t.label] = {d: self.est.s_eff(t.label, d, prior, max(1, len(base))) for d in DIRECTIONS}
        self.prev_seff = {l: v for l, v in seff.items() if l in ref_labels}

        # members per direction
        results = {}
        pack_lims: dict[str, dict[str, float]] = {}
        blind_ref = [t for t in ref if roles[t.label] == "blind"]
        summing = cfg.limit_control == "sum"
        for d in DIRECTIONS:
            act, unc = [], []
            for t in tracks:
                r = roles[t.label]
                if r == "blind" and t.admitted and t.last_valid is not None:
                    r = "uncertain"   # SR-02: held values may only tighten (world A) until GONE
                if r not in ("active", "uncertain") or t.disconnected:
                    continue
                if (t.chg_open if d == CHARGE else t.dchg_open):
                    continue
                v = data_for(t)
                lim, why = (source_limit if summing else pack_limit)(v, d, cfg, t.hv_latched)
                pack_lims.setdefault(t.label, {})[d] = lim
                off = self.est.offset(t.label)
                off_d = max(0.0, off) if d == CHARGE else max(0.0, -off)
                ocp = t.ocp_charge_a if d == CHARGE else t.ocp_discharge_a
                m = Member(t.label, lim, seff[t.label][d], off_d, ocp, r, why)
                (act if r == "active" else unc).append(m)
            ref_sh = {t.label: seff[t.label][d] for t in ref if t.label in seff}
            hw = cfg.hw_cap(d == CHARGE)
            if summing:
                # only live packs count (a lost pack drops out); held values of uncertain packs
                # only while NO pack is live (stale grace), never a DCL 0 from a comms glitch
                used = act or unc
                total = sum(m.limit for m in used)
                res = LimitResult(total, sum_reason(used) + ("" if act else " (held)"), total, total,
                                  float("inf"), ref_sh)
                if hw < res.value:
                    res = LimitResult(hw, f"hardware cap ({res.reason})", total, total, float("inf"), ref_sh)
                res.ceiling, res.floor = res.value, 0.0
                results[d] = (res, act, [])
                continue
            results[d] = (bank_limit(act, unc, bool(blind_ref), ref_sh, hw, cfg), act, unc)

        # SR-04/SR-07: packs without live temperature must not be charged blindly
        blind_block = None
        for t in ([] if summing else ref):
            if roles[t.label] == "active" or t.chg_open or t.disconnected:
                continue
            lv = t.last_valid
            if cfg.blind_charge_policy == "block":
                blind_block = f"{t.label} blind: charge blocked"
            elif lv is None or lv.temp_min is None or lv.temp_min < cfg.t_charge_min_c + cfg.blind_temp_margin_k:
                blind_block = f"{t.label} blind and last temperature unknown/cold: charge blocked"
            elif lv.temp_max is not None and lv.temp_max > cfg.t_charge_hot_start_c:
                blind_block = f"{t.label} blind and last temperature hot: charge blocked"

        # measured per-pack utilisation I_i / L_i -> closed loop (control.py) or legacy feedback
        for d in DIRECTIONS:
            _, act, unc = results[d]
            ratios = []
            sign = 1.0 if d == CHARGE else -1.0
            for m in act:
                cur = sign * self.tracks_by_label(m.label).valid.current
                if cur > 0:
                    ratios.append((m.label, cur / m.limit if m.limit > 0.1 else (10.0 if cur > 1.0 else 0.0)))
            age = max((now - (self.tracks_by_label(m.label).last_valid_t or -1e18) for m in act), default=0.0)
            self.meas[d] = (ratios, sign * i_bank, [(m.label, m.role) for m in act + unc],
                            age <= cfg.ctl_max_age_s and not unc, age <= cfg.ctl_flow_age_s)
            if cfg.limit_control == "legacy":
                self.fb[d].update(now, ratios, sign * i_bank > 0, events, d)

        self._overrun_check(i_bank, now, events)

        # CVL
        cvl_in = []
        for t in tracks:
            r = roles[t.label]
            if r == "none" or t.disconnected or t.chg_open:
                continue
            if r == "active" or (r == "uncertain" and t.state == PackState.QUARANTINE):
                v = data_for(t)       # live, fresh values
                cvl_in.append(CvlInput(t.label, v.cvl, v.cell_max, v.cell_max_id))
            elif t.last_valid is not None and t.admitted:
                lv = t.last_valid     # M2: held values never drive the regulator
                cvl_in.append(CvlInput(t.label, lv.cvl, None,
                                       static_cap=static_cell_cap(lv.voltage, lv.cell_max, cfg)))
        bus_src = [t.valid.voltage for t in active] or [data_for(t).voltage for t in tracks
                                                         if roles[t.label] == "uncertain"]
        v_bus = _median(bus_src) if bus_src else None
        cal_cap = self.calib.cap(tracks, now, wall, i_bank, events)
        cvl_res = compute_cvl(cvl_in, v_bus, cfg, cal_cap, self.cell_reg, i_bank)

        # mode
        stale_any = any(t.state == PackState.STALE for t in ref)
        uncertain_any = any(roles[t.label] == "uncertain" for t in tracks)
        if active:
            ok_count = cfg.expected_pack_count is None or len(ref) == cfg.expected_pack_count
            all_ok = all(roles[t.label] == "active" for t in tracks if roles[t.label] != "none")
            mode = BankMode.NORMAL if all_ok and ok_count else BankMode.DEGRADED
        elif stale_any and self.ever_active:
            mode = BankMode.DEGRADED          # STALE grace: held values (design 5.3 step 1)
        elif not self.ever_active and now - self.start_t < cfg.init_timeout_s:
            mode = BankMode.INIT
        else:
            mode = BankMode.FAILSAFE

        out = self._finish(mode, results, cvl_res, blind_block, stale_any, uncertain_any, now, events,
                           hold_cancel_reason(tracks, roles, blind_block) if self.hold.active else None)
        self._fill_measurements(out, tracks, roles, now, wall, v_bus, i_bank, events, seff, pack_lims)
        if mode in (BankMode.NORMAL, BankMode.DEGRADED) and wall is not None:
            self.out_rec = {"ccl": out.ccl, "dcl": out.dcl, "cvl": out.cvl, "wall": float(wall)}
        out.events = events
        self.last = out
        return out

    def _overrun_check(self, i_bank: float, now: float, events: list) -> None:
        """Bank current beyond the published limit (island load, non-DVCC sources, S39)."""
        if self.last is None:
            return
        over = (i_bank > 1.05 * self.last.ccl + 1.0) or (-i_bank > 1.05 * self.last.dcl + 1.0)
        if not over:
            self.overrun_since = None
            return
        if self.overrun_since is None:
            self.overrun_since = now
        elif now - self.overrun_since >= self.cfg.fb_trip_s:
            lim = "CCL" if i_bank > 0 else "DCL"
            events.append(("warn", "bank", f"{lim} exceeded: bank {i_bank:+.0f}A (not enforceable, "
                                           f"BMS/fuse protection only)"))
            self.overrun_since = now + 60.0

    def tracks_by_label(self, label: str) -> PackTrack:
        for t in self.tracks.values():
            if t.label == label:
                return t
        raise KeyError(label)

    # ------------------------------------------------------------------ limits -> published
    def _finish(self, mode, results, cvl_res, blind_block, stale_any, uncertain_any, now, events,
                hold_cancel=None) -> BankOutputs:
        cfg = self.cfg
        res_c, res_d = results[CHARGE][0], results[DISCHARGE][0]
        tgt = {CHARGE: res_c.value, DISCHARGE: res_d.value}
        why = {CHARGE: res_c.reason, DISCHARGE: res_d.reason}
        if blind_block and tgt[CHARGE] > 0:
            tgt[CHARGE], why[CHARGE] = 0.0, blind_block
        dcl_model = tgt[DISCHARGE]            # restart hold DCL: no SR-21 start-up cap (D8)
        # SR-21: with the closed loop the measured per-pack utilisation protects every pack, so the
        # static commissioning cap only applies while that measurement is missing/stale (or legacy)
        live = {d: cfg.limit_control == "pi" and not stale_any and self.meas.get(d, (0, 0, 0, False))[3]
                for d in DIRECTIONS}
        if cfg.limit_control == "sum":   # cap only without live pack data
            live = {d: bool(results[d][1]) and self.meas.get(d, (0, 0, 0, False))[3] for d in DIRECTIONS}
        if not cfg.measured_shares_verified:
            caps = {CHARGE: cfg.commissioning_ccl_cap_a, DISCHARGE: cfg.commissioning_dcl_cap_a}
            for d in DIRECTIONS:
                if tgt[d] > caps[d] and not live[d]:
                    tgt[d], why[d] = caps[d], f"SR-21 commissioning cap {caps[d]:.0f}A (model {tgt[d]:.1f}A)"
        ceil = {d: results[d][0].ceiling for d in DIRECTIONS}
        if blind_block: ceil[CHARGE] = 0.0  # noqa: E701
        for d in DIRECTIONS:
            k = self.fb[d].k
            if k < 1.0 and cfg.limit_control == "legacy":
                tgt[d] *= k
                why[d] += f" x feedback {k:.2f}"
        cvl_t, cvl_why = cvl_res.value, cvl_res.reason
        tgt_live = {**tgt, DISCHARGE: dcl_model}  # live guards; clamp the restart hold (feedback k=1 in INIT)
        held = self.hold.apply(now, mode, tgt_live, cvl_res.value, hold_cancel, events)

        if mode in (BankMode.INIT, BankMode.FAILSAFE):
            if self.fs_t0 is None:
                self.fs_t0 = now
                self.fs_dcl0 = self.ramps[DISCHARGE].value
                if mode == BankMode.FAILSAFE:
                    events.append(("alarm", "bank", "FAILSAFE: no pack data, CCL 0"))
            fs = self._failsafe_dcl_base()
            if cfg.failsafe_dcl_never_raise:
                fs = min(fs, self.fs_dcl0)
            if self.fs_dcl0 > fs and cfg.failsafe_dcl_ramp_s > 0:
                frac = min(1.0, (now - self.fs_t0) / cfg.failsafe_dcl_ramp_s)
                fs_now = self.fs_dcl0 - (self.fs_dcl0 - fs) * frac
            else:
                fs_now = fs
            if uncertain_any and res_d.value < fs_now:
                fs_now, why[DISCHARGE] = res_d.value, res_d.reason
            else:
                why[DISCHARGE] = f"{mode.value}: emergency DCL {fs:.0f}A"
            tgt = {CHARGE: 0.0, DISCHARGE: fs_now}
            why[CHARGE] = f"{mode.value}: no charging without pack data"
            cvl_t = min(cfg.cells * cfg.failsafe_cvl_v_per_cell, cvl_res.value if uncertain_any else 1e9)
            cvl_why = f"{mode.value}: conservative CVL"
            allow_up = False
            if held is not None:
                # design 5.3a: hold the persisted outputs, only ever lowered by the live model
                tgt, cvl_t = {CHARGE: held["ccl"], DISCHARGE: held["dcl"]}, held["cvl"]
                for d in DIRECTIONS:
                    self.ramps[d].value = tgt[d]
                    why[d] = f"restart hold {tgt[d]:.1f}A (live model {tgt_live[d]:.1f}A)"
                self.cvl_ramp.value = cvl_t
                cvl_why = f"restart hold {cvl_t:.2f}V (live {cvl_res.value:.2f}V)"
        else:
            self.fs_t0 = None
            allow_up = not stale_any
        if mode not in (BankMode.INIT, BankMode.FAILSAFE):
            # restart: start from the packs' effective values, not from the floor (else every
            # service restart cuts charging for minutes); only ever upwards to the live target
            if CVL_SEED not in self.seeded:
                self.seeded.add(CVL_SEED)
                self.cvl_ramp.value = max(self.cvl_ramp.value, min(cvl_t, cfg.cvl_max_v()))
            for d in DIRECTIONS:
                if live[d] and d not in self.seeded:
                    self.seeded.add(d)
                    self.ramps[d].value = max(self.ramps[d].value, tgt[d])
        if cfg.limit_control == "sum" and mode not in (BankMode.INIT, BankMode.FAILSAFE):
            ccl, dcl = self._sum_apply(tgt, why, results, now, events)
        else:
            ccl = self.ramps[CHARGE].step(tgt[CHARGE], now, allow_up)
            dcl = self.ramps[DISCHARGE].step(tgt[DISCHARGE], now, allow_up)
        kfb = {d: self.fb[d].k for d in DIRECTIONS}
        if cfg.limit_control == "pi" and mode not in (BankMode.INIT, BankMode.FAILSAFE):
            ramped = {CHARGE: ccl, DISCHARGE: dcl}
            for d in DIRECTIONS:
                ratios, flow, members, live, flow_ok = self.meas.get(d, ([], 0.0, [], False, False))
                v = self.ctl[d].update(now, anchor=ramped[d], ceiling=ceil[d], floor=results[d][0].floor,
                                       live=live and allow_up, members=members, ratios=ratios,
                                       flow=flow, events=events, flow_ok=flow_ok)
                kfb[d] = min(1.0, v / ramped[d]) if ramped[d] > 0 else 1.0
                if abs(v - ramped[d]) > 0.05:
                    why[d] += f" -> closed loop {v:.1f}A ({self.ctl[d].state}, u {self.ctl[d].u_pred:.2f})"
                ramped[d] = v
            ccl, dcl = ramped[CHARGE], ramped[DISCHARGE]
        cvl = self.cvl_ramp.step(cvl_t, now, allow_up)

        out = BankOutputs(mode=mode, ccl=float(ccl), dcl=float(dcl), cvl=float(cvl), soc=None, soc_raw=None,
                          soc_weighted=None, soc_min=None, soc_min_pack=None, capacity_ah=0.0,
                          installed_capacity_ah=0.0, consumed_ah=0.0, voltage=None, current=None,
                          power=None, temperature=None)
        out.reason_charge, out.reason_discharge, out.reason_voltage = why[CHARGE], why[DISCHARGE], cvl_why
        out.model_ccl, out.model_dcl = res_c.value, res_d.value
        out.term_ccl, out.term_dcl = res_c.model, res_d.model
        out.n1_ccl, out.n1_dcl = res_c.n1, res_d.n1
        out.target_ccl, out.target_dcl, out.target_cvl = tgt[CHARGE], tgt[DISCHARGE], cvl_t
        out.k_fb_charge, out.k_fb_discharge = kfb[CHARGE], kfb[DISCHARGE]
        out.shares_charge, out.shares_discharge = res_c.shares, res_d.shares
        # SR-05 output validation
        n_packs = max(1, len(self.tracks))
        hi_c = min(cfg.hw_cap(True), cfg.pack_limit_max_a * n_packs)
        hi_d = min(cfg.hw_cap(False), cfg.pack_limit_max_a * n_packs)
        cvl_lo, cvl_hi = cfg.cells * cfg.cvl_min_v_per_cell, cfg.cvl_max_v()
        if out.check() or out.ccl > hi_c or out.dcl > hi_d or not cvl_lo <= out.cvl <= cvl_hi:
            events.append(("alarm", "bank", f"output validation failed ({out.ccl}, {out.dcl}, {out.cvl})"))
            out.ccl = 0.0
            out.dcl = float(min(self._failsafe_dcl_base(), max(0.0, out.dcl if out.dcl == out.dcl else 0.0), hi_d))
            out.cvl = cfg.cells * cfg.failsafe_cvl_v_per_cell
            self.clamp_published(out.ccl, out.dcl, out.cvl, now)
            out.alarms["InternalFailure"] = 1
        out.allow_to_charge = out.ccl > 0.0
        out.allow_to_discharge = out.dcl > 0.0
        return out

    def _sum_apply(self, tgt, why, results, now, events) -> tuple[float, float]:
        return sum_apply(self, tgt, why, results, now, events)

    # ------------------------------------------------------------------ measurements
    def _fill_measurements(self, out, tracks, roles, now, wall, v_bus, i_bank, events, seff, pack_lims) -> None:
        cfg = self.cfg
        live = [(t.label, t.valid) for t in tracks if roles[t.label] == "active"]
        held = [(t.label, t.last_valid) for t in tracks
                if t.state == PackState.STALE and t.last_valid is not None]
        # quarantined packs deliver fresh, validated data: measure with them too (display/DVCC
        # voltage+current only, never limits), so /Dc/0/* is not empty for 30 s after a restart
        quar = [(t.label, t.valid) for t in tracks if t.state == PackState.QUARANTINE
                and t.valid is not None and t.last_valid_t is not None
                and now - t.last_valid_t <= cfg.stale_after_s]
        meas = live + held + quar
        if meas:
            out.voltage = _median(v.voltage for _, v in meas)
            out.current = sum(v.current for _, v in meas)
            out.power = out.voltage * out.current
        out.current_incomplete = any(roles[t.label] == "blind" for t in tracks if t.admitted)
        ext = alarm_mod.extremes(meas)
        for f in ("cell_min", "cell_min_id", "cell_max", "cell_max_id", "temp_min", "temp_min_id",
                  "temp_max", "temp_max_id"):
            setattr(out, f, getattr(ext, f))
        out.temperature = ext.temp_max

        # SoC
        soc_mod.update_plausibility(tracks, now, cfg, events)
        self.calib.observe_full(tracks, wall, v_bus, out.cvl, events)
        items = []
        for t in tracks:
            if t.state in (PackState.GONE, PackState.EXCLUDED) or not (t.admitted or t.state == PackState.ACTIVE):
                continue
            v = data_for(t)
            items.append(soc_mod.SocItem(t.label, v.soc if v else None, t.capacity(), t.soc_suspect))
        sres = soc_mod.aggregate(items, cfg)
        out.soc_raw, out.soc_weighted = sres.raw, sres.weighted
        out.soc_min, out.soc_min_pack = sres.min_soc, sres.min_label
        out.soc = self.soc_slew.step(sres.raw, now)
        out.installed_capacity_ah = sres.installed_ah
        out.capacity_ah = sres.remaining_ah
        out.consumed_ah = -(sres.installed_ah - sres.remaining_ah)
        out.calibration_recommended = self.calib.due(tracks, wall) or any(t.soc_suspect for t in tracks)
        out.calibration_active = self.calib.active_since is not None

        # alarms
        al = alarm_mod.aggregate(meas)
        ref_bad = [t for t in tracks if t.admitted and t.state not in
                   (PackState.ACTIVE, PackState.GONE, PackState.EXCLUDED)]
        if out.mode == BankMode.FAILSAFE:
            al.raise_("BmsCable", 2, "bank")
        elif ref_bad:
            al.raise_("BmsCable", 1, ",".join(t.label for t in ref_bad))
        n_ref = len([t for t in tracks if t.admitted and t.state not in (PackState.GONE, PackState.EXCLUDED)])
        if cfg.expected_pack_count is not None and self.ever_active and n_ref != cfg.expected_pack_count:
            al.raise_("BmsCable", 1, "pack count")
        if sres.spread > cfg.imbalance_dsoc_pct:
            al.raise_("CellImbalance", 1, "soc spread")
        for k, v in out.alarms.items():
            al.raise_(k, v, "core")
        out.alarms = dict(al.levels)

        out.nr_online = sum(1 for t in tracks if t.state == PackState.ACTIVE)
        out.nr_offline = sum(1 for t in tracks if t.admitted and t.state in
                             (PackState.STALE, PackState.OFFLINE, PackState.UNTRUSTED, PackState.QUARANTINE))
        out.nr_blocking_charge = sum(1 for t in tracks if roles[t.label] != "none" and (
            t.chg_open or pack_lims.get(t.label, {}).get(CHARGE, 1.0) <= 0.0))
        out.nr_blocking_discharge = sum(1 for t in tracks if roles[t.label] != "none" and (
            t.dchg_open or pack_lims.get(t.label, {}).get(DISCHARGE, 1.0) <= 0.0))
        for t in tracks:
            v = data_for(t)
            out.packs.append(PackStatus(
                t.label, t.serial, t.service, t.state.value, roles[t.label],
                v.soc if v else None, v.current if v else None,
                seff.get(t.label, {}).get(CHARGE), seff.get(t.label, {}).get(DISCHARGE),
                pack_lims.get(t.label, {}).get(CHARGE), pack_lims.get(t.label, {}).get(DISCHARGE),
                v.cvl if v else None, t.soc_suspect, t.chg_open, t.dchg_open, t.disconnected, t.problems))
