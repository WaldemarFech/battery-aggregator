"""Restart hold (design 5.3a, 0.2.1): no charge stop / CVL step across a service restart."""
import pytest
from bench import Bench, cfg

from battery_aggregator import Core, SafeCore
from battery_aggregator.adapter import Adapter, FakeBus
from battery_aggregator.model import BankMode
from battery_aggregator.persistence import load_state, save_state

WALL0 = 1.8e9


def _running(tmp_path, **kw):
    """A bank that charged for a while, its state file, and what it last published."""
    b = Bench(cfg(**kw))
    b.warm()
    b.run(300)
    assert b.out.mode == BankMode.NORMAL and b.out.ccl > 100.0 and b.out.cvl == pytest.approx(56.8)
    path = str(tmp_path / "state.json")
    save_state(path, b.core.export_state())
    saved_wall = WALL0 + b.t - 1.0
    return b.out, load_state(path), saved_wall


def _restart(state, saved_wall, age_s, **kw):
    nb = Bench(cfg(**kw), core=Core(cfg(**kw), persisted=state))
    wall = saved_wall + age_s
    return nb, (lambda n=1: nb.run(n, wall=wall + nb.t - 0.0))


def test_fresh_state_no_charge_stop_across_restart(tmp_path):
    last, state, w = _running(tmp_path)
    assert state["outputs"]["ccl"] == pytest.approx(last.ccl)
    nb, run = _restart(state, w, age_s=5.0)
    outs = [run() for _ in range(40)]
    init = [o for o in outs if o.mode == BankMode.INIT]
    assert len(init) >= 25                                      # quarantine 30 s
    for o in init:                                              # held, never the INIT defaults
        assert o.ccl == pytest.approx(last.ccl) and o.cvl == pytest.approx(last.cvl)
        assert o.dcl == pytest.approx(last.dcl) and o.allow_to_charge
        assert "restart hold" in o.reason_charge
    first = next(o for o in outs if o.mode == BankMode.NORMAL)
    assert first.cvl == pytest.approx(56.8) and first.ccl >= last.ccl - 1e-6
    assert min(o.ccl for o in outs) >= last.ccl - 1e-6 and min(o.cvl for o in outs) >= 56.8 - 1e-6
    ev = " ".join(e[2] for o in outs for e in o.events)
    assert "restart hold: CVL 56.80V" in ev and "restart hold ended: packs admitted" in ev


def test_stale_state_is_conservative(tmp_path):
    last, state, w = _running(tmp_path)
    nb, run = _restart(state, w, age_s=600.0)                   # > restart_hold_max_age_s
    o = run()
    assert o.mode == BankMode.INIT and o.ccl == 0.0 and o.cvl == pytest.approx(53.6)
    assert "restart hold skipped" in " ".join(e[2] for e in o.events)
    nb2, run2 = _restart(state, w, age_s=-30.0)                 # clock went backwards: not trusted
    assert run2().ccl == 0.0


def test_no_hold_without_state_or_when_disabled(tmp_path):
    o = Bench().run(1)
    assert o.mode == BankMode.INIT and o.ccl == 0.0 and o.cvl == pytest.approx(53.6)
    last, state, w = _running(tmp_path)
    nb, run = _restart(state, w, age_s=5.0, restart_hold_s=0.0)
    assert run().ccl == 0.0


def test_pack_lowers_limit_during_window_applied_at_once_and_never_raised(tmp_path):
    last, state, w = _running(tmp_path)
    nb, run = _restart(state, w, age_s=5.0)
    run(5)
    nb.set("P2", ccl=20.0, cvl=55.2)
    o = run()
    assert o.mode == BankMode.INIT
    assert o.ccl == pytest.approx(o.target_ccl) and o.ccl <= 20.0 / 0.30 and o.ccl < last.ccl
    assert o.cvl == pytest.approx(55.2)
    nb.set("P2", ccl=200.0, cvl=56.8)                           # back up: the hold only ratchets down
    o = run(3)
    assert o.mode == BankMode.INIT and o.ccl <= 20.0 / 0.30 and o.cvl == pytest.approx(55.2)


def test_cell_and_temperature_guards_win_during_window(tmp_path):
    last, state, w = _running(tmp_path)
    nb, run = _restart(state, w, age_s=5.0)
    run(3)
    nb.set("P3", temperature=-2.0)                              # charge blocked < 0 C
    o = run()
    assert o.mode == BankMode.INIT and o.ccl == 0.0 and not o.allow_to_charge
    nb2, run2 = _restart(state, w, age_s=5.0)
    run2(3)
    nb2.set("P1", cell_max=3.61)                                # >= v_cell_hard
    o = run2()
    assert o.ccl == 0.0 and o.cvl < last.cvl


def test_pack_alarm_or_blind_pack_ends_hold(tmp_path):
    last, state, w = _running(tmp_path)
    nb, run = _restart(state, w, age_s=5.0)
    run(3)
    nb.set("P1", alarms={"HighTemperature": 2})
    o = run()
    assert o.ccl == 0.0 and o.cvl == pytest.approx(53.6)
    assert "restart hold ended: P1 alarm HighTemperature" in " ".join(e[2] for e in o.events)
    nb.set("P1", alarms={})
    assert run(5).ccl == 0.0                                    # ended for good
    nb2, run2 = _restart(state, w, age_s=5.0)
    run2(3)
    nb2.set("P2", voltage=float("nan"))                         # untrusted -> blind
    o = run2()
    assert o.ccl == 0.0 and o.cvl == pytest.approx(53.6)


