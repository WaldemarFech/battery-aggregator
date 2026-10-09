"""Scenarios S31-S40 (part E: platform, restarts, updates, garbage, load steps)."""
import json
import math

import pytest
from bench import Bench, cfg

from battery_aggregator import Core, SafeCore
from battery_aggregator.adapter import Adapter, FakeBus, check_system_settings
from battery_aggregator.model import DISCHARGE, BankMode
from battery_aggregator.persistence import load_state, save_state
from battery_aggregator.sim import Plant, SimPack, run


def test_S31_venus_restart_init_then_normal_with_persisted_shares(tmp_path):
    b = Bench()
    b.warm()
    b.bank_current(-100.0)
    b.run(200)
    path = str(tmp_path / "state.json")
    save_state(path, b.core.export_state())

    core = Core(cfg(), persisted=load_state(path))
    nb = Bench(core=core)
    for l in nb.online:
        nb.online[l] = False
    o = nb.run(1)
    assert o.mode == BankMode.INIT
    assert o.ccl == 0.0 and o.dcl == pytest.approx(90.0) and o.cvl == pytest.approx(53.6)
    appear = {"P1": 20, "P2": 40, "P3": 60}
    modes = []
    for k in range(1, 130):
        for l, t in appear.items():
            nb.online[l] = k >= t
        modes.append(nb.run(1).mode)
    assert modes[30] == BankMode.INIT                       # nobody admitted yet at ~31 s
    assert BankMode.DEGRADED in modes and modes[-1] == BankMode.NORMAL
    assert core.est.valid("P1", DISCHARGE)                   # estimator restored from state.json


