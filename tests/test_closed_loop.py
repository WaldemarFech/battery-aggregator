"""Closed-loop per-pack limit protection (control.py, design 4.2a, scenarios S41-S48).

Owner requirement: P1/P2 say 50 A, P3 says "nearly full, max 10 A". Allowing 110 A is unsafe
(P3 would carry ~30 A), allowing min x N = 30 A wastes the other packs. The bank limit must be
driven by what P3 really takes: cut drastically when P3 is over its limit, raise slowly when
it is below, never keep a pack over its limit for long.
"""
import pytest

from battery_aggregator import Core
from battery_aggregator.config import Config
from battery_aggregator.control import LimitController
from battery_aggregator.model import CHARGE, DISCHARGE
from battery_aggregator.sim import Plant, SimPack, run
from battery_aggregator.tools.control_sim import LABELS, make_cfg, metrics, setup, simulate


def _over_runs(hist, watch, limit, sign=1.0, tol=1.0):
    """Lengths of consecutive runs with the watched pack above tol x its limit."""
    runs, n = [], 0
    for t, _, c in hist:
        if sign * c[watch] > tol * limit(t):
            n += 1
        elif n:
            runs.append(n)
            n = 0
    return runs + ([n] if n else [])


# ---------------------------------------------------------------- the control law itself
def test_S41_cut_law_owner_numbers():
    """110 A allowed, P3 reaches 15 A at its 10 A limit (u = 1.5): the headroom is halved at
    once (110 -> 55 A); the next, still lagging measurement does not cut again."""
    ctl = LimitController(Config(), CHARGE)
    ev = []
    kw = dict(anchor=110.0, ceiling=110.0, floor=0.0, live=True, members=[("P1", "active"),
              ("P2", "active"), ("P3", "active")], events=ev)
    assert ctl.update(0.0, ratios=[("P1", 0.95), ("P3", 1.5)], flow=110.0, **kw) == pytest.approx(55.0)
    assert ev and ev[-1][0] == "feedback" and "P3" in ev[-1][2]
    # inverter has not reacted yet: same currents measured -> predicted u at 55 A = 0.75
    assert ctl.update(1.0, ratios=[("P3", 1.5)], flow=110.0, **kw) == pytest.approx(55.0)
    # it did react and P3 is still over (it takes more than expected) -> cut again, confirmed
    # by the median-of-3 on the second sample (a single sample cuts only above 1.3)
    ctl.update(2.0, ratios=[("P3", 1.2)], flow=55.0, **kw)
    assert ctl.update(3.0, ratios=[("P3", 1.2)], flow=55.0, **kw) <= 55.0 * 0.95 / 1.2 + 1e-9


def test_S41_cut_is_bounded_by_floor_and_proportional_correction():
    ctl = LimitController(Config(), CHARGE)
    kw = dict(anchor=110.0, ceiling=110.0, floor=10.0, live=True, members=[("P3", "active")], events=[])
    # 20 A at a 10 A limit: factor clamps at ctl_cut_min_factor 0.25 above the floor
    assert ctl.update(0.0, ratios=[("P3", 2.0)], flow=110.0, **kw) == pytest.approx(10 + 100 * 0.25)
    ctl = LimitController(Config(), CHARGE)
    # small overshoot: at least the proportional correction out * 0.95 / u
    assert ctl.update(0.0, ratios=[("P3", 1.04)], flow=110.0, **kw) == pytest.approx(110 * 0.95 / 1.04)


def test_S41_no_raise_while_not_binding_anti_windup():
    ctl = LimitController(Config(), CHARGE)
    kw = dict(anchor=30.0, ceiling=110.0, floor=10.0, live=True, members=[("P3", "active")], events=[])
    for k in range(120):
        v = ctl.update(float(k), ratios=[("P3", 0.2)], flow=10.0, **kw)
    assert v == pytest.approx(30.0)                              # load below the limit: hold
    for k in range(120, 130):
        v = ctl.update(float(k), ratios=[("P3", 0.5)], flow=v, **kw)
    assert 30.0 < v <= 30.0 + 10 * 2.0 + 1e-9                   # binding: slow, rate-limited raise


def test_S41_no_closed_loop_gain_without_live_measurement():
    ctl = LimitController(Config(), CHARGE)
    kw = dict(ceiling=110.0, floor=10.0, members=[("P3", "active")], events=[])
    v = 30.0
    for k in range(200):
        v = ctl.update(float(k), anchor=30.0, live=True, ratios=[("P3", 0.5 * v / 30)], flow=v, **kw)
    assert v > 50.0
    v = ctl.update(200.0, anchor=30.0, live=False, ratios=[("P3", 0.5)], flow=v, **kw)
    assert v == pytest.approx(30.0)                              # uncertain member: model only


