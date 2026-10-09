"""GUI parity with dbus-serialbattery, tested against D-Bus values recorded on the owner's Cerbo
(tests/fixtures/venus_battery_20261005.json, read-only MQTT dump 2026-10-05)."""
import copy
import json
from pathlib import Path

import pytest

from battery_aggregator import Core, SafeCore, parity
from battery_aggregator.adapter import Adapter, FakeBus, LivePublisher
from battery_aggregator.adapter import paths as P
from battery_aggregator.config import Config

FIX = json.loads((Path(__file__).parent / "fixtures" / "venus_battery_20261005.json").read_text())
PACKS = {k: v for k, v in FIX["services"].items() if k != P.OWN_SERVICE}
LIVE_CFG = {  # /data/battery-aggregator/config.json on the Cerbo (2026-10-05), shadow false
    "auto_add_unknown": True, "blind_charge_policy": "last_temp_margin", "blind_temp_margin_k": 10.0,
    "ccl_hw_a": 1000.0, "commissioning_ccl_cap_a": 210.0, "commissioning_dcl_cap_a": 59.0,
    "dcl_emergency_a": 90.0, "dcl_hw_a": 1000.0, "device_instance": 512, "enforce_system_settings": False,
    "expected_pack_count": 3, "inverter_ccl_cap_a": 210.0, "measured_shares_verified": False, "n1_mode": "hw",
    "packs": [
        {"capacity_ah": 305.0, "cells": 16, "label": "P1", "ocp_charge_a": 200.0, "ocp_discharge_a": 200.0,
         "serial": "links2upd_links2bt"},
        {"capacity_ah": 314.0, "cells": 16, "label": "P2", "ocp_charge_a": 200.0, "ocp_discharge_a": 200.0,
         "serial": "links3upd_links3"},
        {"capacity_ah": 305.0, "cells": 16, "label": "P3", "ocp_charge_a": 200.0, "ocp_discharge_a": 200.0,
         "serial": "links1upd_links1bt"}],
    "product_name": "Merge-Batterie (3 Packs)", "shadow": False, "share_floor_equal_frac": 0.8,
}
# serialbattery services: DeviceInstance -> service (ttyUSB2 = P1, ttyUSB3 = P2, ttyUSB1 = P3)
LABEL = {"links2upd_links2bt": "P1", "links3upd_links3": "P2", "links1upd_links1bt": "P3"}


def _run(ticks=45, mutate=None):
    bus = FakeBus()
    bus.services = copy.deepcopy(PACKS)
    if mutate:
        mutate(bus.services)
    cfg = Config.from_dict(LIVE_CFG)
    clock = [0.0]
    ad = Adapter(bus, SafeCore(Core(cfg)), cfg, publisher=LivePublisher(bus, cfg),
                 clock=lambda: clock[0], wall=lambda: 1.8e9)
    out = None
    for k in range(ticks):
        clock[0] = float(k)
        for v in bus.services.values():          # keep the packs "alive" (freshness)
            v["/Dc/0/Voltage"] = round(v["/Dc/0/Voltage"] + (0.001 if k % 2 else -0.001), 3)
        out = ad.tick()
    return bus, out, bus.published[P.OWN_SERVICE]


def _svc(label):
    return next(s for s, v in PACKS.items() if LABEL[v["/Serial"]] == label)


def test_recorded_fixture_is_complete():
    assert len(PACKS) == 3
    for v in PACKS.values():
        assert v["/ProductId"] == 0xBA77 and v["/System/NrOfCellsPerBattery"] == 16
        assert all(f"/Voltages/Cell{c}" in v and f"/Balances/Cell{c}" in v for c in range(1, 17))


def test_aggregate_today_lacks_the_paths_the_gui_pages_gate_on():
    """Inventory finding: the deployed 0.2.2 aggregate (as seen on MQTT) has none of them."""
    agg = FIX["services"][P.OWN_SERVICE]
    for p in ("/Voltages/Cell3", "/Io/AllowToCharge", "/Info/ChargeLimitation", "/Info/ChargeMode",
              "/History/ChargedEnergy", "/Balancing", "/System/Temperature1"):
        assert p not in agg


def test_all_48_cells_pack_major_with_layout():
    _, out, pub = _run()
    assert out.mode.value == "normal" and len(out.packs) == 3
    for lbl, base in (("P1", 0), ("P2", 16), ("P3", 32)):
        src = PACKS[_svc(lbl)]
        for c in range(1, 17):
            assert pub[f"/Voltages/Cell{base + c}"] == src[f"/Voltages/Cell{c}"]
            assert pub[f"/Balances/Cell{base + c}"] == int(bool(src[f"/Balances/Cell{c}"]))
    assert "/Voltages/Cell49" not in pub
    assert pub["/Custom/Cells/Layout"] == "P1:1-16,P2:17-32,P3:33-48"
    cells = [pub[f"/Voltages/Cell{n}"] for n in range(1, 49)]
    assert pub["/Voltages/Diff"] == round(max(cells) - min(cells), 3)
    assert pub["/System/NrOfCellsPerBattery"] == 16


