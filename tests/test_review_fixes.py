"""Review findings B1, M1-M6 and the owner's 04.10.2026 charge data point (scenario style)."""
import pytest
from bench import CAP, SERIAL, Bench, cfg

from battery_aggregator import Config, Core, PackConfig
from battery_aggregator.adapter import build_snapshot
from battery_aggregator.limits import charge_temp_factor
from battery_aggregator.model import BankMode
from battery_aggregator.sim import Plant, SimPack, run


def _charged_bench(**kw):
    b = Bench(cfg(**kw))
    for l in b.vals:
        b.set(l, voltage=55.4, cell_min=3.40, cell_max=3.42)
    b.warm()
    b.run(800)                                   # CVL ramped up to 56.8 V
    return b


def _bus_follows_cvl(b, seconds):
    """Chargers regulate the bus to the published CVL (worst case for integrator windup)."""
    outs = []
    for _ in range(seconds):
        for l in b.vals:
            b.set(l, voltage=b.out.cvl)
        outs.append(b.run(1))
    return outs


# ---------------------------------------------------------------- B1 charge path
def test_B1_explicit_open_contradicted_by_current_is_treated_closed():
    b = Bench()
    b.warm()
    b.run(400)
    b.set("P3", ccl=20.0, charge_fet=0)
    b.bank_current(60.0)                          # P3 still takes 33 % of the charge current
    o = b.run(3)
    assert not b.track("P3").chg_open
    assert o.model_ccl == pytest.approx(20 / 0.35, abs=0.05)       # P3 keeps bounding the bank
    assert any("reported open but pack carries" in e[2] for h in b.hist[-3:] for e in h.events)
    b.bank_current(60.0, {"P1": 0.5, "P2": 0.5, "P3": 0.0})       # now consistent: FET really open
    o = b.run(1)
    assert b.track("P3").chg_open and "P3" not in o.shares_charge


def test_B1_allow_flag_only_counts_as_fet_when_configured():
    b = Bench()
    b.warm()
    b.set("P3", allow_charge=0)
    b.bank_current(60.0, {"P1": 0.5, "P2": 0.5, "P3": 0.0})
    o = b.run(15)
    assert not b.track("P3").chg_open and o.model_ccl == 0.0       # BMS forbids -> bank 0 A
    b2 = Bench(cfg(allow_flag_is_fet=True))
    b2.warm()
    b2.set("P3", allow_charge=0)
    b2.bank_current(60.0, {"P1": 0.5, "P2": 0.5, "P3": 0.0})
    o = b2.run(1)
    assert b2.track("P3").chg_open and o.model_ccl > 0.0


def test_B1_open_path_of_a_pack_that_goes_blind_is_unknown_hence_closed():
    b = Bench()
    b.warm()
    b.run(400)
    b.set("P3", charge_fet=0, ccl=30.0)
    b.bank_current(60.0, {"P1": 0.5, "P2": 0.5, "P3": 0.0})
    b.run(1)
    assert b.track("P3").chg_open
    b.online["P3"] = False
    o = b.run(15)                                  # STALE -> blind: FET may have re-closed
    assert b.state("P3") == "stale" and not b.track("P3").chg_open
    assert o.model_ccl <= 30.0 / (0.8 / 3) + 1e-6  # held 30 A of P3 constrains again


# ---------------------------------------------------------------- M1 identity
def test_M1_missing_serial_keeps_binding_without_flapping():
    b = Bench()
    b.warm()
    b.run(10)
    b.serial["P2"] = None
    hist = [b.run(1) for _ in range(90)]
    assert b.core.tracks[SERIAL["P2"]].state.value == "active"
    assert not [e for o in hist for e in o.events if e[0] == "state"]
    assert sum(1 for o in hist for e in o.events if "kept bound" in e[2]) == 1
    b.serial["P2"] = "unknown"                     # placeholder = no identity either
    b.run(30)
    assert b.core.tracks[SERIAL["P2"]].state.value == "active" and "unknown" not in b.core.tracks