# ---------------------------------------------------------------- owner scenario (closed loop)
def test_S42_owner_scenario_50_50_10_named():
    """P3 near full takes less than 1/3: settle well above min x N = 30 A, below 110 A."""
    hist, d, watch, limit, t_on = simulate("owner", "pi", 300)
    m = metrics(hist, d, watch, limit, t_on)
    assert m["over5_s"] == 0 and m["u_max"] <= 1.0                # P3 never over its limit
    assert 40.0 < m["bank_settled"] < 60.0                       # ~48.5 A: not 30, not 110
    assert m["ripple"] < 2.0                                     # bounded ripple
    tail = [c for _, _, c in hist[-60:]]
    assert all(c["P3"] <= 10.0 for c in tail) and min(c["P3"] for c in tail) > 8.5
    assert all(c["P1"] <= 50.0 and c["P2"] <= 50.0 for c in tail)
    legacy = metrics(*simulate("owner", "legacy", 300))
    assert legacy["bank_settled"] < 30.0 < m["bank_settled"] - 15.0   # old: stuck at min x N


def test_S42_owner_scenario_step_back_under_limit_within_cycles():
    """All packs at 50 A while charging hard; P3 then says 'nearly full, 10 A' while carrying
    ~38 A. P3 must be back under its limit within N = 2 cycles, then the bank re-opens."""
    hist, d, watch, limit, t_on = simulate("owner_step", "pi", 300)
    runs = _over_runs(hist, watch, limit)
    assert runs and max(runs) <= 2
    m = metrics(hist, d, watch, limit, t_on)
    assert 40.0 < m["bank_settled"] < 110.0 and m["ripple"] < 2.0
    assert max(sum(c.values()) for t, _, c in hist if t > t_on + 62) < 60.0   # never back to naive


def test_S43_hungry_pack_takes_more_than_its_share():
    """Stale estimate 1/3, P3 really takes 0.48: back under its limit within 3 cycles."""
    hist, d, watch, limit, t_on = simulate("hungry", "pi", 300)
    assert max(_over_runs(hist, watch, limit)) <= 3
    m = metrics(hist, d, watch, limit, t_on)
    assert m["bank_settled"] <= 10.0 / 0.48 + 0.5 and m["bank_settled"] > 17.0
    assert all(c["P3"] <= 10.0 for _, _, c in hist[-200:])


def test_S44_ccl_taper_steps_feedforward():
    """P3 CCL 50 -> 40 -> 30 -> 20 -> 10 A: every step is answered in the same cycle (limit
    feedforward), and the loop re-opens to what P3 really takes."""
    hist, d, watch, limit, t_on = simulate("taper", "pi", 400)
    assert max(_over_runs(hist, watch, limit)) <= 1
    m = metrics(hist, d, watch, limit, t_on)
    assert m["bank_settled"] > 45.0
    legacy = metrics(*simulate("taper", "legacy", 400))
    assert legacy["bank_settled"] < 30.0


def test_S45_discharge_direction():
    hist, d, watch, limit, t_on = simulate("discharge", "pi", 300)
    m = metrics(hist, d, watch, limit, t_on)
    assert d == DISCHARGE and m["over5_s"] == 0 and m["u_max"] <= 1.0
    assert 40.0 < m["bank_settled"] < 110.0 and m["ripple"] < 2.0


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_S46_noise_and_delays(seed):
    """1 A current noise, 2 s measurement lag, 3 s inverter delay."""
    hist, d, watch, limit, t_on = simulate("noisy", "pi", 600, seed=seed)
    m = metrics(hist, d, watch, limit, t_on)
    assert m["over5_s"] <= 2 and m["u_max"] < 1.08                # brief, small excursions only
    assert m["bank_settled"] > 40.0 and m["ripple"] < 10.0
    hist, d, watch, limit, t_on = simulate("noisy_h", "pi", 600, seed=seed)
    assert max(_over_runs(hist, watch, limit, tol=1.05)) <= 7      # loop dead time ~5 s
    assert metrics(hist, d, watch, limit, t_on)["bank_settled"] <= 21.0


# ---------------------------------------------------------------- topology changes
def test_S47_pack_disappears_mid_control():
    core, plant, demand, d, watch, limit, t_on = setup("owner")
    hist = run(core, plant, 120, demand, t0=t_on - 5)
    assert sum(hist[-1][2].values()) > 40.0
    p2 = next(p for p in plant.packs if p.label == "P2")
    p2.online, p2.charge_fet, p2.discharge_fet = False, False, False   # unplugged
    hist2 = run(core, plant, 900, demand, t0=t_on + 115)
    ev = [e for _, o, _ in hist2 for e in o.events]
    assert any("controller reset" in e[2] for e in ev)
    assert max(_over_runs(hist2, watch, limit)) <= 3
    assert all(o.ccl >= 0.0 for _, o, _ in hist2)
    tail = hist2[-60:]
    i3 = [c["P3"] for _, _, c in tail]
    assert max(i3) <= 10.0 and min(i3) > 8.0                     # closed loop resumed on 2 packs
    assert 20.0 < sum(tail[-1][2].values()) < 40.0


