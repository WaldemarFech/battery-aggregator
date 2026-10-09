"""Fault injection for GUI parity (X1-CEO Opus review of PR #1, taken over as regression):
whatever the display layer does, the control outputs must be identical to a run without it."""
import copy
import time

import pytest
import test_gui_parity as T
from battery_aggregator import Core, SafeCore, parity
from battery_aggregator.adapter import Adapter, FakeBus, LivePublisher, service as S
from battery_aggregator.adapter import paths as P
from battery_aggregator.config import Config

CTRL = ("/Info/MaxChargeCurrent", "/Info/MaxDischargeCurrent", "/Info/MaxChargeVoltage", "/Soc", "/Custom/Mode")

def run(mut_tick=None, ticks=60, parity_on=True, monkey=None, busfactory=FakeBus):
    bus = busfactory(); bus.services = copy.deepcopy(T.PACKS)
    cfg = Config.from_dict(T.LIVE_CFG); clock = [0.0]
    ad = Adapter(bus, SafeCore(Core(cfg)), cfg, publisher=LivePublisher(bus, cfg), clock=lambda: clock[0], wall=lambda: 1.8e9)
    if not parity_on:
        ad.gui_parity = lambda raw, out, now: {}
    trace = []
    for k in range(ticks):
        clock[0] = float(k)
        for v in bus.services.values():
            if "/Dc/0/Voltage" in v and isinstance(v["/Dc/0/Voltage"], float):
                v["/Dc/0/Voltage"] = round(v["/Dc/0/Voltage"] + (0.001 if k % 2 else -0.001), 3)
        if mut_tick: mut_tick(k, bus.services)
        try:
            ad.tick()
        except Exception as e:
            trace.append(("EXC", repr(e))); continue
        pub = bus.published[P.OWN_SERVICE]
        trace.append(tuple(pub.get(p) for p in CTRL))
    return trace, bus

def svc(lbl): return T._svc(lbl)

def nan_cells(k, s):
    for c in range(1, 17): s[svc("P2")][f"/Voltages/Cell{c}"] = float("nan")
    s[svc("P1")]["/Voltages/Cell3"] = "garbage"; s[svc("P1")]["/Balances/Cell3"] = "x"
    s[svc("P3")]["/History/ChargedEnergy"] = 1e308; s[svc("P1")]["/History/ChargedEnergy"] = 1e308
    s[svc("P3")]["/CurrentAvg"] = float("inf"); s[svc("P1")]["/Info/ChargeMode"] = 12
    s[svc("P1")]["/Info/ChargeLimitation"] = None

def offline(k, s):
    if k == 20: s.pop(svc("P3"))
def frozen(k, s):
    if k >= 20: s[svc("P3")]["/Dc/0/Voltage"] = 52.0  # stale -> freshness timeout
def none_everything(k, s):
    if k >= 10:
        for key in list(s[svc("P2")]):
            if key.startswith(("/Voltages", "/History", "/System", "/Info/ChargeMode", "/Balanc", "/Io")):
                s[svc("P2")][key] = None

@pytest.mark.parametrize("mut", [None, nan_cells, offline, frozen, none_everything])
def test_ctrl_identical(mut):
    a, _ = run(mut, parity_on=True); b, _ = run(mut, parity_on=False)
    assert a == b
    assert not any(x[0] == "EXC" for x in a)

def test_formatter_exception(monkeypatch):
    b, _ = run(parity_on=False)
    monkeypatch.setattr(parity, "debug_text", lambda *a, **k: 1/0)
    a, _ = run()
    assert a == b
    monkeypatch.setattr(parity, "limitation", lambda *a, **k: (_ for _ in ()).throw(RecursionError()))
    a2, _ = run(); assert a2 == b

class RaisingBus(FakeBus):
    """publish of ONE parity path raises (e.g. dbus marshalling error on the device)"""
    def publish_to(self, service, path, value):
        if path == "/Voltages/Cell5" and service == P.OWN_SERVICE:
            raise TypeError("cannot marshal")
        super().publish_to(service, path, value)

def test_publish_layer_exception_order():
    a, _ = run(busfactory=RaisingBus)
    print("first entries:", a[:3])
    assert not any(x[0] == "EXC" for x in a), "parity value publish error aborts the tick before CCL/DCL"

def test_build_cost():
    _, bus = run(ticks=45)
    cfg = Config.from_dict(T.LIVE_CFG)
    bus2, out, pub = T._run()
    raw = {k: v for k, v in bus2.services.items() if k != P.OWN_SERVICE}
    t = time.perf_counter()
    for _ in range(200): r = parity.build(raw, out, cfg, "0.3.0")
    dt = (time.perf_counter() - t) / 200
    assert dt < 0.05, f"build {dt*1e3:.2f} ms"


class _Beat:
    def __init__(self):
        self.n = 0

    def beat(self):
        self.n += 1


def test_display_setter_raises_control_still_published_and_watchdog_beats():
    bus = RaisingBus()
    bus.services = copy.deepcopy(T.PACKS)
    cfg = Config.from_dict(T.LIVE_CFG)
    clock = [0.0]
    wd = _Beat()
    ad = Adapter(bus, SafeCore(Core(cfg)), cfg, publisher=LivePublisher(bus, cfg), clock=lambda: clock[0],
                 wall=lambda: 1.8e9, watchdog=wd)
    for k in range(40):
        clock[0] = float(k)
        out = ad.tick()
        pub = bus.published[P.OWN_SERVICE]
        assert pub["/Info/MaxChargeCurrent"] == round(out.ccl, 1)
        assert pub["/Info/MaxDischargeCurrent"] == round(out.dcl, 1)
        assert pub["/Info/MaxChargeVoltage"] == round(out.cvl, 2)
    assert wd.n == 40
    assert "/Voltages/Cell5" not in pub and pub["/Voltages/Cell6"] is not None   # others still shown


def test_control_paths_are_published_before_display_paths():
    _, bus = run(ticks=3)
    paths = [p for svc, p, _ in bus.writes if svc == P.OWN_SERVICE]
    first_display = paths.index("/Voltages/Cell1")
    assert paths.index("/Info/MaxChargeCurrent") < first_display
    assert paths.index("/Info/MaxDischargeCurrent") < first_display