def test_window_is_never_exceeded(tmp_path):
    last, state, w = _running(tmp_path)
    nb, run = _restart(state, w, age_s=5.0, quarantine_s=90.0)  # admission slower than the hold
    outs = [run() for _ in range(60)]
    assert all(o.mode == BankMode.INIT for o in outs)
    assert all(o.ccl == pytest.approx(last.ccl) for o in outs[:44])
    assert all(o.ccl == 0.0 and o.cvl <= 53.6 + 1e-9 for o in outs[45:])


def test_held_outputs_are_not_re_persisted(tmp_path):
    """Back-to-back restarts cannot extend the hold: only NORMAL/DEGRADED steps refresh it."""
    last, state, w = _running(tmp_path)
    nb, run = _restart(state, w, age_s=5.0)
    run(10)
    assert nb.core.export_state()["outputs"]["wall"] == pytest.approx(w)


def test_corrupt_outputs_record_ignored(tmp_path):
    last, state, w = _running(tmp_path)
    for bad in ({"ccl": "x"}, {"ccl": 1.0, "dcl": 1.0, "cvl": 99.0, "wall": w}, None, []):
        st = dict(state, outputs=bad)
        nb, run = _restart(st, w, age_s=5.0)
        assert run().ccl == 0.0


@pytest.mark.parametrize("bms,mon,level", [(512, "com.victronenergy.battery/512", 0),
                                            (512, "com.victronenergy.battery/3", 0),
                                            (-1, "com.victronenergy.battery/512", 0),
                                            (-1, "com.victronenergy.battery/3", 1)])
def test_S37_half_selection_is_only_a_warning(bms, mon, level):
    c = cfg(shadow=False)
    bus = FakeBus()
    bus.settings = {"/Settings/SystemSetup/BmsInstance": bms, "/Settings/SystemSetup/BatteryService": mon}
    ad = Adapter(bus, SafeCore(Core(c)), c, clock=lambda: 0.0, wall=lambda: WALL0)
    assert ad.tick().alarms["InternalFailure"] == level


# ---------------------------------------------------------------- 0.2.2: DCL without SR-21 start-up cap
OWNER = dict(measured_shares_verified=False, commissioning_ccl_cap_a=210.0, commissioning_dcl_cap_a=59.0,
             ccl_hw_a=1000.0, dcl_hw_a=1000.0)


def _owner_running(tmp_path):
    b = Bench(cfg(**OWNER))
    for l in b.vals:
        b.set(l, dcl=200.0)
    b.warm()
    b.bank_current(-30.0)                                       # live per-pack currents -> SR-21 lifted
    b.run(300)
    assert b.out.mode == BankMode.NORMAL and b.out.dcl > 150.0
    path = str(tmp_path / "state.json")
    save_state(path, b.core.export_state())
    return b.out, load_state(path), WALL0 + b.t - 1.0


def _owner_restart(state, w, age_s):
    nb = Bench(cfg(**OWNER), core=Core(cfg(**OWNER), persisted=state))
    for l in nb.vals:
        nb.set(l, dcl=200.0, current=-10.0)
    return nb, (lambda n=1: nb.run(n, wall=w + age_s + nb.t))


def test_dcl_held_without_sr21_cap_and_no_dip_at_handover(tmp_path):
    last, state, w = _owner_running(tmp_path)
    nb, run = _owner_restart(state, w, 5.0)
    outs = [run() for _ in range(45)]
    assert any(o.mode == BankMode.INIT for o in outs) and outs[-1].mode == BankMode.NORMAL
    assert min(o.dcl for o in outs) >= 0.98 * last.dcl          # never the 59 A start-up cap
    assert all(o.dcl == pytest.approx(last.dcl, rel=0.02) for o in outs if o.mode == BankMode.INIT)


def test_dcl_sr21_cap_without_fresh_state(tmp_path):
    last, state, w = _owner_running(tmp_path)
    nb, run = _owner_restart(state, w, 600.0)
    o = run()
    assert o.mode == BankMode.INIT and o.dcl == pytest.approx(59.0)


def test_dcl_pack_limit_low_cell_and_alarm_win_during_window(tmp_path):
    last, state, w = _owner_running(tmp_path)
    nb, run = _owner_restart(state, w, 5.0)
    run(3)
    nb.set("P2", dcl=30.0)
    o = run()
    assert o.mode == BankMode.INIT and o.dcl == pytest.approx(o.model_dcl) and o.dcl < 30.0 / 0.30
    nb.set("P2", dcl=200.0)
    assert run(2).dcl < 30.0 / 0.30                             # ratchet
    nb2, run2 = _owner_restart(state, w, 5.0)
    run2(3)
    nb2.set("P1", cell_min=2.85)                                # below v_cell_low_hard -> 0 A
    assert run2().dcl == 0.0
    nb3, run3 = _owner_restart(state, w, 5.0)
    run3(3)
    nb3.set("P3", alarms={"LowCellVoltage": 2})                 # alarm ends the hold -> INIT values
    o = run3()
    assert o.dcl <= 59.0 + 1e-9 and o.ccl == 0.0