def test_M1_never_seen_serial_is_ignored_not_merged():
    b = Bench(cfg(auto_add_unknown=True, expected_pack_count=None), labels=("P1", "P2", "P3", "P4"))
    b.serial["P4"] = "0"
    b.warm()
    o = b.run(40)
    assert len(b.core.tracks) == 3 and "0" not in b.core.tracks
    assert any("has no serial, ignored" in e[2] for h in b.hist for e in h.events)
    assert o.installed_capacity_ah == pytest.approx(sum(CAP.values()))


def test_M1_two_packs_with_same_serial_are_not_merged():
    b = Bench()
    b.warm()
    b.bank_current(-100.0)
    b.run(60)
    dcl0 = b.out.dcl
    b.serial["P2"] = SERIAL["P1"]                  # cloned/defaulted serial, different currents
    hist = [b.run(1) for _ in range(40)]
    st = {l: b.core.tracks[SERIAL[l]].state.value for l in ("P1", "P2")}
    assert st["P1"] == "untrusted"                 # data cannot be attributed -> not trusted
    assert st["P2"] in ("stale", "offline")        # its own identity is silent -> held, blind
    assert all(o.dcl <= dcl0 + 1e-9 for o in hist)
    assert hist[-1].mode == BankMode.DEGRADED


def test_M1_auto_label_does_not_collide_with_configured_label():
    c = cfg(packs=(PackConfig(SERIAL["P2"], "P2", 314.0, 16, 250.0, 250.0),), auto_add_unknown=True,
            expected_pack_count=None)
    b = Bench(c, labels=("P1", "P2"))
    b.warm()
    labels = sorted(t.label for t in b.core.tracks.values())
    assert labels == ["P2", "P3"]


# ---------------------------------------------------------------- M2 gone pack + CVL
def test_M2_blind_pack_with_high_cell_gives_static_cvl_cap_no_windup_then_ramps_after_gone():
    b = _charged_bench()
    b.set("P3", cell_max=3.55, cell_max_id=9)
    o = b.run(1)
    assert o.target_cvl == pytest.approx(53.8, abs=0.01)
    b.online["P3"] = False
    b.run(12)                                      # P3 STALE -> blind, its data only held
    b.bank_current(30.0, {"P1": 0.5, "P2": 0.5})
    outs = _bus_follows_cvl(b, 300)                # bus follows CVL, held P3 cell 3.55 V
    assert b.state("P3") == "offline"
    assert all(o.target_cvl == pytest.approx(53.8, abs=0.01) for o in outs)  # static, no windup
    assert min(o.cvl for o in outs) >= 53.8 - 0.01
    assert "held cell cap of blind P3" in outs[-1].reason_voltage
    ccl_before = outs[-1].ccl
    outs = [b.run(1) for _ in range(400)]          # P3 GONE at ~630 s offline
    assert b.state("P3") == "gone"
    assert outs[-1].target_cvl == pytest.approx(56.8)            # clamp released at GONE ...
    cv = [o.cvl for o in outs]
    assert max(y - x for x, y in zip(cv, cv[1:])) <= 0.005 + 1e-9  # ... but only via the ramp
    cc = [ccl_before] + [o.ccl for o in outs]
    assert max(y - x for x, y in zip(cc, cc[1:])) <= 5.0 + 1e-9    # limits never jump up


# ---------------------------------------------------------------- M3 floor + anti-windup
def test_M3_cell_regulator_floor_when_bus_follows_cvl():
    b = _charged_bench()
    b.set("P1", cell_max=3.50)                     # top cell stuck above target (worst case)
    b.bank_current(40.0)
    outs = _bus_follows_cvl(b, 120)
    floor = 16 * 3.35
    assert min(o.cvl for o in outs) >= floor - 1e-9              # old law: down to 48.0 V
    assert outs[-1].cvl == pytest.approx(floor)


def test_M3_anti_windup_holds_without_charge_current_and_releases():
    b = _charged_bench()
    b.set("P1", cell_max=3.50)
    o = b.run(1)
    u0 = o.target_cvl
    assert u0 == pytest.approx(55.4 - 16 * 0.05, abs=0.01)       # 54.6 V
    for k in range(100):                           # charging stopped, bus relaxes 10 mV/s
        for l in b.vals:
            b.set(l, voltage=55.4 - 0.01 * (k + 1))
        o = b.run(1)
        assert o.target_cvl == pytest.approx(u0, abs=1e-9)        # held, no chasing down
    b.set("P1", cell_max=3.44)
    o = b.run(1)
    assert o.target_cvl == pytest.approx(56.8)
    vals = [b.run(1).cvl for _ in range(200)]
    assert vals[-1] > u0 and max(y - x for x, y in zip(vals, vals[1:])) <= 0.005 + 1e-9


