"""Randomised property tests with fixed seeds (hypothesis is optional and not required).

Invariants (design 8.2, FMEA SR-01/02/05/09):
- no exception, all outputs finite, 0 <= CCL/DCL <= hw cap, CVL within configured band
- model safety: s_i * limit + c_i <= L_i for every member pack
- N-1: min(1, s_i/(1-s_k)) * limit <= ocp_i for all k != i
- monotonicity: lowering a pack limit / CVL never raises the target in the same step
- SR-02: losing a pack never raises the model limit
- rate limits: rises <= ramp bound, drops immediate
- fail-safe monotone: all packs gone -> CCL 0, DCL never rises
- determinism
"""
import copy
import math
import random

import pytest
from bench import SERIAL, Bench, cfg

from battery_aggregator import Core, PackSnapshot
from battery_aggregator.model import CHARGE, DISCHARGE, BankMode

SEEDS = list(range(25))
LABELS = ("P1", "P2", "P3")


def _random_values(rng, label):
    return dict(voltage=rng.uniform(48.0, 55.0), current=0.0, temperature=rng.uniform(-5, 50),
                soc=rng.uniform(0, 100), installed_capacity=rng.choice([280, 305, 314]),
                cvl=rng.uniform(52.0, 56.8), ccl=rng.choice([0.0, 5.0, 20.0, rng.uniform(0, 200), 200.0]),
                dcl=rng.choice([0.0, 20.0, rng.uniform(0, 200), 200.0]), allow_charge=1, allow_discharge=1,
                cell_min=rng.uniform(2.85, 3.3), cell_max=rng.uniform(3.3, 3.65), cells=16,
                temp_min_id=1, cell_max_id=rng.randint(1, 16), alarms={})


def _random_bench(rng, **kw):
    c = cfg(**kw)
    b = Bench(c)
    for l in LABELS:
        b.vals[l] = _random_values(rng, l)
    b.warm(32)
    b.shares({l: rng.uniform(0.15, 0.5) for l in LABELS}, sigma=rng.uniform(0, 0.05))
    for l in LABELS:
        b.core.est.packs[l].offset = rng.uniform(-5, 5)
    return b


def _check_outputs(o, c):
    for x in (o.ccl, o.dcl, o.cvl, o.target_ccl, o.target_dcl):
        assert isinstance(x, float) and math.isfinite(x)
    assert 0.0 <= o.ccl <= c.ccl_hw_a and 0.0 <= o.dcl <= c.dcl_hw_a
    assert c.cells * c.cvl_min_v_per_cell - 1e-9 <= o.cvl <= c.cells * c.cvl_max_v_per_cell + 1e-9
    for v in o.published().values():
        assert not (isinstance(v, float) and not math.isfinite(v))


@pytest.mark.parametrize("seed", SEEDS)
def test_model_safety_and_n1(seed):
    rng = random.Random(seed)
    b = _random_bench(rng)
    o = b.run(1)
    _check_outputs(o, b.cfg)
    if o.mode not in (BankMode.NORMAL, BankMode.DEGRADED):
        return
    for d, limit, shares in ((CHARGE, o.target_ccl, o.shares_charge), (DISCHARGE, o.target_dcl, o.shares_discharge)):
        published = o.ccl if d == CHARGE else o.dcl
        assert published <= limit + 1e-9                         # ramp never above target
        for st in o.packs:
            if st.label not in shares:
                continue
            lim = st.ccl if d == CHARGE else st.dcl
            off = b.core.est.offset(st.label)
            c_d = max(0.0, off) if d == CHARGE else max(0.0, -off)
            # circulating current alone may exceed L_i; then the bank limit must be 0
            assert shares[st.label] * limit <= max(0.0, lim - c_d) + 1e-6, (d, st.label)
        for k in shares:
            for i in shares:
                if i == k:
                    continue
                after = min(1.0, shares[i] / max(0.05, 1 - shares[k]))
                assert after * limit <= 250.0 + 1e-6


