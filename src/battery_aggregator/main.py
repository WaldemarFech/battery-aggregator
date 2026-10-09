"""Service entry point on the Cerbo: python3 -m battery_aggregator.main --config ... --state ...

Runs one GLib main loop with a 1 s tick (adapter -> SafeCore -> publisher), a watchdog
thread (SR-10), state persistence every 60 s and on SIGTERM (last published outputs for the
restart hold, design 5.3a) and a heartbeat file for an external check.
`--check-config` validates the config and lists missing commissioning inputs (install.sh).
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

from . import __version__
from .config import Config
from .core import Core
from .fault import SafeCore
from .persistence import load_state, save_state

log = logging.getLogger("battery_aggregator")
SAVE_EVERY_S = 60.0     # state.json ~0.6 kB/min on /data; outputs older than restart_hold_max_age_s are not held


def check_config(path: str) -> int:
    try:
        cfg = Config.load_json(path)
    except (OSError, ValueError, TypeError) as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2
    errs = cfg.validate()
    for e in errs:
        print(f"CONFIG ERROR: {e}")
    for m in cfg.commissioning_missing():
        print(f"COMMISSIONING INPUT MISSING: {m}")
    print(f"shadow={cfg.shadow} measured_shares_verified={cfg.measured_shares_verified} "
          f"device_instance={cfg.device_instance}")
    return 2 if errs else 0


def write_heartbeat(path: str | None, n: int) -> None:
    if not path:
        return
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(f"{time.time():.0f} {n}\n")
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="battery-aggregator")
    ap.add_argument("--config", default="/data/battery-aggregator/config.json")
    ap.add_argument("--state", default="/data/battery-aggregator/state.json")
    ap.add_argument("--velib", default=None, help="path to velib_python (VERIFY default)")
    ap.add_argument("--heartbeat-file", default="/run/battery-aggregator.heartbeat")  # VERIFY /run tmpfs
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--check-config", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=getattr(logging, a.log_level.upper(), logging.INFO),
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    if a.check_config:
        return check_config(a.config)

    try:
        cfg = Config.load_json(a.config)
    except (OSError, ValueError, TypeError) as exc:
        log.critical("cannot load config %s: %s", a.config, exc)
        time.sleep(30)          # avoid a tight daemontools restart loop
        return 2
    errs = cfg.validate()
    if errs:
        log.critical("invalid config: %s", "; ".join(errs))
        time.sleep(30)
        return 2
    for m in cfg.commissioning_missing():
        log.warning("commissioning input missing: %s", m)
    log.info("battery-aggregator %s starting (shadow=%s, verified_shares=%s)", __version__, cfg.shadow,
             cfg.measured_shares_verified)

    from gi.repository import GLib

    from .adapter.service import Adapter, LivePublisher, ShadowPublisher, Watchdog
    from .adapter.velib_bus import VenusBus

    core = SafeCore(Core(cfg, load_state(a.state)))
    bus = VenusBus(a.velib)
    publisher = ShadowPublisher(bus=bus) if cfg.shadow else LivePublisher(bus, cfg)
    wd = Watchdog(timeout_s=5.0)
    wd.start_thread()
    from .incident import IncidentRecorder
    inc = IncidentRecorder(os.path.join(os.path.dirname(os.path.abspath(a.state)), "incidents"), cfg.cells)
    adapter = Adapter(bus, core, cfg, publisher=publisher, watchdog=wd, incidents=inc)
    last_save = [time.monotonic()]

    def tick() -> bool:
        try:
            out = adapter.tick()
            write_heartbeat(a.heartbeat_file, out.heartbeat)
            for ev in out.events:
                log.info("%s %s: %s", *ev)
            if time.monotonic() - last_save[0] >= SAVE_EVERY_S:
                last_save[0] = time.monotonic()
                save_state(a.state, core.core.export_state())
        except Exception:  # noqa: BLE001 - never let GLib drop the timer (prior-art bug #170)
            log.exception("tick failed")
        return True

    loop = GLib.MainLoop()

    def on_term() -> bool:
        # daemontools `svc -t`/`-d`: persist the last outputs so the next start can hold them
        try:
            save_state(a.state, core.core.export_state())
            log.info("SIGTERM: state saved, exiting")
        except Exception:  # noqa: BLE001
            log.exception("SIGTERM: saving state failed")
        loop.quit()
        return False

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, on_term)
    GLib.timeout_add(1000, tick)
    log.info("main loop running")
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "check_config"]
