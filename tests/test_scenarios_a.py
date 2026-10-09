"""Scenarios S01-S12 (docs/03_SZENARIEN.md, part A: normal operation, current sharing)."""
import pytest
from bench import CAP, REAL_DCL, Bench, cfg

from battery_aggregator import Core
from battery_aggregator.model import CHARGE, DISCHARGE, BankMode
from battery_aggregator.sim import Plant, SimPack, run


def test_S01_normal_charge_all_healthy():
    b = Bench()
    b.warm()
    b.bank_current(150.0)
    o = b.run(3)
    assert o.mode == BankMode.NORMAL
    assert o.term_ccl == pytest.approx(200 / 0.36, abs=0.1)      # 555.6 A share term
    assert o.model_ccl == pytest.approx(300.0)                    # ccl_hw binds
    assert o.reason_charge == "hardware cap"
    assert o.target_cvl == pytest.approx(56.8)
    assert all(v == 0 for v in o.alarms.values())


def test_S02_real_discharge_limit_today():
    b = Bench()
    b.warm()
    b.bank_current(-100.0)
    o = b.run(3)
    assert o.model_dcl == pytest.approx(43.6 / 0.35, abs=0.05)    # 124.6 A instead of 59 A
    assert o.reason_discharge.startswith("P1 43.6A / s 0.35")
    assert o.n1_dcl == pytest.approx(250 * 0.65 / 0.36, abs=0.5)  # ~451 A, not binding
    per_pack = {l: s * o.model_dcl for l, s in {"P1": 0.33, "P2": 0.34, "P3": 0.33}.items()}
    assert per_pack["P1"] <= 43.6
    # counter-checks from the scenario text
    assert min(REAL_DCL.values()) == 43.6 and sum(REAL_DCL.values()) == pytest.approx(194.6)
    assert 0.33 / 0.67 * sum(REAL_DCL.values()) > 43.6 * 1.4      # sum would overload P1 (~66 A at 1/3 share)


def test_S02_sr21_commissioning_cap_only_without_live_measurement():
    """Closed loop (default): the measured per-pack utilisation protects every pack, so the
    static SR-21 cap (59 A DCL) only applies while a pack's current measurement is missing or
    stale. Legacy mode keeps the cap until measured_shares_verified."""
    b = Bench(cfg(measured_shares_verified=False, inverter_ccl_cap_a=210.0))
    b.warm()
    b.bank_current(-100.0)
    o = b.run(400)
    assert o.model_dcl == pytest.approx(124.58, abs=2.5)       # sigma decays while learning
    assert o.target_dcl == pytest.approx(o.model_dcl) and o.dcl > 59.0
    assert o.target_ccl <= 210.0 and o.ccl <= 210.0                # D5: CCL cap = inverter cap
    b.frozen["P3"] = True                                          # no update for P3 any more
    b.online["P3"] = False
    o = b.run(5)                                                   # > ctl_max_age_s, not yet STALE
    assert o.target_dcl == pytest.approx(59.0) and o.dcl <= 59.0
    assert "SR-21" in o.reason_discharge
    legacy = Bench(cfg(measured_shares_verified=False, limit_control="legacy"))
    legacy.warm()
    legacy.bank_current(-100.0)
    o = legacy.run(400)
    assert o.target_dcl == pytest.approx(59.0) and o.dcl <= 59.0 and "SR-21" in o.reason_discharge


def test_S03_one_pack_throttles_charge_to_20A():
    b = Bench()
    b.warm()
    o = b.run(400)
    assert o.ccl == pytest.approx(300.0)
    b.set("P3", ccl=20.0)
    o = b.run(1)
    assert o.model_ccl == pytest.approx(20 / 0.35, abs=0.05)      # 57 A, not 20 A, not 420 A
    assert o.ccl == pytest.approx(20 / 0.35, abs=0.05)            # dropped in the same step


def test_S04_default_floor_08_over_n():
    """D2 (DECISIONS.md): share floor 0.8/N -> a fuller pack cannot drop below 0.267."""
    b = Bench()
    b.warm()
    b.run(400)
    b.set("P3", ccl=20.0)
    b.shares({"P1": 0.425, "P2": 0.425, "P3": 0.15})
    o = b.run(1)
    assert o.shares_charge["P3"] == pytest.approx(0.8 / 3)
    assert o.model_ccl == pytest.approx(20 / (0.8 / 3), abs=0.1)   # 75 A instead of 117 A


