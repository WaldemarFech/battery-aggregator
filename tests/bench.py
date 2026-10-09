"""Static test bench: real pack values from 04.10.2026 (docs/03_SZENARIEN.md)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from battery_aggregator import Config, Core, PackConfig, PackSnapshot  # noqa: E402
from battery_aggregator.model import CHARGE, DISCHARGE  # noqa: E402

SERIAL = {"P1": "JK-SN-0001", "P2": "JK-SN-0002", "P3": "JK-SN-0003"}
CAP = {"P1": 305.0, "P2": 314.0, "P3": 305.0}
REAL_DCL = {"P1": 43.6, "P2": 92.0, "P3": 59.0}
S_HAT = {"P1": 0.33, "P2": 0.34, "P3": 0.33}


def cfg(**kw) -> Config:
    base = dict(
        packs=tuple(PackConfig(SERIAL[l], l, CAP[l], 16, 250.0, 250.0) for l in ("P1", "P2", "P3")),
        expected_pack_count=3, ccl_hw_a=300.0, dcl_hw_a=300.0, dcl_emergency_a=90.0,
        inverter_ccl_cap_a=None,             # design numbers; owner inverter cap in test_review_fixes
        measured_shares_verified=True,
    )
    base.update(kw)
    return Config(**base)


def default_values(label: str) -> dict:
    return dict(voltage=48.5, current=0.0, temperature=20.0, soc={"P1": 8, "P2": 1, "P3": 13}.get(label, 20),
                installed_capacity=CAP.get(label, 280.0), cvl=56.8, ccl=200.0, dcl=REAL_DCL.get(label, 100.0),
                allow_charge=1, allow_discharge=1, cell_min=3.02, cell_max=3.04,
                cell_min_id=3, cell_max_id=9, cells=16, alarms={})


class Bench:
    """Drives a Core with per-pack value dicts, 1 s per step, jitter against frozen detection."""

    def __init__(self, config: Config | None = None, labels=("P1", "P2", "P3"), core: Core | None = None):
        self.cfg = config or cfg()
        self.core = core or Core(self.cfg)
        self.t = 0.0
        self.vals = {l: default_values(l) for l in labels}
        self.online = {l: True for l in labels}
        self.frozen = {l: False for l in labels}
        self.serial = {l: SERIAL.get(l, f"JK-SN-{l}") for l in labels}
        self.service = {l: f"com.victronenergy.battery.ttyUSB{i}" for i, l in enumerate(labels)}
        self._k = 0
        self.frozen_snap: dict = {}
        self.out = None
        self.hist: list = []

    def set(self, label: str, **kw) -> None:
        self.vals[label].update(kw)

    def bank_current(self, i_bank: float, shares=None) -> None:
        shares = shares or S_HAT
        for l in self.vals:
            self.vals[l]["current"] = i_bank * shares.get(l, 0.0)

    def snaps(self) -> list:
        res = []
        self._k += 1
        jit = 0.001 * (self._k % 2)
        for l, v in self.vals.items():
            if not self.online[l]:
                continue
            if self.frozen[l]:
                s = self.frozen_snap.setdefault(l, self._mk(l, v, 0.0))
                s.timestamp = self.t
                res.append(s)
                continue
            self.frozen_snap.pop(l, None)
            res.append(self._mk(l, v, jit))
        return res

    def _mk(self, l, v, jit) -> PackSnapshot:
        d = dict(v)
        if isinstance(d.get("voltage"), (int, float)) and not isinstance(d["voltage"], bool):
            d["voltage"] = d["voltage"] + jit
        return PackSnapshot(service=self.service[l], serial=self.serial[l], timestamp=self.t, **d)

    def run(self, seconds: int = 1, wall: float | None = 1.8e9):
        for _ in range(int(seconds)):
            self.out = self.core.step(self.snaps(), self.t, None if wall is None else wall + self.t)
            self.hist.append(self.out)
            self.t += 1.0
        return self.out

    def warm(self, seconds: int = 32):
        """Bring all packs to ACTIVE with zero current, then inject the scenario shares."""
        self.run(seconds)
        self.shares()
        return self.out

    def shares(self, s_hat=None, sigma: float = 0.01) -> None:
        s_hat = s_hat or S_HAT
        for l, s in s_hat.items():
            for d in (CHARGE, DISCHARGE):
                self.core.est.set_estimate(l, d, s, sigma, offset=0.0)

    def state(self, label: str) -> str:
        return self.core.tracks[self.serial[label]].state.value

    def track(self, label: str):
        return self.core.tracks[self.serial[label]]