def test_S31_init_timeout_goes_failsafe_and_corrupt_state_is_ignored(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json", encoding="utf-8")
    core = Core(cfg(), persisted=load_state(str(p)))
    nb = Bench(core=core)
    for l in nb.online:
        nb.online[l] = False
    assert nb.run(119).mode == BankMode.INIT
    assert nb.run(2).mode == BankMode.FAILSAFE
    assert Core(cfg(), persisted={"version": 1, "estimator": {"P1": {"discharge": {"s_hat": "x"}}}})


def test_S32_kill9_restart_is_conservative():
    b = Bench()
    b.warm()
    b.run(300)
    assert b.out.ccl > 100
    fresh = Bench(core=Core(cfg()))
    o = fresh.run(1)                                         # process restarted: INIT
    assert o.mode == BankMode.INIT and o.ccl == 0.0


def test_S33_core_exception_fault_mode():
    b = Bench()
    b.warm()
    b.run(300)
    safe = SafeCore(b.core)
    good = safe.step(b.snaps(), b.t)
    b.t += 1
    original = b.core.step

    def boom(*a, **k):
        raise ZeroDivisionError("injected")
    b.core.step = boom
    f1 = safe.step(b.snaps(), b.t)
    assert f1.mode == BankMode.FAULT and f1.ccl == pytest.approx(good.ccl / 2)
    assert f1.alarms["InternalFailure"] == 2
    for k in range(1, 12):
        f = safe.step(b.snaps(), b.t + k)
    assert f.ccl == 0.0 and f.dcl <= 90.0 and not f.restart_requested
    for k in range(12, 62):
        f = safe.step(b.snaps(), b.t + k)
    assert f.restart_requested
    b.core.step = original
    r = safe.step(b.snaps(), b.t + 62)
    assert r.ccl <= f.ccl + 1e-9 and r.mode != BankMode.FAULT  # recovers without jumping up


def test_S34_missing_path_untrusted():
    b = Bench()
    b.warm()
    b.set("P2", dcl=None)
    o = b.run(1)
    assert b.state("P2") == "untrusted"
    assert "missing" in " ".join(b.track("P2").problems) or "invalid dcl" in " ".join(b.track("P2").problems)
    s1 = 0.35 / 0.64
    assert o.model_dcl == pytest.approx(43.6 / s1, abs=0.2)  # P2 treated as blind, others renormalised
    assert o.alarms["BmsCable"] == 1


def test_S34_adapter_reports_missing_path():
    bus = FakeBus()
    vals = _bus_values("JK-SN-0001")
    del vals["/Info/MaxDischargeCurrent"]
    bus.services["com.victronenergy.battery.ttyUSB0"] = vals
    clock = [0.0]
    ad = Adapter(bus, SafeCore(Core(cfg())), cfg(), clock=lambda: clock[0], wall=lambda: 1.8e9)
    ad.tick()
    tr = ad.core.core.tracks["JK-SN-0001"]
    assert tr.state.value == "untrusted"
    assert any("/Info/MaxDischargeCurrent" in p for p in tr.problems)


def test_S35_ttyusb_swap_keeps_identity():
    b = Bench()
    b.warm()
    b.bank_current(-100.0)
    b.run(60)
    est_before = b.core.est.to_dict()
    b.service["P1"], b.service["P2"] = b.service["P2"], b.service["P1"]
    b.run(5)
    assert all(b.state(l) == "active" for l in ("P1", "P2", "P3"))
    assert b.track("P1").service.endswith("ttyUSB1") and b.track("P1").capacity_ah == 305
    assert b.core.est.to_dict().keys() == est_before.keys()


def test_S35_duplicate_serial_counted_once():
    b = Bench()
    b.warm()
    snaps = b.snaps()
    dup = snaps[0].__class__(**{**snaps[0].__dict__, "service": "com.victronenergy.battery.ttyUSB9"})
    o = b.core.step(snaps + [dup], b.t)
    assert sum(1 for p in o.packs if p.serial == "JK-SN-0001") == 1
    assert any("duplicate serial" in e[2] for e in o.events)


def test_S36_garbage_values():
    b = Bench()
    b.warm()
    b.set("P1", soc=255, cell_min=0.0, current=float("nan"), installed_capacity=-1)
    o = b.run(2)
    assert b.state("P1") == "untrusted"
    assert all(math.isfinite(x) for x in (o.ccl, o.dcl, o.cvl))
    b.set("P1", cell_min=3.02, current=0.0)
    o = b.run(1)
    assert "ignored soc=255" in " ".join(b.track("P1").problems)
    assert b.track("P1").valid.soc is None and b.track("P1").valid.capacity_ah is None


def test_S37_system_settings_check():
    c = cfg(shadow=False)
    bus = FakeBus()
    bus.settings = {"/Settings/SystemSetup/BmsInstance": -1, "/Settings/SystemSetup/BatteryService": "default"}
    ok, msg = check_system_settings(bus, c)
    assert not ok and not bus.writes                         # only reported
    ad = Adapter(bus, SafeCore(Core(c)), c, clock=lambda: 0.0, wall=lambda: 1.8e9)
    out = ad.tick()
    assert out.alarms["InternalFailure"] == 1
    c2 = cfg(shadow=False, enforce_system_settings=True)
    ok, msg = check_system_settings(bus, c2)
    assert "corrected" in msg and bus.settings["/Settings/SystemSetup/BmsInstance"] == 512
    assert check_system_settings(bus, c2)[0]


def test_S38_instance_collision_rejected():
    assert cfg().device_instance == 512 and not cfg().validate()
    errs = cfg(device_instance=0).validate()
    assert any("collides" in e for e in errs)
    with pytest.raises(ValueError):
        Core(cfg(device_instance=4))


def _wallbox_plant():
    return Plant([SimPack("P1", "JK-SN-0001", 305, soc=50, dcl=43.6),
                  SimPack("P2", "JK-SN-0002", 314, soc=50, dcl=92.0),
                  SimPack("P3", "JK-SN-0003", 305, soc=50, dcl=59.0)], delay_s=2)


def test_S39_wallbox_step_grid_parallel_enforces_dcl():
    core = Core(cfg())
    plant = _wallbox_plant()
    hist = run(core, plant, 400, lambda t: -240.0 if t > 60 else 0.0)
    tail = hist[-60:]
    assert all(-c["P1"] <= 43.6 * 1.05 for _, _, c in tail)
    assert -plant.i_bank <= max(o.dcl for _, o, _ in tail) + 1.0     # rest comes from the grid
    assert -plant.i_bank < 240.0


def test_S39_island_mode_dcl_not_enforceable_warns():
    core = Core(cfg())
    plant = _wallbox_plant()
    run(core, plant, 300, lambda t: -100.0)

    class Island(Plant):
        def step(self, t, dt, demand, published):          # load is load: limits ignored
            self.solve(demand)
    island = Island(plant.packs)
    hist = run(core, island, 20, lambda t: -240.0, t0=300)
    assert any("DCL exceeded" in e[2] for _, o, _ in hist for e in o.events)


def test_S40_flapping_pack_limit_no_oscillation():
    b = Bench()
    b.warm()
    b.run(400)
    vals = []
    for k in range(120):
        b.set("P3", ccl=20.0 if (k // 2) % 2 == 0 else 200.0)
        vals.append(b.run(1).ccl)
    assert vals[0] == pytest.approx(20 / 0.35, abs=0.05)
    assert max(vals) <= 20 / 0.35 + 1e-6


def _bus_values(serial):
    return {"/Dc/0/Voltage": 48.5, "/Dc/0/Current": 0.0, "/Dc/0/Temperature": 20.0, "/Soc": 50,
            "/InstalledCapacity": 305, "/Info/MaxChargeVoltage": 56.8, "/Info/MaxChargeCurrent": 200.0,
            "/Info/MaxDischargeCurrent": 43.6, "/Io/AllowToCharge": 1, "/Io/AllowToDischarge": 1,
            "/System/MinCellVoltage": 3.02, "/System/MaxCellVoltage": 3.04, "/System/NrOfCellsPerBattery": 16,
            "/Serial": serial, "/Mgmt/ProcessName": "/data/apps/dbus-serialbattery/dbus-serialbattery.py",
            "/Alarms/HighCellVoltage": 0}


def test_persistence_roundtrip(tmp_path):
    p = str(tmp_path / "x" / "state.json")
    save_state(p, {"version": 1, "a": 1})
    assert load_state(p) == {"version": 1, "a": 1}
    assert json.loads(open(p, encoding="utf-8").read())["a"] == 1
