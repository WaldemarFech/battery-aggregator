"""limit_control = "sum" (owner rule 2026-10-08): bank CCL/DCL = sum of the live pack limits,
reactive cut only on a measured pack overcurrent, a lost pack drops out of the sum."""
import pytest
from bench import Bench, cfg

from battery_aggregator.model import BankMode
from battery_aggregator.parity import limitation
from battery_aggregator.sum_mode import ReactiveCut


def owner_cfg(**kw):
    base = dict(limit_control="sum", ccl_hw_a=1000.0, dcl_hw_a=1000.0, inverter_ccl_cap_a=None,
                measured_shares_verified=False)          # SR-21 cap must not bind with live data
    base.update(kw)
    return cfg(**base)


def bench(**kw):
    b = Bench(owner_cfg(**kw))
    for l in b.vals:
        b.set(l, ccl=200.0, dcl=200.0, soc=50)
    b.warm()
    return b


def test_three_packs_200_give_600():
    b = bench()
    b.bank_current(150.0)
    o = b.run(3)
    assert o.mode == BankMode.NORMAL
    assert o.ccl == pytest.approx(600.0) and o.dcl == pytest.approx(600.0)
    assert o.reason_charge.startswith("sum 3 packs")


def test_no_ramp_rise_is_immediate():
    b = bench()
    b.set("P2", ccl=50.0)
    assert b.run(2).ccl == pytest.approx(450.0)
    b.set("P2", ccl=200.0)
    assert b.run(1).ccl == pytest.approx(600.0)       # no 60 s window, no 5 A/s ramp


def test_pack_fails_sum_of_remaining_400():
    b = bench()
    b.online["P3"] = False
    o = b.run(15)                                      # P3 stale -> drops out at once
    assert o.ccl == pytest.approx(400.0) and o.dcl == pytest.approx(400.0)
    o = b.run(40)                                      # offline
    assert o.ccl == pytest.approx(400.0) and o.dcl == pytest.approx(400.0)
    assert o.mode == BankMode.DEGRADED


def test_cell_voltage_reduces_one_pack_to_50_gives_450():
    b = bench()
    b.set("P2", ccl=50.0, cell_max=3.45)
    o = b.run(2)
    assert o.ccl == pytest.approx(450.0)
    assert o.dcl == pytest.approx(600.0)
    assert "P2 50A (BMS CCL 50.0A)" in o.reason_charge
    raw = {"P2": {"/Info/ChargeLimitation": "Cell Voltage", "/System/MaxCellVoltage": 3.45}}
    txt = limitation(o.reason_charge, o.ccl, o.packs, raw, True)
    assert txt == "Summe 3 Packs: P2 Cell Voltage 3.450V 450A"


def test_no_own_taper_no_n1_no_inverter_cap():
    b = bench()
    for l in b.vals:
        b.set(l, cell_max=3.50, temperature=42.0)     # old code tapered here; the BMS decides now
    o = b.run(2)
    assert o.ccl == pytest.approx(600.0)
    assert o.n1_ccl == float("inf")


def test_overload_loop_120_of_100_lower_then_raise_stepwise():
    b = bench()
    b.set("P2", ccl=100.0)                             # sum 500 A
    sh = {"P1": 0.3, "P2": 0.4, "P3": 0.3}
    b.bank_current(300.0, sh)                          # P2 carries 120/100 A
    assert b.run(1).ccl == pytest.approx(500.0)        # < trip_s: no reaction yet
    o = b.run(2)
    assert o.ccl == pytest.approx(250.0)               # 300 * 100/120: one step down
    assert o.reason_charge.startswith("overload: P2 120/100A -> bank 250A")
    assert limitation(o.reason_charge, o.ccl, o.packs, {}, True) == "Ueberlast P2 120/100 A -> Bank 250 A"
    b.bank_current(250.0, sh)                          # inverter follows: P2 at 100 A, hold
    assert b.run(10).ccl == pytest.approx(250.0)
    b.bank_current(200.0, sh)                          # P2 80 A, below its limit -> raise in steps
    seen = [b.run(1).ccl for _ in range(20)]
    assert 270.0 in seen and 290.0 in seen and max(seen) <= 330.0
    assert all(y >= x for x, y in zip(seen, seen[1:]))
    o = b.run(80)
    assert o.ccl == pytest.approx(500.0) and o.reason_charge.startswith("sum 3 packs")


