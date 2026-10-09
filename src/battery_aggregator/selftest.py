"""On-device self test without pytest (install.sh): python3 -m battery_aggregator.selftest

Reproduces key scenario numbers through the real core on the target Python. Exit 0 = ok.
"""
from __future__ import annotations

import sys

from .config import Config, PackConfig
from .core import Core
from .model import CHARGE, DISCHARGE, BankMode, PackSnapshot

CAP = {"P1": 305.0, "P2": 314.0, "P3": 305.0}
DCL = {"P1": 43.6, "P2": 92.0, "P3": 59.0}


def _cfg(**kw) -> Config:
    base = dict(packs=tuple(PackConfig(f"SN{l}", l, CAP[l], 16, 250.0, 250.0) for l in CAP),
                ccl_hw_a=300.0, dcl_hw_a=300.0, inverter_ccl_cap_a=None, measured_shares_verified=True,
                expected_pack_count=3)
    base.update(kw)
    return Config(**base)


def _snaps(t: float, online=("P1", "P2", "P3"), current=0.0):
    share = {"P1": 0.33, "P2": 0.34, "P3": 0.33}
    return [PackSnapshot(service=f"com.victronenergy.battery.{l}", serial=f"SN{l}", timestamp=t,
                         voltage=48.5 + 0.001 * (int(t) % 2), current=current * share[l], temperature=20.0,
                         soc={"P1": 8, "P2": 1, "P3": 13}[l], installed_capacity=CAP[l], cvl=56.8, ccl=200.0,
                         dcl=DCL[l], allow_charge=1, allow_discharge=1, cell_min=3.02, cell_max=3.04, cells=16)
            for l in online]


def run() -> list[str]:
    fails = []

    def check(name, cond, detail=""):
        print(("OK   " if cond else "FAIL ") + name + (f" ({detail})" if detail else ""))
        if not cond:
            fails.append(name)

    core = Core(_cfg())
    t = 0.0
    for t in range(33):
        core.step(_snaps(float(t)), float(t))
    for l, s in {"P1": 0.33, "P2": 0.34, "P3": 0.33}.items():
        for d in (CHARGE, DISCHARGE):
            core.est.set_estimate(l, d, s, 0.01, offset=0.0)
    o = core.step(_snaps(33.0, current=-100.0), 33.0)
    check("S02 DCL model 124.6 A", abs(o.model_dcl - 124.57) < 0.1, f"{o.model_dcl:.2f}")
    check("S19 SoC 7.27 %", o.soc_raw is not None and abs(o.soc_raw - 7.27) < 0.01, f"{o.soc_raw}")
    for k in range(34, 70):
        o = core.step(_snaps(float(k), online=("P1", "P2"), current=-100.0), float(k))
    check("S22 N-1 DCL 81 A", abs(o.model_dcl - 43.6 / (0.35 / 0.65)) < 0.5, f"{o.model_dcl:.2f}")
    for k in range(70, 110):
        o = core.step([], float(k))
    check("S28 FAILSAFE CCL 0", o.mode == BankMode.FAILSAFE and o.ccl == 0.0, f"{o.mode} {o.ccl}")
    capped = Core(_cfg(measured_shares_verified=False))
    for k in range(40):
        o = capped.step(_snaps(float(k), current=-100.0), float(k))
    check("SR-21 lifted with live per-pack currents", o.target_dcl > 59.0, f"{o.target_dcl}")
    for k in range(40, 45):
        o = capped.step(_snaps(float(k), online=("P1", "P2"), current=-100.0), float(k))
    check("SR-21 cap DCL <= 59 A while a pack is not measured", o.dcl <= 59.0 and o.target_dcl <= 59.0,
          f"{o.dcl}")
    try:
        o = capped.step([PackSnapshot(service="x", serial="SNP1", timestamp=41.0, voltage=float("nan"))], 41.0)
        ok = all(v == v for v in (o.ccl, o.dcl, o.cvl))
    except Exception as exc:  # noqa: BLE001
        ok, o = False, exc
    check("garbage input no exception/NaN", ok)
    return fails


def main() -> int:
    print(f"python {sys.version.split()[0]}")
    fails = run()
    print("SELFTEST " + ("PASSED" if not fails else "FAILED: " + ", ".join(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