def test_gui_gate_paths_present_so_existing_pages_show():
    _, _, pub = _run()
    # "dbus-serialbattery - Cell Voltages" (Cell3), IO page, General rows, History
    for p in ("/Voltages/Cell3", "/Io/AllowToCharge", "/Io/AllowToDischarge", "/Io/AllowToBalance",
              "/Info/ChargeLimitation", "/Info/DischargeLimitation", "/Info/ChargeMode", "/Balancing",
              "/System/MOSTemperature", "/System/Temperature1", "/System/Temperature1Name", "/CurrentAvg",
              "/History/ChargedEnergy", "/History/MinimumCellVoltage", "/Info/ChargeModeDebug"):
        assert pub.get(p) is not None, p
    assert pub["/System/Temperature1Name"] == "P1 max" and pub["/System/Temperature4"] is None
    # ProductId stays our own: the serialbattery Settings page (writes pack settings) stays hidden
    assert pub.get("/ProductId") is None


def test_history_aggregates_over_packs():
    _, _, pub = _run()
    vals = [PACKS[s] for s in PACKS]
    assert pub["/History/ChargedEnergy"] == round(sum(v["/History/ChargedEnergy"] for v in vals), 2)
    assert pub["/History/MinimumCellVoltage"] == min(v["/History/MinimumCellVoltage"] for v in vals)
    assert pub["/History/CanBeCleared"] == 0


def test_control_values_win_over_parity_and_are_unchanged():
    bus_a, out_a, pub_a = _run()
    # same run without parity: the published DVCC values must be bit-identical
    cfg = Config.from_dict(LIVE_CFG)
    bus = FakeBus()
    bus.services = copy.deepcopy(PACKS)
    clock = [0.0]
    ad = Adapter(bus, SafeCore(Core(cfg)), cfg, publisher=LivePublisher(bus, cfg),
                 clock=lambda: clock[0], wall=lambda: 1.8e9)
    ad.gui_parity = lambda raw, out, now: {}
    for k in range(45):
        clock[0] = float(k)
        for v in bus.services.values():
            v["/Dc/0/Voltage"] = round(v["/Dc/0/Voltage"] + (0.001 if k % 2 else -0.001), 3)
        ad.tick()
    pub_b = bus.published[P.OWN_SERVICE]
    for k, v in out_a.published().items():
        if k != "/Custom/Heartbeat":
            assert pub_a[k] == pub_b[k] == v, k


def test_parity_crash_never_stops_dvcc(monkeypatch):
    def boom(*a, **k):
        raise ZeroDivisionError("display bug")
    monkeypatch.setattr(parity, "build", boom)
    _, out, pub = _run(ticks=5)
    assert pub["/Info/MaxChargeCurrent"] == round(out.ccl, 1)
    assert "/Voltages/Cell1" not in pub


def test_parity_never_writes_to_pack_services():
    bus, _, _ = _run()
    assert all(svc == P.OWN_SERVICE for svc, _, _ in bus.writes)


def _mk_status(label, current, ccl, dcl, state="active"):
    from battery_aggregator.outputs import PackStatus
    return PackStatus(label, label, f"svc.{label}", state, "active", 50.0, current, None, None, ccl, dcl,
                      55.0, False, True, True, False)


@pytest.mark.parametrize("reason,own,expect", [
    ("P3 10.0A / s 0.33 (cell taper 3.450V)", "Max Battery Charge Current", "P3 Cell Voltage"),
    ("P2 40.0A / s 0.33 (cold taper 4.0C)", "", "P2 Temp"),
    ("P1 0.0A / s 0.33", "SoC", "P1 SoC"),
    ("P1 20.0A / s 0.33", "MOSFET", "P1 MOSFET"),
    ("P1 20.0A / s 0.33", "Max Battery Charge Current", "P1 BMS limit"),
    ("N-1: P1 if P2 trips", "", "N-1 reserve P1"),
    ("hardware cap", "", "Inverter/HW cap"),
    ("SR-21 commissioning cap 59A (model 300.0A)", "", "Commissioning cap"),
    ("restart hold 120.0A (live model 130.0A)", "", "Restart hold"),
    ("P3 blind and last temperature unknown/cold: charge blocked", "", "P3 blind Temp"),
])
def test_limitation_text_names_pack_and_carries_gui_keyword(reason, own, expect):
    packs = [_mk_status(lbl, 0.0, 100.0, 100.0) for lbl in ("P1", "P2", "P3")]
    by_label = {lbl: {"/Info/ChargeLimitation": own} for lbl in ("P1", "P2", "P3")}
    txt = parity.limitation(reason, 55.0, packs, by_label, True)
    assert txt.startswith(expect) and txt.endswith("55A")


