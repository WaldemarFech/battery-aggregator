"""Thin D-Bus adapter skeleton: discovery, snapshot building, publishing, watchdog.

Interface only - runs against any object implementing bus.BusReader/BusPublisher/BusSettings
(FakeBus in tests). The on-device implementation wraps velib_python + GLib (not included yet).
The adapter never writes to pack services (SR-14); with cfg.shadow it does not register a
battery service at all and only logs what it would publish (docs/05_SHADOW_MODE.md).
"""
from __future__ import annotations

import json
import logging
import os
import time

from .. import parity
from ..fault import SafeCore
from ..model import ALARM_PATHS, PackSnapshot
from ..outputs import BankOutputs
from . import paths as P

log = logging.getLogger("battery_aggregator.adapter")


def build_snapshot(service: str, values: dict, now: float) -> PackSnapshot:
    kw = {}
    missing = []
    for field, path in P.PACK_PATHS.items():
        if path in values and values[path] is not None and values[path] != []:
            kw[field] = values[path]
        elif field in P.REQUIRED:
            missing.append(path)
    alarms = {name: values.get(P.ALARM_PREFIX + name) for name in ALARM_PATHS
              if values.get(P.ALARM_PREFIX + name) is not None}
    return PackSnapshot(service=service, timestamp=now, alarms=alarms, missing_paths=tuple(missing),
                        **{"serial": kw.pop("serial", None), **kw})


def is_pack_service(service: str, values: dict | None) -> bool:
    if not service.startswith(P.BATTERY_PREFIX) or service == P.OWN_SERVICE:
        return False
    if values is None:
        return False
    proc = values.get("/Mgmt/ProcessName")
    if proc is not None and not any(str(proc).endswith(n) for n in P.PACK_PROCESS_NAMES):
        return False  # e.g. a SmartShunt or another virtual battery (C12)
    return True


class ShadowPublisher:
    """Publishes nothing to DVCC. Logs changes of the would-be values, and a full line
    every `full_every_s`. Optionally exposes /Custom/Shadow/* under a non-battery name."""

    KEYS = ("/Info/MaxChargeCurrent", "/Info/MaxDischargeCurrent", "/Info/MaxChargeVoltage", "/Soc",
            "/Custom/Mode")

    def __init__(self, bus=None, full_every_s: float = 60.0, sink=None) -> None:
        self.bus = bus
        self.full_every_s = full_every_s
        self.last_key: tuple | None = None
        self.last_full: float | None = None
        self.sink = sink if sink is not None else (lambda line: log.info("%s", line))
        if bus is not None:
            bus.register(P.SHADOW_SERVICE, 0, "Battery Aggregator (shadow)")

    def publish(self, out: BankOutputs, now: float, extra: dict | None = None) -> None:
        pub = out.published()          # GUI parity paths (extra) are not logged in shadow mode
        key = tuple(pub.get(k) for k in self.KEYS)
        if key != self.last_key or self.last_full is None or now - self.last_full >= self.full_every_s:
            rec = {"t": round(now, 1), "would_publish": pub, "model_ccl": _f(out.model_ccl),
                   "model_dcl": _f(out.model_dcl), "events": [list(e) for e in out.events],
                   "packs": [p.__dict__ for p in out.packs]}
            self.sink(json.dumps(rec, default=str, sort_keys=True))
            self.last_key = key
            self.last_full = now
        if self.bus is not None:
            for k in self.KEYS:
                self.bus.publish_to(P.SHADOW_SERVICE, "/Custom/Shadow" + k, pub.get(k))


def _f(x: float):
    return None if x != x or x in (float("inf"), float("-inf")) else round(x, 2)