# ---------------------------------------------------------------- M4 cvl_max
def test_M4_cvl_max_is_355_per_cell_and_only_lowerable():
    assert Config().cvl_max_v() == pytest.approx(16 * 3.55)
    assert any("cvl_max_v_per_cell" in e for e in Config(cvl_max_v_per_cell=3.60).validate())
    assert any("cvl_max_v_per_cell" in e for e in Config.from_dict({"cvl_max_v_per_cell": 3.56}).validate())
    assert not Config(cvl_max_v_per_cell=3.50).validate()
    bad = cfg(packs=(PackConfig("A", "A", 305.0, 15),))
    assert any("bank cells" in e for e in bad.validate())
    b = _charged_bench()
    for l in b.vals:
        b.set(l, cvl=58.4)                         # packs ask for 3.65 V/cell
    assert b.run(5).target_cvl == pytest.approx(56.8)
    b2 = _charged_bench(cvl_max_v_per_cell=3.50)
    assert b2.out.target_cvl == pytest.approx(56.0) and b2.out.cvl <= 56.0 + 1e-9


# ---------------------------------------------------------------- M5 temperature
@pytest.mark.parametrize("t,f", [(-5, 0.0), (-0.1, 0.0), (0, 0.0), (5, 0.5), (10, 1.0), (25, 1.0),
                                 (40, 1.0), (42.5, 0.5), (45, 0.0), (50, 0.0)])
def test_M5_lfp_charge_temperature_zones(t, f):
    assert charge_temp_factor(float(t), float(t), Config())[0] == pytest.approx(f)


def test_M5_most_restrictive_pack_sets_bank_ccl_and_config_only_narrower():
    b = Bench()
    b.warm()
    b.set("P1", temperature=42.5)                  # hot taper 50 %
    b.set("P2", temperature=5.0)                   # cold taper 50 %
    o = b.run(1)
    assert o.model_ccl == pytest.approx(100 / 0.36, abs=0.1)      # P2 (larger share) binds
    assert o.reason_charge.startswith("P2") and "cold taper" in o.reason_charge
    b.set("P3", temperature=46.0)
    assert b.run(1).model_ccl == 0.0                              # one pack too hot -> bank 0 A
    b.set("P3", temperature=20.0, dcl=59.0)
    b.set("P1", temperature=-15.0)                                # discharge cold taper
    assert b.run(1).model_dcl == pytest.approx(43.6 * 0.5 / 0.35, abs=0.1)
    assert any("t_charge_min_c" in e for e in Config(t_charge_min_c=-5.0).validate())
    assert any("t_charge_hot_stop_c" in e for e in Config(t_charge_hot_stop_c=50.0).validate())


# ---------------------------------------------------------------- M6 freshness
def test_M6_connected_zero_goes_offline_at_once_with_safe_fallback():
    b = Bench()
    b.warm()
    b.bank_current(-100.0)
    b.run(60)
    dcl0 = b.out.dcl
    b.set("P3", connected=0)
    o = b.run(1)
    assert b.state("P3") == "offline"                            # no 10-30 s STALE grace
    assert o.mode == BankMode.DEGRADED and o.dcl <= dcl0 + 1e-9
    assert o.model_dcl == pytest.approx(43.6 / (0.35 / 0.65), abs=0.5)   # world "P3 gone"
    b.set("P3", connected=1)
    b.run(2)
    assert b.state("P3") == "quarantine"


def test_M6_stale_pack_is_treated_as_offline():
    b = Bench()
    b.set("P3", temperature=8.0)                   # last temperature < 10 C (D3 margin)
    b.warm()
    b.run(300)
    assert b.out.ccl > 0
    b.online["P3"] = False
    o = b.run(12)
    assert b.state("P3") == "stale"
    p3 = next(p for p in o.packs if p.label == "P3")
    assert p3.role == "blind"
    assert o.target_ccl == 0.0 and "blind" in o.reason_charge     # safe fallback