def test_closed_loop_names_the_worst_utilised_pack():
    # owner scenario: P1/P2 allow 50 A, P3 nearly full 10 A but carries 18 A -> PI cuts the bank
    packs = [_mk_status("P1", 20.0, 50.0, 100.0), _mk_status("P2", 21.0, 50.0, 100.0),
             _mk_status("P3", 18.0, 10.0, 100.0)]
    by_label = {"P1": {}, "P2": {}, "P3": {"/Info/ChargeLimitation": "Cell Voltage"}}
    txt = parity.limitation("sum of pack limits -> closed loop 55.0A (down, u 1.80)", 55.0, packs, by_label, True)
    assert txt == "P3 Cell Voltage PI u1.80 55A"
    assert any(k in txt for k in parity.GUI_KEYWORDS)       # GUI colours the Cell max/min row


def test_offline_pack_cells_are_invalid_not_stale():
    def drop_p3(services):
        services[_svc("P3")]["/Connected"] = 0
    _, out, pub = _run(mutate=drop_p3)
    assert pub["/Voltages/Cell33"] is None and pub["/Voltages/Cell1"] is not None


def test_debug_text_lists_every_pack_and_all_cells():
    _, _, pub = _run()
    txt = pub["/Info/ChargeModeDebug"]
    for lbl in ("P1", "P2", "P3"):
        assert f"{lbl} cells " in txt
    assert txt.count(" C1-8: ") == 3 and txt.count(" C9-16: ") == 3
    assert "Config (read-only)" in txt


def test_debug_text_is_throttled():
    bus = FakeBus()
    bus.services = copy.deepcopy(PACKS)
    cfg = Config.from_dict(LIVE_CFG)
    clock = [0.0]
    ad = Adapter(bus, SafeCore(Core(cfg)), cfg, publisher=LivePublisher(bus, cfg),
                 clock=lambda: clock[0], wall=lambda: 1.8e9)
    for k in range(10):
        clock[0] = float(k)
        ad.tick()
    n = sum(1 for svc, p, _ in bus.writes if p == "/Info/ChargeModeDebug")
    assert n == 2                                         # t=0 and t=5


def test_charge_mode_mixed_and_degraded():
    order = [("P1", 16), ("P2", 16)]
    by = {"P1": {"/Info/ChargeMode": "Bulk, Linear Mode"}, "P2": {"/Info/ChargeMode": "Float, Linear Mode"}}
    assert parity._charge_mode(order, by, "normal") == "P1 Bulk / P2 Float"
    by["P2"]["/Info/ChargeMode"] = "Bulk, Linear Mode"
    assert parity._charge_mode(order, by, "degraded") == "[degraded] Bulk, Linear Mode"


class _FakeVeService:
    """Mirrors velib: add_path after register is silent; ServiceContext flushes ItemsChanged."""

    def __init__(self):
        self.items, self.signals, self.prop_changes = {}, [], []

    def __contains__(self, p):
        return p in self.items

    def __setitem__(self, p, v):
        if self.items[p] != v:
            self.prop_changes.append(p)
        self.items[p] = v

    def add_path(self, p, v):
        self.items[p] = v

    def __enter__(self):
        svc = self

        class Ctx:
            changes = {}

            def add_path(self, p, v):
                svc.add_path(p, v)
                self.changes[p] = v
        self._ctx = Ctx()
        self._ctx.changes = {}
        return self._ctx

    def __exit__(self, *exc):
        if self._ctx.changes:
            self.signals.append(dict(self._ctx.changes))


def test_velib_publish_many_announces_new_paths_and_keeps_item_updates():
    from battery_aggregator.adapter.velib_bus import VenusBus
    vb = object.__new__(VenusBus)
    s = _FakeVeService()
    s.add_path("/Info/MaxChargeCurrent", 10.0)          # registered earlier
    vb.services = {P.OWN_SERVICE: s}
    vb.publish_many(P.OWN_SERVICE, {"/Info/MaxChargeCurrent": 20.0, "/Io/AllowToCharge": 1,
                                    "/Alarms/LowVoltage": 0})
    assert s.prop_changes == ["/Info/MaxChargeCurrent"]  # DVCC value: unchanged per-item path
    assert s.signals == [{"/Io/AllowToCharge": 1, "/Alarms/LowVoltage": 0}]  # constant new paths announced
    vb.publish_many(P.OWN_SERVICE, {"/Io/AllowToCharge": 1})
    assert len(s.signals) == 1
