"""Scenarios S13-S21 (part B: voltage/cells/temperature, part C: SoC)."""
import pytest
from bench import Bench, cfg

from battery_aggregator.model import BankMode


def _charged_bench(**kw):
    b = Bench(cfg(**kw))
    for l in b.vals:
        b.set(l, voltage=55.4, cell_min=3.40, cell_max=3.42)
    b.warm()
    b.run(800)                      # CVL ramps from INIT 53.6 V up to 56.8 V
    return b


def test_S13_cell_at_355_cell_controller_and_taper():
    b = _charged_bench()
    assert b.out.cvl == pytest.approx(56.8)
    b.set("P3", cell_max=3.55, cell_max_id=9)
    o = b.run(1)
    assert o.target_cvl == pytest.approx(55.4 - 16 * 0.10, abs=0.01)   # 53.8 V
    assert o.cvl == pytest.approx(53.8, abs=0.01)                       # immediately
    assert "cell controller P3.C09" in o.reason_voltage
    assert o.model_ccl == pytest.approx(200 * 0.30 / 0.35, abs=0.1)     # ~171 A, no hard 0
    b.set("P3", cell_max=3.44)
    vals = [b.run(1).cvl for _ in range(120)]
    assert vals[55] == pytest.approx(53.8, abs=0.01)                    # 60 s hold
    rises = [y - x for x, y in zip(vals, vals[1:])]
    assert max(rises) <= 0.005 + 1e-9                                   # 0.05 V / 10 s
    assert vals[-1] > 53.8


def test_S14_cell_above_360_hard_stop_then_fet_open():
    b = _charged_bench()
    b.set("P3", cell_max=3.62, charge_fet=1)
    o = b.run(1)
    assert o.model_ccl == 0.0 and o.ccl == 0.0
    assert o.target_cvl < 55.4
    b.set("P3", charge_fet=0)                                           # BMS OVP opened
    o = b.run(1)
    assert b.track("P3").chg_open
    assert o.model_ccl == pytest.approx(250.0)                          # P1/P2 continue (N-1 at N=2)
    assert o.target_cvl == pytest.approx(56.8)
    assert o.cell_max == pytest.approx(3.62) and o.cell_max_id.startswith("P3")


def test_S15_high_cell_alarm_without_fet_info_no_deadlock_trick():
    b = Bench()
    b.warm()
    b.run(300)
    b.set("P3", alarms={"HighCellVoltage": 2}, cell_max=3.58)
    o = b.run(1)
    assert o.ccl == 0.0
    b.run(60)
    assert b.out.ccl == 0.0 and not b.track("P3").chg_open             # no current evidence possible
    b.set("P3", alarms={"HighCellVoltage": 0}, cell_max=3.52)
    assert b.run(40).target_ccl == 0.0                                  # latch until < 3.50 V
    b.set("P3", cell_max=3.48)
    o = b.run(1)
    assert o.target_ccl > 0.0
    o = b.run(60)
    assert 0.0 < o.ccl < o.target_ccl + 1e-9                            # back via ramp


def test_S16_low_temperature_default_m5_taper_and_block():
    b = Bench()
    b.warm()
    b.set("P1", temperature=3.0, ccl=30.0, temp_min_id=1)
    o = b.run(1)
    assert o.model_ccl == pytest.approx(30 * 0.3 / 0.35, abs=0.1)      # M5 taper 0..10 C: 30 % at 3 C
    assert o.temp_min == 3.0 and o.temp_min_id == "P1.T01"
    b.set("P1", temperature=-0.5)
    o = b.run(1)
    assert o.model_ccl == 0.0 and "cold" in o.reason_charge          # owner policy: < 0 C -> 0 A


