"""D-Bus adapter skeleton against the FakeBus (shadow mode, read-only, watchdog)."""
import json

import pytest
from bench import SERIAL, cfg
from test_scenarios_e import _bus_values

from battery_aggregator import Core, SafeCore
from battery_aggregator.adapter import (Adapter, FakeBus, LivePublisher, ShadowPublisher, Watchdog,
                                        build_snapshot, is_pack_service)
from battery_aggregator.adapter import paths as P


def _bus():
    bus = FakeBus()
    for i, l in enumerate(("P1", "P2", "P3")):
        bus.services[f"com.victronenergy.battery.ttyUSB{i}"] = _bus_values(SERIAL[l])
    bus.services["com.victronenergy.battery.ttyS5"] = {"/Mgmt/ProcessName": "dbus-smartshunt", "/Dc/0/Current": 1}
    bus.services["com.victronenergy.solarcharger.ttyUSB7"] = {"/Dc/0/Current": 5}
    return bus


def _jitter(bus, k):
    for svc, v in bus.services.items():
        if "/Dc/0/Voltage" in v:
            v["/Dc/0/Voltage"] = 48.5 + 0.001 * (k % 2)


def test_build_snapshot_maps_contract_paths():
    s = build_snapshot("com.victronenergy.battery.ttyUSB0", _bus_values("X1"), 5.0)
    assert s.serial == "X1" and s.ccl == 200.0 and s.cell_max == 3.04 and s.timestamp == 5.0
    assert s.alarms == {"HighCellVoltage": 0} and s.missing_paths == ()


def test_discovery_filters_foreign_and_own_services():
    assert is_pack_service("com.victronenergy.battery.ttyUSB0", _bus_values("A"))
    assert not is_pack_service(P.OWN_SERVICE, _bus_values("A"))
    assert not is_pack_service("com.victronenergy.battery.ttyS5", {"/Mgmt/ProcessName": "dbus-smartshunt"})
    assert not is_pack_service("com.victronenergy.solarcharger.x", _bus_values("A"))


def test_shadow_mode_publishes_nothing_to_dvcc_and_is_read_only():
    bus = _bus()
    lines = []
    c = cfg()                                   # shadow=True by default
    pub = ShadowPublisher(bus=bus, sink=lines.append)
    clock = [0.0]
    ad = Adapter(bus, SafeCore(Core(c)), c, publisher=pub, clock=lambda: clock[0], wall=lambda: 1.8e9)
    for k in range(40):
        clock[0] = float(k)
        _jitter(bus, k)
        out = ad.tick()
    assert out.mode.value == "normal" and len(out.packs) == 3
    assert P.OWN_SERVICE not in bus.registered                         # no battery service
    assert set(bus.registered) == {P.SHADOW_SERVICE}
    assert all(svc == P.SHADOW_SERVICE and path.startswith("/Custom/Shadow/") for svc, path, _ in bus.writes)
    rec = json.loads(lines[-1])
    assert "/Info/MaxDischargeCurrent" in rec["would_publish"]
    assert len(lines) < 40                                             # change-driven, not every tick


def test_live_publisher_refused_in_shadow_and_publishes_when_live():
    with pytest.raises(RuntimeError):
        LivePublisher(FakeBus(), cfg())
    bus = _bus()
    c = cfg(shadow=False)
    bus.settings = {P.SETTING_BMS_INSTANCE: 512, P.SETTING_BATTERY_SERVICE: "com.victronenergy.battery/512"}
    ad = Adapter(bus, SafeCore(Core(c)), c, clock=lambda: 0.0, wall=lambda: 1.8e9)
    out = ad.tick()
    assert bus.registered[P.OWN_SERVICE]["DeviceInstance"] == 512
    assert bus.published[P.OWN_SERVICE]["/Info/MaxChargeCurrent"] == 0.0     # INIT
    assert out.alarms["InternalFailure"] == 0
    assert not [w for w in bus.writes if w[0].startswith("com.victronenergy.battery.tty")]


def test_broken_service_read_is_isolated():
    bus = _bus()
    bus.fail_reads.add("com.victronenergy.battery.ttyUSB1")
    c = cfg()
    ad = Adapter(bus, SafeCore(Core(c)), c, publisher=ShadowPublisher(sink=lambda l: None),
                 clock=lambda: 0.0, wall=lambda: 1.8e9)
    out = ad.tick()                                                    # no exception
    assert out.mode.value == "init"


def test_watchdog_and_restart_request():
    t = [0.0]
    exits = []
    wd = Watchdog(timeout_s=5.0, clock=lambda: t[0], exit_fn=exits.append)
    t[0] = 4.0
    assert wd.check() and not exits
    t[0] = 10.0
    wd.check()
    assert exits == [1]

    bus = _bus()
    c = cfg()
    core = Core(c)
    core.step = lambda *a, **k: 1 / 0
    clock = [0.0]
    ad = Adapter(bus, SafeCore(core), c, publisher=ShadowPublisher(sink=lambda l: None),
                 clock=lambda: clock[0], wall=lambda: 1.8e9, exit_fn=exits.append)
    for k in range(62):
        clock[0] = float(k)
        out = ad.tick()
    assert out.restart_requested and exits[-1] == 1