@pytest.mark.parametrize("seed", SEEDS)
def test_monotonic_in_pack_limits_and_cvl(seed):
    rng = random.Random(seed)
    b = _random_bench(rng)
    b.run(3)
    base = copy.deepcopy(b)
    o0 = base.run(1)
    lbl = rng.choice(LABELS)
    for field in ("ccl", "dcl", "cvl"):
        alt = copy.deepcopy(b)
        alt.vals[lbl][field] = alt.vals[lbl][field] * rng.uniform(0.0, 0.99) if field != "cvl" else \
            alt.vals[lbl][field] - rng.uniform(0.01, 2.0)
        o1 = alt.run(1)
        assert o1.target_ccl <= o0.target_ccl + 1e-9
        assert o1.target_dcl <= o0.target_dcl + 1e-9
        assert o1.target_cvl <= o0.target_cvl + 1e-9
        assert o1.ccl <= o0.ccl + 1e-9 and o1.dcl <= o0.dcl + 1e-9 and o1.cvl <= o0.cvl + 1e-9


@pytest.mark.parametrize("seed", SEEDS)
def test_pack_loss_never_raises_limits(seed):
    rng = random.Random(seed)
    b = _random_bench(rng)
    b.run(3)
    stay = copy.deepcopy(b)
    lose = copy.deepcopy(b)
    lose.online[rng.choice(LABELS)] = False
    for k in range(600):   # SR-02 holds until GONE (gone_after_s, design 5.3 step 3)
        a, c = stay.run(1), lose.run(1)
        assert c.model_ccl <= a.model_ccl + 1e-6, k
        assert c.model_dcl <= a.model_dcl + 1e-6, k
        assert c.ccl <= a.ccl + 1e-6 and c.dcl <= a.dcl + 1e-6


@pytest.mark.parametrize("seed", SEEDS[:10])
def test_rate_limits(seed):
    rng = random.Random(seed)
    c = cfg()
    b = Bench(c)
    b.warm()
    prev = b.out
    last_low = {}
    for k in range(600):
        if rng.random() < 0.05:
            l = rng.choice(LABELS)
            b.set(l, ccl=rng.uniform(0, 200), dcl=rng.uniform(0, 200))
        o = b.run(1)
        for name in ("ccl", "dcl"):
            p, n = getattr(prev, name), getattr(o, name)
            tgt = getattr(o, "target_" + name)
            if n > p:
                assert n - p <= min(c.ramp_up_a_s, c.ramp_up_pct_s / 100 * max(p, c.ramp_pct_base_a)) + 1e-9
                assert k - last_low.get(name, -999) >= c.hold_after_drop_s
            else:
                assert n == pytest.approx(min(p, tgt)) or n <= p
            if tgt < p or (n == p and tgt - p < c.limit_deadband_a):
                last_low[name] = k
        prev = o


GARBAGE = [None, float("nan"), float("inf"), -float("inf"), "12", True, -1, 0, 1e9, -1e9, [], {}, 3450]


@pytest.mark.parametrize("seed", SEEDS)
def test_garbage_never_propagates(seed):
    rng = random.Random(seed)
    b = _random_bench(rng)
    fields = list(b.vals["P1"].keys()) + ["charge_fet", "discharge_fet", "soc_calibrated", "cell_min_id"]
    for k in range(60):
        for l in LABELS:
            if rng.random() < 0.5:
                b.vals[l][rng.choice(fields)] = rng.choice(GARBAGE)
        if rng.random() < 0.1:
            b.serial["P2"] = rng.choice([None, 12, "", SERIAL["P2"], float("nan")])
        o = b.run(1)
        _check_outputs(o, b.cfg)


def test_failsafe_monotone():
    for seed in SEEDS[:10]:
        rng = random.Random(seed)
        b = _random_bench(rng)
        b.run(rng.randint(1, 200))
        for l in LABELS:
            b.online[l] = False
        prev = b.out
        for k in range(200):
            o = b.run(1)
            assert o.dcl <= prev.dcl + 1e-9 and o.ccl <= prev.ccl + 1e-9
            if k > b.cfg.offline_after_s + 1:
                assert o.ccl == 0.0 and o.mode == BankMode.FAILSAFE
            prev = o


def test_determinism():
    def seq(seed):
        rng = random.Random(seed)
        b = _random_bench(rng)
        res = []
        for k in range(200):
            if rng.random() < 0.1:
                b.online[rng.choice(LABELS)] = rng.random() < 0.6
            b.bank_current(rng.uniform(-150, 150))
            o = b.run(1)
            res.append((o.ccl, o.dcl, o.cvl, o.soc, o.mode))
        return res
    assert seq(7) == seq(7)


def test_snapshot_without_serial_and_weird_objects():
    core = Core(cfg())
    o = core.step([PackSnapshot(service="x", serial=None, timestamp=0.0)], 0.0)
    assert o.mode == BankMode.INIT and any("no serial" in e[2] for e in o.events)
