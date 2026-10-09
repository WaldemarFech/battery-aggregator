"""Closed-loop analysis of the bank-limit controller in the simulator (PC only, not used on Venus).

    python -m battery_aggregator.tools.control_sim            # table: PI controller vs legacy
    python -m battery_aggregator.tools.control_sim --plot docs/img/owner_scenario.png

Scenarios (charge direction unless noted; 3 x 280 Ah packs, 16s LFP, R 15 mOhm):
  owner      P1/P2 CCL 50 A, P3 nearly full (top knee, higher OCV) CCL 10 A -> P3 takes LESS than 1/3
  owner_step owner physics, all packs report 50 A while charging hard; after 60 s P3 says
             "nearly full, max 10 A" (the bank runs at ~140 A, P3 carries ~40 A at that moment)
  hungry     P3 has the lowest resistance -> takes MORE than its share (0.48) at CCL 10 A
  taper      P3 CCL steps 50 -> 40 -> 30 -> 20 -> 10 A every 60 s (owner physics)
  noisy      owner + 1 A current noise, 2 s measurement lag, 3 s inverter delay
  noisy_h    hungry + the same noise/delays
  discharge  owner mirrored: P3 nearly empty, DCL 10 A, P1/P2 DCL 50 A
"""
from __future__ import annotations

import argparse
from dataclasses import fields

from ..config import Config, PackConfig
from ..core import Core
from ..model import CHARGE, DISCHARGE
from ..sim import Plant, SimPack, run

LABELS = ("P1", "P2", "P3")


def make_cfg(mode: str, **kw) -> Config:
    base = dict(packs=tuple(PackConfig(f"JK-SIM-{i}", l, 280.0, 16, 250.0, 250.0)
                            for i, l in enumerate(LABELS, 1)),
                expected_pack_count=3, ccl_hw_a=300.0, dcl_hw_a=300.0, measured_shares_verified=True)
    if "limit_control" in {f.name for f in fields(Config)}:
        base["limit_control"] = mode
    base.update(kw)
    return Config(**base)