def test_S04_fuller_throttling_pack_and_ramp_design_floor():
    b = Bench(cfg(share_floor_equal_frac=0.0))                    # design numbers (floor 0.5*prior)
    b.warm()
    b.run(400)
    b.set("P3", ccl=20.0)
    b.shares({"P1": 0.425, "P2": 0.425, "P3": 0.15})
    o = b.run(1)
    assert o.model_ccl == pytest.approx(20 / 0.17, abs=0.1)       # ~117 A
    dropped = o.ccl
    b.shares({"P1": 0.45, "P2": 0.45, "P3": 0.10})               # P3 fuller -> share falls
    vals = [b.run(1).ccl for _ in range(120)]
    assert all(v == pytest.approx(dropped) for v in vals[:28])   # 30 s hold after the drop
    rises = [b2 - a for a, b2 in zip(vals, vals[1:])]
    assert max(rises) <= 5.0 + 1e-9
    assert all(r <= 0.01 * max(v, 50) + 1e-9 for r, v in zip(rises, vals))
    assert vals[-1] > dropped


def test_S05_B1_current_alone_never_opens_charge_path():
    """B1: P3 reads ~0 A while the bank charges, no FET info -> path stays CLOSED and P3
    keeps constraining the bank with its own 20 A limit (conservative), even after minutes."""
    b = Bench()
    b.warm()
    b.run(400)
    b.set("P3", ccl=20.0, cvl=55.0)
    b.bank_current(60.0, {"P1": 0.5, "P2": 0.5, "P3": 0.0})
    hist = [b.run(1) for _ in range(120)]
    assert not b.track("P3").chg_open
    assert not any(e[0] == "path" for o in hist for e in o.events)
    o = hist[-1]
    assert "P3" in o.shares_charge
    floor = 0.8 / 3
    assert o.model_ccl <= 20.0 / floor + 1e-6                       # still bounded by P3's 20 A
    assert o.target_cvl == pytest.approx(55.0)                      # P3 CVL still followed


@pytest.mark.parametrize("via", ["fet"])
def test_S05_pack_opens_charge_fet(via):
    b = Bench()
    b.warm()
    b.run(400)
    b.set("P3", ccl=20.0, cvl=55.0)
    o = b.run(1)
    assert o.ccl == pytest.approx(57.14, abs=0.05)
    if via == "fet":
        b.set("P3", charge_fet=0)
        o = b.run(1)
    else:
        b.bank_current(60.0, {"P1": 0.5, "P2": 0.5, "P3": 0.0})
        o = b.run(12)
    assert b.track("P3").chg_open
    # Doc expects 300 A (hw cap). With 2 packs left SR-01 (N-1 also at N=2) binds:
    # if one of P1/P2 trips the other carries everything -> ocp 250 A.
    if via == "fet":
        assert o.model_ccl == pytest.approx(250.0, abs=1.0)
    else:  # estimator also learned the skewed shares during the 10 s evidence window
        assert 200.0 <= o.model_ccl <= 250.0 + 1e-9
    if via == "fet":
        assert o.reason_charge.startswith("N-1")
    assert o.target_cvl == pytest.approx(56.8)                   # P3 no longer in CVL_follow
    assert o.nr_blocking_charge == 1
    if via == "fet":  # with current evidence the estimator also learned P3 ~ 0 meanwhile
        assert o.shares_charge["P1"] == pytest.approx(0.35 / 0.65, abs=1e-3)
    start = o.ccl
    hist = [b.run(1).ccl for _ in range(240)]
    if via == "fet":
        assert hist[25] == pytest.approx(start)                  # hold time
    assert hist[-1] == pytest.approx(b.out.target_ccl, abs=1)   # reached via ramp (~3 min)
    assert sum(1 for h in hist if h < b.out.target_ccl - 1) > 100


