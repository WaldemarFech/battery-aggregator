"""Scenarios S22-S30 (part D: failure, return, N-1; R1/R5)."""
import pytest
from bench import Bench, cfg

from battery_aggregator.model import BankMode

N1_P3_GONE = 43.6 / (0.35 / 0.65)       # 80.97 A (doc rounds s to 0.54 -> 80.7 A)


def _discharging(seconds=400, **kw):
    b = Bench(cfg(**kw))
    b.warm()
    b.bank_current(-100.0)
    b.run(seconds)
    b.shares()          # re-inject scenario shares (sigma decays while learning)
    b.run(1)
    return b


def test_S22_n1_comm_loss_of_P3_while_discharging():
    b = _discharging()
    b.set("P3", cvl=56.0)
    b.run(1)
    b.online["P3"] = False
    o = b.run(5)
    assert b.state("P3") == "active" and o.model_dcl == pytest.approx(124.6, abs=2.5)
    o = b.run(7)                                                      # t = 12 s: STALE
    assert b.state("P3") == "stale"
    assert o.model_dcl == pytest.approx(N1_P3_GONE, abs=0.5)          # world "P3 gone" already
    o = b.run(20)                                                     # t = 32 s: OFFLINE/HOLD
    assert b.state("P3") == "offline"
    assert o.model_dcl == pytest.approx(N1_P3_GONE, abs=0.5)
    assert o.reason_discharge.startswith("P1")
    assert o.mode == BankMode.DEGRADED
    assert o.alarms["BmsCable"] == 1 and o.nr_offline == 1
    assert o.target_cvl == pytest.approx(56.0)                        # last P3 CVL held
    assert o.soc_raw == pytest.approx(7.27, abs=0.01)                 # last P3 SoC used
    before = o.model_dcl
    o = b.run(600)                                                    # GONE
    assert b.state("P3") == "gone"
    assert o.model_dcl <= before + 0.5                                # SR-02: no jump up at GONE
    assert o.target_cvl == pytest.approx(56.8)                        # CVL hold ended


def test_S23_P1_trips_hardware():
    b = _discharging()
    o = b.out
    s2, s3 = 0.36 / 0.65, 0.35 / 0.65
    assert s2 * 124.6 < 92 and s3 * 124.6 < 250                       # no cascade (hw OCP 250 A)
    b.set("P1", discharge_fet=0)
    b.bank_current(-100.0, {"P1": 0.0, "P2": 0.51, "P3": 0.49})        # opened FET: P1 carries nothing
    o = b.run(1)
    assert o.model_dcl == pytest.approx(59 / s3, abs=0.2)             # 109.6 A
    assert o.model_ccl == pytest.approx(300.0)                        # P1 still chargeable


def test_S23_B1_trip_not_inferred_from_current_alone():
    """B1: P1 at 0 A without FET info is NOT assumed open; P1 keeps bounding the DCL."""
    b = _discharging()
    b.bank_current(-100.0, {"P1": 0.0, "P2": 0.51, "P3": 0.49})
    o = b.run(30)
    assert not b.track("P1").dchg_open and not b.track("P1").chg_open
    assert "P1" in o.shares_discharge
    assert o.model_dcl <= 43.6 / (0.8 / 3) + 1e-6                     # P1's own 43.6 A still binds


def test_S24_n1_preventive_limit_binds():
    b = Bench(cfg(packs=tuple(p if p.label != "P1" else p.__class__(p.serial, p.label, p.capacity_ah, 16, 250.0, 60.0)
                              for p in cfg().packs)))
    b.warm()
    o = b.run(1)
    # min over k: failure of P2 (s 0.36) is worse than failure of P3 (doc: 111 A)
    assert o.n1_dcl == pytest.approx(60 * 0.64 / 0.35, abs=0.1)       # 109.7 A
    assert 60 * 0.65 / 0.35 == pytest.approx(111.4, abs=0.1)
    assert o.model_dcl == pytest.approx(109.7, abs=0.1)
    assert o.reason_discharge == "N-1: P1 if P2 trips"
    soft = Bench(cfg(n1_mode="soft"))
    soft.warm()
    assert soft.run(1).model_dcl == pytest.approx(43.6 * 0.64 / 0.35, abs=0.1)   # 79.7 A


def test_S25_frozen_driver():
    b = _discharging(seconds=10)
    b.frozen["P2"] = True
    b.run(59)
    assert b.state("P2") == "active"
    b.run(3)
    assert b.state("P2") == "stale"
    b.run(20)
    assert b.state("P2") == "offline"
    assert b.out.mode == BankMode.DEGRADED