class LivePublisher:
    """Registers com.victronenergy.battery.aggregate. Refuses to exist in shadow mode."""

    def __init__(self, bus, cfg) -> None:
        if cfg.shadow:
            raise RuntimeError("LivePublisher refused: config.shadow is true")
        self.bus = bus
        self.display_err_t: float | None = None
        bus.register(P.OWN_SERVICE, cfg.device_instance, cfg.product_name, cfg.product_id)

    def publish(self, out: BankOutputs, now: float, extra: dict | None = None) -> None:
        # 1) control paths (CCL/DCL/CVL/SoC/...) first and alone - an error here propagates as before
        control = out.published()
        self._send(control)
        # 2) display-only parity paths afterwards, isolated: they can neither override a control
        #    value nor stop the tick (watchdog beat, restart hold) with an exception
        if extra:
            self.publish_display({p: v for p, v in extra.items() if p not in control}, now)

    def _send(self, items: dict) -> None:
        many = getattr(self.bus, "publish_many", None)
        if many is not None:
            many(P.OWN_SERVICE, items)   # new paths announced via ItemsChanged (MQTT/GUI/VRM)
        else:
            for path, value in items.items():
                self.bus.publish_to(P.OWN_SERVICE, path, value)

    def publish_display(self, items: dict, now: float) -> None:
        try:
            self._send(items)
            return
        except Exception as exc:  # noqa: BLE001
            first = exc
        bad = []
        for path, value in items.items():     # one bad path must not hide all others
            try:
                self.bus.publish_to(P.OWN_SERVICE, path, value)
            except Exception:  # noqa: BLE001
                bad.append(path)
        if self.display_err_t is None or now - self.display_err_t >= 60.0:
            self.display_err_t = now
            log.warning("gui parity publish failed (%s); bad paths: %s", first, bad[:5])


class Watchdog:
    """SR-10: if the main loop stops beating for timeout_s, exit hard (service vanishes)."""

    def __init__(self, timeout_s: float = 5.0, clock=time.monotonic, exit_fn=os._exit) -> None:
        self.timeout_s = timeout_s
        self.clock = clock
        self.exit_fn = exit_fn
        self.last = clock()

    def beat(self) -> None:
        self.last = self.clock()

    def start_thread(self, period_s: float = 1.0):
        """Separate thread: detects a blocked GLib main loop (the loop cannot watch itself)."""
        import threading

        def loop():
            while True:
                time.sleep(period_s)
                self.check()
        th = threading.Thread(target=loop, name="watchdog", daemon=True)
        th.start()
        return th

    def check(self) -> bool:
        if self.clock() - self.last > self.timeout_s:
            log.critical("watchdog: main loop stalled for %.1fs, exiting", self.clock() - self.last)
            self.exit_fn(1)
            return False
        return True


def selection(bus, cfg) -> str:
    """S37: 'ok' (monitor and controlling BMS = aggregate), 'partial' (only one) or 'none'."""
    bms = bus.get_setting(P.SETTING_BMS_INSTANCE)
    mon = bus.get_setting(P.SETTING_BATTERY_SERVICE)
    n = int(bms == cfg.device_instance) + int(mon == f"com.victronenergy.battery/{cfg.device_instance}")
    return ("none", "partial", "ok")[n]


def check_system_settings(bus, cfg) -> tuple[bool, str]:
    """S37: Battery monitor and controlling BMS must point at the aggregate."""
    bms = bus.get_setting(P.SETTING_BMS_INSTANCE)
    mon = bus.get_setting(P.SETTING_BATTERY_SERVICE)
    want_mon = f"com.victronenergy.battery/{cfg.device_instance}"
    ok = bms == cfg.device_instance and mon == want_mon
    msg = f"BmsInstance={bms!r} BatteryService={mon!r} (want {cfg.device_instance}/{want_mon})"
    if not ok and cfg.enforce_system_settings and not cfg.shadow:
        bus.set_setting(P.SETTING_BMS_INSTANCE, cfg.device_instance)
        bus.set_setting(P.SETTING_BATTERY_SERVICE, want_mon)
        msg += " -> corrected (enforce_system_settings)"
    return ok, msg