def test_M6_data_age_and_future_timestamp():
    b = Bench()
    b.warm()
    snaps = b.snaps()
    snaps[0].timestamp = b.t + 100.0               # clock jump / garbage timestamp
    b.core.step(snaps, b.t)
    b.t += 1
    assert b.state("P1") == "stale"
    s = build_snapshot("com.victronenergy.battery.ttyUSB0", {"/Connected": 0, "/Serial": "X"}, 1.0)
    assert s.connected == 0


# ---------------------------------------------------------------- owner data point 04.10.2026
def _today_plant(ccl3=200.0):
    packs = [SimPack("P1", SERIAL["P1"], 305, soc=50, ccl=200.0, dcl=43.6),
             SimPack("P2", SERIAL["P2"], 314, soc=50, ccl=200.0, dcl=92.0),
             SimPack("P3", SERIAL["P3"], 305, soc=50, ccl=ccl3, dcl=59.0)]
    return Plant(packs, delay_s=2)


def _today_core():
    # owner defaults: inverter cap 210 A, CVL 3.55 V/cell, shadow, DCLs only as BMS-reported
    return Core(Config(packs=tuple(PackConfig(SERIAL[l], l, CAP[l]) for l in ("P1", "P2", "P3")),
                       expected_pack_count=3))


def test_today_bank_ccl_210A_instead_of_170A_from_pack3():
    core, plant = _today_core(), _today_plant()
    hist = run(core, plant, 900, lambda t: 300.0 if t >= 40 else 0.0)
    out = hist[-1][1]
    assert out.mode == BankMode.NORMAL
    assert out.ccl == pytest.approx(210.0) and out.ccl > 170.0     # DVCC today: 170 A from P3 alone
    assert out.reason_charge.startswith("SR-21") or out.reason_charge == "hardware cap"
    assert out.model_ccl == pytest.approx(210.0)
    for _, o, cur in hist:
        for p in plant.packs:                                     # every pack within its own CCL
            assert cur[p.label] <= p.limit("ccl", 0) + 1e-6
    assert not plant.trips


def test_today_throttled_pack_still_respected():
    core, plant = _today_core(), _today_plant(ccl3=60.0)
    hist = run(core, plant, 900, lambda t: 300.0 if t >= 40 else 0.0)
    out = hist[-1][1]
    s3 = out.shares_charge["P3"]
    # P3's 60 A binds via its share term or the N-1 check (soft: OCP unknown) - never 210 A
    # closed loop may add up to the (normalised) N-1 ceiling on top of the model; P3 stays protected
    assert abs(out.ccl - out.model_ccl) <= 1.0 and out.model_ccl <= 60.0 / s3 + 1e-6
    assert out.ccl < 210.0 and max(c["P3"] for _, _, c in hist) <= 60.0 + 1e-6
    over = [t for t, _, c in hist if c["P3"] > 60.0 * 1.05]
    assert len(over) <= 3
    assert "P3" in out.reason_charge


# ---------------------------------------------------------------- live findings (Cerbo, 4babc48)
def test_restart_starts_from_effective_cvl_not_floor():
    """A service restart must not cut charging: once the packs are admitted the CVL starts at
    the packs' effective CVL (56.8 V here), not at the 53.6 V failsafe floor ramping up at
    0.05 V/10 s; CCL/DCL start at the live target as the closed loop protects each pack."""
    b = Bench()
    o = b.run(1)
    while o.mode == BankMode.INIT:                                 # quarantine after the restart
        assert o.ccl == 0.0
        o = b.run(1)
    assert o.mode == BankMode.NORMAL                               # first step with admitted packs
    assert o.cvl == pytest.approx(56.8)
    assert o.ccl == pytest.approx(o.target_ccl) and o.ccl > 200.0
    assert o.dcl == pytest.approx(o.target_dcl)
    # a high cell still lowers the start value (seed never above the live CVL target)
    b2 = Bench()
    for l in b2.vals:
        b2.set(l, cell_max=3.58)
    b2.warm()
    o2 = b2.run(2)
    assert o2.cvl <= o2.target_cvl + 1e-9 and o2.cvl < 56.8