def test_overload_persisting_lowers_further_after_observation():
    b = bench()
    b.set("P2", ccl=100.0)
    b.bank_current(300.0, {"P1": 0.3, "P2": 0.4, "P3": 0.3})
    assert b.run(3).ccl == pytest.approx(250.0)
    b.bank_current(250.0, {"P1": 0.2, "P2": 0.6, "P3": 0.2})   # P2 now 150/100 A
    assert b.run(1).ccl == pytest.approx(250.0)        # waits down_interval_s before the next step
    o = b.run(3)
    assert o.ccl == pytest.approx(250.0 * 100 / 150)


def test_allow_flag_zero_from_source_adds_zero_no_own_voltage_stop():
    b = bench()
    b.set("P3", allow_charge=0)
    b.set("P1", cell_max=3.62)                         # no own 3.60 V stop any more
    o = b.run(2)
    assert o.ccl == pytest.approx(400.0)


def test_small_noise_does_not_trip():
    b = bench()
    b.bank_current(600.0, {"P1": 0.33, "P2": 0.335, "P3": 0.335})  # 201 A at 200 A (< 2 % + 1 A)
    assert b.run(10).ccl == pytest.approx(600.0)


def test_discharge_reactive():
    b = bench()
    b.set("P1", dcl=40.0)
    b.bank_current(-300.0, {"P1": 0.33, "P2": 0.33, "P3": 0.34})    # P1 99/40 A
    o = b.run(4)
    assert o.dcl == pytest.approx(300 * 40 / 99, rel=0.01)       # one step: I_bank * L/I


def test_all_packs_stale_holds_not_zero_dcl():
    b = bench()
    for l in b.vals:
        b.frozen[l] = True
    o = b.run(12)                                      # stale grace: held values, no DCL 0
    assert o.dcl > 0


def test_reactive_cut_unit():
    r = ReactiveCut(trip_s=0.0, release_s=0.0, up_step_a=20.0, up_interval_s=1.0)
    assert r.update(0.0, [("P1", 214.0, 200.0)], 600.0, 600.0) == pytest.approx(600 * 200 / 214)
    assert r.update(1.0, [("P1", 150.0, 200.0)], 450.0, 600.0) == pytest.approx(600 * 200 / 214 + 20)


def test_incident_recorded_on_pack_loss(tmp_path):
    import json

    from battery_aggregator.incident import IncidentRecorder
    b = bench()
    rec = IncidentRecorder(str(tmp_path), cells=16, sample_every_s=1.0, wall=lambda: 1.8e9 + b.t)
    raw = {b.service[l]: {f"/Voltages/Cell{c}": 3.30 + (0.05 if (l, c) == ("P3", 7) else 0.0)
                          for c in range(1, 17)} for l in b.vals}
    for _ in range(5):
        rec.observe(raw, b.run(1), b.t)
    b.online["P3"] = False
    paths = []
    for _ in range(15):
        p = rec.observe(raw, b.run(1), b.t)
        if p:
            paths.append(p)
    assert len(paths) == 1 and "P3_stale" in paths[0]
    d = json.load(open(paths[0], encoding="utf-8"))
    assert d["pack"] == "P3" and len(d["history"]) >= 5
    p3 = d["history"][0]["packs"]["P3"]
    assert p3["max_cell"] == 7 and p3["delta_mv"] == pytest.approx(50.0)
    assert set(d["now"]["packs"]) == {"P1", "P2", "P3"}


def test_dc_measurements_present_during_quarantine_after_start():
    b = Bench(owner_cfg())
    b.bank_current(-30.0)
    o = b.run(3)                                       # packs still in quarantine (30 s)
    assert o.voltage is not None and o.current == pytest.approx(-30.0)
    assert o.power is not None