def _late_third_pack(cfg):
    packs = [SimPack(l, f"JK-SIM-{i}", 280.0, soc=(90.0 if l == "P3" else 85.0),
                     ccl=(10.0 if l == "P3" else 50.0), dcl=50.0) for i, l in enumerate(LABELS, 1)]
    packs[2].online = packs[2].charge_fet = packs[2].discharge_fet = False   # not yet plugged in
    plant = Plant(packs, delay_s=2)
    core = Core(cfg)
    demand = lambda t: 0.0 if t < 100 else 200.0                 # noqa: E731
    before = run(core, plant, 300, demand)
    packs[2].online = packs[2].charge_fet = packs[2].discharge_fet = True
    return core, before, run(core, plant, 400, demand, t0=300)


def test_S48_pack_appears_closed_loop_resumes():
    """P3 is plugged in while the bank charges at ~99 A on two packs: one physical inrush
    sample, then the loop protects P3 and re-opens to what P3 really takes."""
    core, before, hist = _late_third_pack(make_cfg("pi"))
    assert sum(before[-1][2].values()) > 90.0                   # 2 x 50 A before
    ev = [e for _, o, _ in hist for e in o.events]
    assert any("controller reset" in e[2] for e in ev)
    assert max(_over_runs(hist, "P3", lambda t: 10.0, tol=1.05)) <= 2
    tail = [c for _, _, c in hist[-60:]]
    assert all(c["P3"] <= 10.0 for c in tail)
    assert sum(tail[-1].values()) > 40.0                         # third pack adds, not min x N


def test_S48_dynamic_discovery_without_pack_list():
    """No pack list and no count configured: packs are found on the bus by serial; the third
    one joins at runtime. Without configured JK OCP values N-1 falls back to BMS limits (D4),
    which is conservative but never crashes and never overloads P3."""
    core, before, hist = _late_third_pack(make_cfg("pi", packs=(), expected_pack_count=None))
    assert len(core.tracks) == 3 and {t.label for t in core.tracks.values()} == set(LABELS)
    ev = [e for _, o, _ in hist for e in o.events]
    assert any("new pack" in e[2] for e in ev) and any("controller reset" in e[2] for e in ev)
    assert "N-1" in before[-1][1].reason_charge and before[-1][1].ccl == pytest.approx(50.0)
    assert all(c["P3"] <= 10.0 for _, _, c in hist[5:])
    assert all(o.ccl > 0.0 for _, o, _ in hist)


def test_S48_legacy_mode_still_selectable():
    hist, d, watch, limit, t_on = simulate("owner", "legacy", 120)
    assert hist[-1][1].k_fb_charge == 1.0
    with pytest.raises(ValueError):
        Core(make_cfg("bogus"))


@pytest.mark.parametrize("d", [CHARGE, DISCHARGE])
def test_S49_cautious_shares_not_counted_twice(d):
    """Live Cerbo: learned shares 0.495/0.51/0.495 (sum ~1.5, deliberately cautious). Owner facts:
    200 A per pack, busbar 1000 A, inverter ~210 A. The cautious shares may shape the open-loop
    anchor, but the closed-loop ceiling uses Kirchhoff-normalised shares - otherwise N-1 would
    read 'one pack carries everything' (200 A) and cap the bank below what it can do."""
    cfg = make_cfg("pi", packs=tuple(type(p)(p.serial, p.label, 280.0, 16, 200.0, 200.0)
                                     for p in make_cfg("pi").packs),
                   ccl_hw_a=1000.0, dcl_hw_a=1000.0, inverter_ccl_cap_a=210.0,
                   measured_shares_verified=False)
    packs = [SimPack(l, f"JK-SIM-{i}", 280.0, soc=50.0, ccl=200.0, dcl=200.0) for i, l in enumerate(LABELS, 1)]
    plant = Plant(packs, delay_s=2)
    core = Core(cfg)
    for l, s in {"P1": 0.495, "P2": 0.51, "P3": 0.495}.items():
        core.est.set_estimate(l, d, s, 0.0)
    sign = 1.0 if d == CHARGE else -1.0
    hist = run(core, plant, 500, lambda t: 0.0 if t < 60 else sign * 450.0)
    o = hist[-1][1]
    flow = sign * sum(hist[-1][2].values())
    if d == CHARGE:
        assert o.ccl == pytest.approx(210.0, abs=0.5) and flow == pytest.approx(210.0, abs=1.0)
    else:
        assert o.dcl > 390.0 and flow > 390.0                      # 3 x 200 A packs, N-1 ~400 A
    assert all(abs(c) <= 200.0 for c in hist[-1][2].values())