def test_S16_low_temperature_design_numbers_with_threshold_0C():
    b = Bench(cfg(t_charge_min_c=0.0, t_charge_full_c=0.0))
    b.warm()
    b.set("P1", temperature=3.0, ccl=30.0)
    assert b.run(1).model_ccl == pytest.approx(30 / 0.35, abs=0.1)     # ~86 A
    b.set("P1", temperature=-1.0, ccl=0.0, charge_fet=1)
    assert b.run(1).model_ccl == 0.0
    b.set("P1", charge_fet=0)                                           # JK LTP opens
    o = b.run(1)
    assert o.model_ccl == pytest.approx(250.0)                          # doc 300; N-1 at N=2 binds


def test_S17_high_temperature_discharge():
    b = Bench()
    b.warm()
    b.set("P2", temperature=48.0, dcl=40.0)
    o = b.run(1)
    assert o.model_dcl == pytest.approx(40 / 0.36, abs=0.1)            # 111 A
    assert o.reason_discharge.startswith("P2")
    assert o.temperature == 48.0


def test_S18_cell_undervoltage_taper():
    b = Bench()
    b.warm()
    b.set("P1", cell_min=2.987)
    o = b.run(1)
    assert o.model_dcl == pytest.approx(43.6 * 0.87 / 0.35, abs=0.1)   # ~108 A
    b.set("P1", cell_min=2.90)
    assert b.run(1).model_dcl == 0.0
    b.set("P1", discharge_fet=0)
    o = b.run(1)
    assert o.model_dcl == pytest.approx(59 / (0.35 / 0.65), abs=0.1)   # 109.6 A from P2/P3
    assert o.model_ccl == pytest.approx(300.0)                          # P1 still in P_chg


def test_S19_uncalibrated_soc_real_values():
    b = Bench()
    for l in b.vals:
        b.set(l, voltage=48.48)                                         # 3.03 V per cell
    b.warm()
    events = []
    for _ in range(1830):
        events += b.run(1).events
    o = b.out
    assert o.soc_raw == pytest.approx(100 * (0.08 * 305 + 0.01 * 314 + 0.13 * 305) / 924, abs=0.01)
    assert o.soc_raw == pytest.approx(7.27, abs=0.01)
    assert o.soc_min == 1 and o.soc_min_pack == "P2"
    assert all(b.track(l).soc_suspect for l in ("P1", "P2", "P3"))
    assert any("SoC drift" in e[2] for e in events)
    assert o.calibration_recommended
    assert o.soc == pytest.approx(7.27, abs=0.01)


def test_S20_calibration_charge():
    b = Bench(cfg(calibration_enabled=True))
    b.warm()
    b.bank_current(30.0)
    o = b.run(2)
    assert o.calibration_active
    assert o.target_cvl <= 16 * 3.45 + 1e-9
    for l in b.vals:
        b.set(l, soc=100, current=3.0, voltage=55.2, cell_max=3.45)
        b.track(l).soc_suspect = True
    events = []
    for _ in range(70):
        events += b.run(1).events
    assert not b.out.calibration_active
    assert all(b.track(l).t_last_full is not None for l in ("P1", "P2", "P3"))
    assert not any(b.track(l).soc_suspect for l in ("P1", "P2", "P3"))
    assert any("calibration charge finished" in e[2] for e in events)
    assert "calibration" not in b.out.reason_voltage


def test_S21_topology_change_smooths_soc():
    b = Bench()
    b.warm()
    b.run(5)
    assert b.out.soc == pytest.approx(7.27, abs=0.01)
    b.online["P3"] = False
    b.run(640)
    assert b.state("P3") == "gone"
    o = b.out
    assert o.soc_raw == pytest.approx(100 * (0.08 * 305 + 0.01 * 314) / 619, abs=0.01)   # 4.45 %
    assert o.installed_capacity_ah == pytest.approx(619.0)
    assert o.soc > o.soc_raw                                            # still slewing
    vals = [b.run(1).soc for _ in range(120)]
    drops = [x - y for x, y in zip(vals, vals[1:])]
    assert max(drops) <= 2.0 / 60 + 1e-9
    assert vals[-1] == pytest.approx(4.45, abs=0.01)
    assert b.out.mode == BankMode.DEGRADED                               # SR-12: expected 3 packs