def build(name: str, mode: str = "pi", seed: int = 1):
    """Returns (core, plant, demand(t), hook(t, plant), direction, watched pack, limit(t))."""
    d = DISCHARGE if name == "discharge" else CHARGE
    noisy = name.startswith("noisy")
    hungry = name in ("hungry", "noisy_h")
    if d == CHARGE:
        socs = (85.0, 85.0, 85.0) if hungry else (85.0, 85.0, 90.0)
    else:
        socs = (30.0, 30.0, 30.0) if hungry else (30.0, 30.0, 22.0)
    r3 = 0.008 if hungry else 0.015
    lim3 = (lambda p, t: 10.0)
    if name == "owner_step":
        lim3 = (lambda p, t: 50.0 if t < 460 else 10.0)
    if name == "taper":
        lim3 = (lambda p, t: max(10.0, 50.0 - 10.0 * max(0, int((t - 400) // 60))))
    packs = [SimPack(l, f"JK-SIM-{i}", 280.0, r_ohm=(r3 if l == "P3" else 0.015), soc=s,
                     ccl=(lim3 if (l == "P3" and d == CHARGE) else 50.0),
                     dcl=(lim3 if (l == "P3" and d == DISCHARGE) else 50.0))
             for i, (l, s) in enumerate(zip(LABELS, socs), 1)]
    plant = Plant(packs, delay_s=4 if noisy else 2, meas_delay_s=2 if noisy else 0,
                  noise_a=1.0 if noisy else 0.0, seed=seed)
    core = Core(make_cfg(mode))
    sign = 1.0 if d == CHARGE else -1.0
    t_on = 400.0                                    # rest first (offsets, ramps up), then PV / load

    def demand(t: float) -> float:
        return 0.0 if t < t_on else sign * 200.0

    def limit(t: float) -> float:
        return lim3(None, t)
    return core, plant, demand, d, "P3", limit, t_on


def setup(name: str, mode: str = "pi", seed: int = 1, shares: dict | None = None):
    """Build and warm up (rest, 395 s). Returns (core, plant, demand, d, watch, limit, t_on);
    continue with sim.run(core, plant, n, demand, t0=t_on - 5)."""
    core, plant, demand, d, watch, limit, t_on = build(name, mode, seed)
    # default: stale estimate 1/3 each (what a share learner reports after a period of equal
    # sharing) - the open-loop model then says min x N
    for l in LABELS:
        core.est.set_estimate(l, d, (shares or {}).get(l, 1.0 / 3.0), 0.01)
    run(core, plant, int(t_on) - 5, demand)
    return core, plant, demand, d, watch, limit, t_on


def simulate(name: str, mode: str = "pi", seconds: int = 600, seed: int = 1, shares: dict | None = None):
    core, plant, demand, d, watch, limit, t_on = setup(name, mode, seed, shares)
    hist = run(core, plant, seconds, demand, t0=t_on - 5)
    return hist, d, watch, limit, t_on


def metrics(hist, d, watch, limit, t_on) -> dict:
    sign = 1.0 if d == CHARGE else -1.0
    over = over5 = 0
    u_max = 0.0
    excess_as = 0.0
    for t, o, c in hist:
        if t < t_on:
            continue
        i = sign * c[watch]
        lim = limit(t)
        u_max = max(u_max, i / lim)
        if i > lim:
            over += 1
            excess_as += i - lim
        if i > 1.05 * lim:
            over5 += 1
    tail = [sign * sum(c.values()) for t, _, c in hist[-60:]]
    first = [sign * sum(c.values()) for t, _, c in hist if t_on + 30 <= t < t_on + 90]
    return {"over_s": over, "over5_s": over5, "u_max": u_max, "excess_As": excess_as,
            "bank_60_120": sum(first) / max(1, len(first)), "bank_settled": sum(tail) / len(tail),
            "ripple": max(tail) - min(tail)}


SCENARIOS = ("owner", "owner_step", "hungry", "taper", "noisy", "noisy_h", "discharge")


def table(modes=("legacy", "pi"), seconds: int = 600) -> str:
    rows = [f"{'scenario':10} {'mode':7} {'P3>lim s':>8} {'>5% s':>6} {'u_max':>6} {'excess As':>9} "
            f"{'bank@30-90s':>11} {'bank settled':>12} {'ripple':>6}"]
    for name in SCENARIOS:
        for mode in modes:
            m = metrics(*simulate(name, mode, seconds))
            rows.append(f"{name:10} {mode:7} {m['over_s']:8d} {m['over5_s']:6d} {m['u_max']:6.2f} "
                        f"{m['excess_As']:9.1f} {m['bank_60_120']:11.1f} {m['bank_settled']:12.1f} "
                        f"{m['ripple']:6.1f}")
    return "\n".join(rows)


def plot(path: str, seconds: int = 330) -> None:   # pragma: no cover - needs matplotlib
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 1, figsize=(9, 6.2), sharex=True)
    colors = {"legacy": "#9aa0a6", "pi": "#1a73e8"}
    names = {"legacy": "open-loop share model + slow feedback k (before)", "pi": "closed-loop PI (this project)"}
    for mode in ("legacy", "pi"):
        hist, d, watch, limit, t_on = simulate("owner_step", mode, seconds)
        t_step = t_on + 60
        ts = [t - t_step for t, _, _ in hist]
        axes[0].plot(ts, [sum(c.values()) for _, _, c in hist], color=colors[mode], label=names[mode], lw=1.8)
        axes[1].plot(ts, [c[watch] for _, _, c in hist], color=colors[mode], label=names[mode], lw=1.8)
    ts_l = [t - t_step for t, _, _ in hist]
    axes[1].plot(ts_l, [limit(t) for t, _, _ in hist], color="#d93025", lw=1, ls="--", label="P3 limit (CCL)")
    axes[0].axhline(110, color="#d93025", ls=":", lw=1)
    axes[0].text(150, 113, "naive sum 50+50+10 = 110 A (P3 would carry ~30 A)", color="#d93025", fontsize=8)
    axes[0].axhline(30, color="#5f6368", ls=":", lw=1)
    axes[0].text(150, 33, "min x N = 30 A", color="#5f6368", fontsize=8)
    axes[0].set_ylabel("bank charge current (A)")
    axes[0].set_ylim(0, 150)
    axes[0].legend(loc="upper right", fontsize=8)
    axes[1].set_ylabel("P3 current (A)")
    axes[1].set_ylim(0, 45)
    axes[1].legend(loc="upper right", fontsize=8)
    axes[1].set_xlabel("seconds after P3 reports 'nearly full, max 10 A' (P1/P2 keep 50 A)")
    axes[1].set_xlim(-30, seconds - 70)
    for ax in axes:
        ax.grid(alpha=0.25)
    fig.suptitle("Owner scenario 50 A / 50 A / 10 A: cut at once, then raise to what P3 really takes")
    fig.tight_layout()
    fig.savefig(path, dpi=110)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=int, default=600)
    ap.add_argument("--plot", help="write the owner-scenario PNG to this path")
    ap.add_argument("--modes", default="legacy,pi")
    a = ap.parse_args(argv)
    if a.plot:
        plot(a.plot)
    print(table(tuple(a.modes.split(",")), a.seconds))


if __name__ == "__main__":
    main()