def test_S06_sum_bounds_high_limits():
    b = Bench()
    for l in b.vals:
        b.set(l, dcl=200.0)
    b.warm()
    o = b.run(2)
    assert o.model_dcl == pytest.approx(300.0)                   # dcl_hw
    b2 = Bench(cfg(dcl_hw_a=1000.0, ccl_hw_a=1000.0))
    for l in b2.vals:
        b2.set(l, dcl=200.0)
    b2.warm()
    b2.shares({"P1": 0.2, "P2": 0.2, "P3": 0.2}, sigma=0.0)
    o = b2.run(2)
    assert o.term_dcl == pytest.approx(200 / (0.8 / 3))          # share floored at 0.8/N (D2)
    assert o.model_dcl == pytest.approx(600.0)                   # never above sum DCL_i
    assert o.reason_discharge == "sum of pack limits"


def test_S07_sign_change_separate_estimators():
    b = Bench()
    b.warm()
    est = b.core.est
    b.bank_current(80.0)
    b.run(60)
    n_dis = est.packs["P1"].est[DISCHARGE].n
    ccl_before = b.out.model_ccl
    b.bank_current(-60.0)
    b.run(1)                                     # jump: dI/dt too large, no sample
    o = b.run(1)
    assert est.packs["P1"].est[CHARGE].n > 0
    assert o.model_ccl == pytest.approx(ccl_before)              # no jump in CCL
    assert o.model_dcl == pytest.approx(124.58, abs=0.3)         # from the discharge estimator
    assert est.packs["P1"].est[DISCHARGE].n >= n_dis
    b.bank_current(-10.0)                                        # below i_est_min: frozen
    b.run(1)
    n = est.packs["P1"].est[DISCHARGE].n
    b.run(20)
    assert est.packs["P1"].est[DISCHARGE].n == n


def test_S08_circulating_current_at_rest():
    b = Bench()
    b.warm()
    n0 = b.core.est.packs["P2"].est[CHARGE].n
    ccl0 = b.run(1).model_ccl
    b.set("P1", current=-6.0)
    b.set("P2", current=12.0)
    b.set("P3", current=-6.0)
    o = b.run(1)
    assert o.model_ccl == pytest.approx(ccl0)                    # unchanged in the step
    o = b.run(900)
    assert b.core.est.packs["P2"].est[CHARGE].n == n0          # no share update at I_bank ~ 0
    assert b.core.est.offset("P2") > 5.0 and b.core.est.offset("P1") < -2.0
    assert not [e for e in o.events if e[0] == "feedback"]
    assert o.k_fb_charge == 1.0
    # deviation (documented): learned offsets enter the formula (L_i - c_i)/s_i
    assert o.term_dcl == pytest.approx((43.6 - (-b.core.est.offset("P1"))) / 0.35, abs=0.1)


def test_S09_circulating_current_exceeds_cold_pack_ccl():
    b = Bench()
    b.warm()
    b.set("P2", ccl=5.0)
    b.set("P1", current=-11.0)
    b.set("P2", current=12.0)
    b.set("P3", current=-11.0)
    dcl0 = b.run(1).model_dcl
    events = []
    for _ in range(10):
        events += b.run(1).events
    assert any("circulating current" in e[2] for e in events)
    assert b.out.k_fb_charge == 1.0 and b.out.k_fb_discharge == 1.0
    assert b.out.model_dcl <= dcl0 + 1e-9


def _s10_plant():
    packs = [SimPack("P1", "JK-SN-0001", 305, r_ohm=0.010, soc=50, dcl=43.6),
             SimPack("P2", "JK-SN-0002", 314, r_ohm=0.01636, soc=50, dcl=92.0),
             SimPack("P3", "JK-SN-0003", 305, r_ohm=0.01636, soc=50, dcl=59.0)]
    return Plant(packs, delay_s=1)