def test_S26_return_with_stale_cached_values():
    b = _discharging()
    b.online["P3"] = False
    b.run(35)
    assert b.state("P3") == "offline"
    dcl_off = b.out.dcl
    b.online["P3"] = True
    b.set("P3", voltage=46.5, soc=40)                                  # 2 V off the bus median
    b.run(60)
    assert b.state("P3") == "quarantine"
    assert b.out.dcl <= dcl_off + 1e-9
    b.set("P3", voltage=48.5, soc=13)
    b.run(29)
    assert b.state("P3") == "quarantine"
    vals = [b.run(1) for _ in range(120)]
    assert b.state("P3") == "active"
    rises = [y.dcl - x.dcl for x, y in zip(vals, vals[1:])]
    assert max(rises) <= 5.0 + 1e-9


def test_S27_flapping_pack():
    b = _discharging()
    first_drop = None
    states = []
    dcls = []
    for cycle in range(12):
        b.online["P1"] = False
        for _ in range(15):
            o = b.run(1)
            dcls.append(o.dcl)
            if first_drop is None and o.dcl < 120:
                first_drop = o.dcl
        b.online["P1"] = True
        for _ in range(10):
            o = b.run(1)
            dcls.append(o.dcl)
            states.append((cycle, b.state("P1")))
    assert first_drop is not None
    assert all(s != "active" for c, s in states if c >= 2)
    assert b.track("P1").quarantine_s >= 240
    assert max(dcls[dcls.index(first_drop):]) <= first_drop + 1.0     # no oscillation
    assert o.model_dcl == pytest.approx(59 / (0.35 / 0.65), abs=0.5)  # 109.6 A without P1
    assert o.mode == BankMode.DEGRADED


def test_S28_all_packs_offline():
    b = _discharging()
    dcl0 = b.out.dcl
    for l in b.online:
        b.online[l] = False
    o = b.run(12)
    assert o.mode == BankMode.DEGRADED and o.ccl >= 0                 # stale grace with held values
    o = b.run(19)                                                     # t = 31 s
    assert o.mode == BankMode.FAILSAFE
    assert o.ccl == 0.0
    assert o.alarms["BmsCable"] == 2 and o.nr_online == 0
    assert o.cvl == pytest.approx(16 * 3.35)
    hist = [b.run(1).dcl for _ in range(12)]
    assert hist[-1] == pytest.approx(min(90.0, dcl0))
    assert all(y <= x + 1e-9 for x, y in zip(hist, hist[1:]))         # monotone towards emergency


def test_S28_failsafe_respects_sr21_cap():
    b = _discharging(measured_shares_verified=False)
    for l in b.online:
        b.online[l] = False
    o = b.run(45)
    assert o.mode == BankMode.FAILSAFE and o.dcl <= 59.0 and o.ccl == 0.0


def test_S29_fourth_pack_whitelist_and_auto():
    b = Bench(labels=("P1", "P2", "P3", "P4"))
    b.warm()
    o = b.run(40)
    assert "JK-SN-P4" not in b.core.tracks
    assert any("unknown pack serial" in e[2] for h in b.hist for e in h.events)

    b2 = Bench(cfg(auto_add_unknown=True, expected_pack_count=None), labels=("P1", "P2", "P3", "P4"))
    b2.online["P4"] = False
    b2.vals["P4"].update(installed_capacity=280.0, soc=20)
    b2.warm()
    b2.bank_current(-100.0)
    b2.run(300)
    before = b2.out
    b2.online["P4"] = True
    q = [b2.run(1) for _ in range(29)]
    assert b2.track("P4").state.value == "quarantine"
    assert all(x.dcl <= before.dcl + 1e-9 for x in q)
    assert q[-1].installed_capacity_ah == pytest.approx(924.0)
    after = [b2.run(1) for _ in range(5)]
    assert b2.track("P4").state.value == "active"
    assert after[-1].installed_capacity_ah == pytest.approx(1204.0)
    assert max(y.dcl - x.dcl for x, y in zip(after, after[1:])) <= 5.0 + 1e-9


def test_S30_manual_disconnect_then_excluded():
    b = _discharging(seconds=10)
    b.bank_current(-100.0, {"P1": 0.5, "P2": 0.0, "P3": 0.5})
    b.set("P2", voltage=49.5)
    o = b.run(12)
    assert b.track("P2").disconnected
    assert "P2" not in o.shares_discharge and "P2" not in o.shares_charge
    assert any("electrically disconnected" in e[2] for h in b.hist[-12:] for e in h.events)
    assert o.installed_capacity_ah == pytest.approx(924.0)
    b.core.set_excluded(b.serial["P2"], True)
    o = b.run(2)
    assert b.state("P2") == "excluded"
    assert o.installed_capacity_ah == pytest.approx(610.0)