class Adapter:
    def __init__(self, bus, safe_core: SafeCore, cfg, publisher=None, clock=time.monotonic,
                 wall=time.time, watchdog: Watchdog | None = None, exit_fn=os._exit,
                 incidents=None) -> None:
        self.incidents = incidents          # IncidentRecorder or None (analysis only)
        self.bus = bus
        self.core = safe_core
        self.cfg = cfg
        self.clock = clock
        self.wall = wall
        self.publisher = publisher or (ShadowPublisher() if cfg.shadow else LivePublisher(bus, cfg))
        self.watchdog = watchdog
        self.exit_fn = exit_fn
        self.settings_ok: bool | None = None
        self.selection = "ok"
        self.settings_t: float | None = None
        self.settings_every_s = 60.0
        self.heartbeat = 0
        self.parity_debug_every_s = 5.0   # the multi-line debug text changes every tick
        self.parity_debug_t: float | None = None
        self.parity_err_t: float | None = None

    def gui_parity(self, raw: dict, out: BankOutputs, now: float) -> dict:
        """Display-only serialbattery-compatible paths. Never raises (control output first)."""
        try:
            from .. import __version__
            debug = self.parity_debug_t is None or now - self.parity_debug_t >= self.parity_debug_every_s
            extra = parity.build(raw, out, self.cfg, __version__, debug=debug)
            if debug:
                self.parity_debug_t = now
            return extra
        except Exception as exc:  # noqa: BLE001 - a display bug must never stop DVCC publishing
            if self.parity_err_t is None or now - self.parity_err_t >= 60.0:
                self.parity_err_t = now
                log.warning("gui parity failed: %s", exc)
            return {}

    def tick(self) -> BankOutputs:
        now = self.clock()
        snaps = []
        raw: dict[str, dict] = {}
        for svc in self.bus.list_services():
            if not svc.startswith(P.BATTERY_PREFIX) or svc == P.OWN_SERVICE:
                continue
            try:  # isolation per pack: a broken service never stops the loop (R5)
                values = self.bus.read(svc)
                if not is_pack_service(svc, values):
                    continue
                raw[svc] = values
                last = getattr(self.bus, "last_update", None)
                ts = last(svc) if last else None      # signal-driven freshness on the device
                snaps.append(build_snapshot(svc, values, now if ts is None else ts))
            except Exception as exc:  # noqa: BLE001
                log.warning("reading %s failed: %s", svc, exc)
        out = self.core.step(snaps, now, self.wall())
        if not self.cfg.shadow:
            if self.settings_t is None or now - self.settings_t >= self.settings_every_s:
                self.settings_t = now
                ok, msg = check_system_settings(self.bus, self.cfg)
                self.selection = "ok" if ok else selection(self.bus, self.cfg)
                if ok != self.settings_ok:
                    if ok:
                        log.info("system settings ok")
                    else:
                        log.warning("system settings: %s", msg)
                    self.settings_ok = ok
            # S37: aggregate not selected at all (e.g. reset by a firmware update) -> warning level 1.
            # A half selection (only BMS or only monitor) is a deliberate owner choice and only logged:
            # an InternalFailure on the controlling BMS would be a false alarm (0.2.1).
            if self.settings_ok is False and self.selection == "none":
                out.alarms["InternalFailure"] = max(out.alarms.get("InternalFailure", 0), 1)
        self.heartbeat += 1
        out.heartbeat = self.heartbeat
        extra = self.gui_parity(raw, out, now) if not self.cfg.shadow else None
        self.publisher.publish(out, now, extra)
        if self.incidents is not None:
            try:  # analysis data only: can never change or stop publishing
                p = self.incidents.observe(raw, out, now)
                if p:
                    log.warning("pack incident recorded: %s", p)
            except Exception as exc:  # noqa: BLE001
                log.warning("incident recorder failed: %s", exc)
        if self.watchdog:
            self.watchdog.beat()
        if out.restart_requested:
            log.critical("core in FAULT too long, requesting restart")
            self.exit_fn(1)
        return out