def test_S10_model_error_feedback_without_learning():
    core = Core(cfg(i_est_min_a=1e6))                            # learning off: model stays wrong
    plant = _s10_plant()
    run(core, plant, 32, lambda t: 0.0)
    for l, s in {"P1": 0.33, "P2": 0.34, "P3": 0.33}.items():
        core.est.set_estimate(l, DISCHARGE, s, 0.01)
    hist = run(core, plant, 600, lambda t: -200.0, t0=32)
    fb = [e for _, o, _ in hist for e in o.events if e[0] == "feedback"]
    assert fb, "feedback must react to the measured overload"
    k = hist[-1][1].k_fb_discharge
    assert 0.6 < k < 0.8                                         # ~0.74 per scenario
    assert hist[-1][1].dcl == pytest.approx(124.58 * k, abs=2.0)
    tail = [c["P1"] for _, _, c in hist[-120:]]
    assert max(-x for x in tail) <= 43.6 * 1.05                  # P1 protected
    # no pendulum: published DCL monotone over the last 2 minutes
    dcls = [o.dcl for _, o, _ in hist[-120:]]
    assert max(dcls) - min(dcls) < 1.0


def test_S10_short_window_share_prevents_overload_with_learning():
    core = Core(cfg())
    plant = _s10_plant()
    run(core, plant, 32, lambda t: 0.0)
    for l, s in {"P1": 0.33, "P2": 0.34, "P3": 0.33}.items():
        core.est.set_estimate(l, DISCHARGE, s, 0.01)
    hist = run(core, plant, 600, lambda t: -200.0, t0=32)
    over = [t for t, o, c in hist if -c["P1"] > 43.6 * 1.05]
    assert len(over) <= 3                                        # never > 5 % for more than 3 s
    assert hist[-1][2]["P1"] >= -43.6 * 1.02


def test_S11_noisy_shares_are_conservative_and_stable():
    b = Bench()
    b.warm()
    b.bank_current(-100.0)
    b.run(300)
    seq = []
    for k in range(400):
        s1 = 0.2 if k % 2 else 0.5
        rest = (1 - s1) / 2
        b.bank_current(-100.0, {"P1": s1, "P2": rest, "P3": rest})
        seq.append(b.run(1))
    s_eff = seq[-1].shares_discharge["P1"]
    assert s_eff >= 0.5
    # doc: ~73 A (s_eff ~0.6); the asymmetric filter (SR-03: fast up, slow down) is more
    # conservative here: s_eff ~0.75 -> ~58 A
    assert 50.0 < seq[-1].model_dcl < 90.0
    rises = sum(1 for a, c in zip(seq[-150:], seq[-149:]) if c.dcl > a.dcl + 1.0)
    assert rises <= 5


def test_S12_dead_shunt_floor_and_warning():
    b = Bench()
    b.warm()
    b.set("P2", charge_fet=1, discharge_fet=1)
    b.bank_current(-100.0, {"P1": 0.5, "P2": 0.0, "P3": 0.5})
    b.shares({"P1": 0.5, "P2": 0.0, "P3": 0.5}, sigma=0.0)
    events = []
    for _ in range(15):
        o = b.run(1)
        events += o.events
    floor = 0.5 * CAP["P2"] / sum(CAP.values())
    assert o.shares_discharge["P2"] >= floor - 1e-9                # never 0, never unbounded
    assert not b.track("P2").dchg_open
    assert any("implausible" in e[2] for e in events)


def test_key_number_prior_fallback_88A():
    b = Bench()
    b.run(32)                                    # no share estimate -> capacity prior x 1.5
    o = b.run(1)
    assert o.shares_discharge["P1"] == pytest.approx(1.5 * 305 / 924, abs=1e-4)
    assert o.model_dcl == pytest.approx(43.6 / (1.5 * 305 / 924), abs=0.1)   # ~88 A


def test_fmea_T_S01_n1_example_295A():
    from battery_aggregator.limits import Member, evaluate_world
    ms = [Member(l, 150.0, s, 0.0, None, "active") for l, s in (("A", 0.35), ("B", 0.33), ("C", 0.32))]
    r = evaluate_world(ms, {m.label: m.share for m in ms}, "soft")
    assert r.model == pytest.approx(150 / 0.35, abs=0.1)        # 428.6 A normal
    # FMEA quotes 295 A (failure of the largest pack A). The minimum over all k is the
    # failure of B: A then carries 0.35/0.67 -> 150*0.67/0.35 = 287.1 A.
    assert 150 * 0.65 / 0.33 == pytest.approx(295.5, abs=0.1)
    assert r.n1 == pytest.approx(150 * 0.67 / 0.35, abs=0.1)
    assert r.value == pytest.approx(r.n1)
