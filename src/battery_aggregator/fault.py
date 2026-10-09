"""Fault wrapper around Core.step (design 5.4 FAULT, S33, FMEA F1/F2).

A programming error in the core never kills the main loop: the last outputs are kept with
CCL halved, after fault_failsafe_after_s the failsafe values are published, and after
fault_restart_after_s without recovery `restart_requested` asks the adapter to exit so the
supervisor (daemontools) restarts the process.
"""
from __future__ import annotations

import copy
import logging
import traceback

from .core import Core
from .model import BankMode
from .outputs import BankOutputs

log = logging.getLogger("battery_aggregator")


class SafeCore:
    def __init__(self, core: Core) -> None:
        self.core = core
        self.fault_since: float | None = None
        self.fault_count = 0
        self.last_good: BankOutputs | None = None
        self.last_out: BankOutputs | None = None

    def step(self, snapshots, now: float, wall: float | None = None) -> BankOutputs:
        try:
            out = self.core.step(snapshots, now, wall)
            if self.fault_since is not None and self.last_out is not None:
                # recover without jumping above what the fault path published
                self.core.clamp_published(self.last_out.ccl, self.last_out.dcl, self.last_out.cvl, now)
                out.ccl = min(out.ccl, self.last_out.ccl)
                out.dcl = min(out.dcl, self.last_out.dcl)
                out.cvl = min(out.cvl, self.last_out.cvl)
                out.allow_to_charge = out.ccl > 0
                out.allow_to_discharge = out.dcl > 0
                out.events.append(("info", "bank", "core recovered from fault"))
            self.fault_since = None
            self.last_good = out
            self.last_out = out
            return out
        except Exception:  # noqa: BLE001 - deliberate: isolate programming errors
            self.fault_count += 1
            tb = traceback.format_exc()
            log.error("core step failed (#%d):\n%s", self.fault_count, tb)
            return self._fault(now, tb)

    def _fault(self, now: float, tb: str) -> BankOutputs:
        cfg = self.core.cfg
        first = self.fault_since is None
        if first:
            self.fault_since = now
        base = self.last_out or self.last_good
        if base is None:
            out = BankOutputs(mode=BankMode.FAULT, ccl=0.0, dcl=self.core._failsafe_dcl_base(),
                              cvl=cfg.cells * cfg.failsafe_cvl_v_per_cell, soc=None, soc_raw=None,
                              soc_weighted=None, soc_min=None, soc_min_pack=None, capacity_ah=0.0,
                              installed_capacity_ah=0.0, consumed_ah=0.0, voltage=None, current=None,
                              power=None, temperature=None)
        else:
            out = copy.deepcopy(base)
            out.mode = BankMode.FAULT
            out.events = []
            if first:
                out.ccl = base.ccl / 2.0
        elapsed = now - self.fault_since
        if elapsed >= cfg.fault_failsafe_after_s:
            out.ccl = 0.0
            out.dcl = min(out.dcl, self.core._failsafe_dcl_base())
            out.cvl = min(out.cvl, cfg.cells * cfg.failsafe_cvl_v_per_cell)
        out.restart_requested = elapsed >= cfg.fault_restart_after_s
        out.alarms = dict(out.alarms)
        out.alarms["InternalFailure"] = 2
        out.allow_to_charge = out.ccl > 0
        out.allow_to_discharge = out.dcl > 0
        out.reason_charge = "FAULT: core exception"
        out.events.append(("alarm", "bank", "core exception: " + tb.strip().splitlines()[-1]))
        self.last_out = out
        return out
