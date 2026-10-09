"""1 Hz read-only recorder (design 8.4): all pack paths + what Venus actually applied.

    python3 -m battery_aggregator.tools.recorder --out /data/battery-aggregator/rec --hours 336

Separate process, independent of the aggregator; it only calls GetValue (never SetValue).
Writes gzip CSV per day: packs-YYYYMMDD.csv.gz (one row per pack and second) and
system-YYYYMMDD.csv.gz (systemcalc/DVCC view). Stops when free space < --min-free-mb.
Nothing is ever deleted by this tool.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import os
import sys
import time

from ..adapter import paths as P
from ..model import ALARM_PATHS

PACK_COLUMNS = sorted(set(P.PACK_PATHS.values()) | {P.ALARM_PREFIX + a for a in ALARM_PATHS} |
                      {"/Mgmt/ProcessName", "/Dc/0/Power", "/ConsumedAmphours", "/Capacity",
                       "/System/NrOfModulesBlockingCharge", "/System/NrOfModulesBlockingDischarge",
                       "/DeviceInstance", "/CustomName", "/ProductName"})

# VERIFY: systemcalc paths in Venus v3.70 (DVCC effective values)
SYSTEM_SERVICE = "com.victronenergy.system"
SYSTEM_COLUMNS = ["/Dc/Battery/Voltage", "/Dc/Battery/Current", "/Dc/Battery/Power", "/Dc/Battery/Soc",
                  "/ActiveBatteryService", "/ActiveBmsService", "/Control/BmsParameters",
                  "/Info/MaxChargeCurrent", "/Info/MaxDischargeCurrent", "/Info/MaxChargeVoltage",
                  "/Info/BatteryLowVoltage", "/Dc/Pv/Power", "/Ac/Consumption/L1/Power"]


class SyncDbusReader:
    """Blocking GetValue reader - acceptable here, this is not the control loop."""

    def __init__(self) -> None:
        import dbus
        self.dbus = dbus
        self.bus = dbus.SessionBus() if "DBUS_SESSION_BUS_ADDRESS" in os.environ else dbus.SystemBus()

    def list_services(self) -> list[str]:
        return [str(n) for n in self.bus.list_names() if str(n).startswith("com.victronenergy.")]

    def read(self, service: str) -> dict | None:
        from ..adapter.velib_bus import BUSITEM, unwrap
        try:
            tree = self.bus.get_object(service, "/", introspect=False).GetValue(
                dbus_interface=BUSITEM, timeout=1.0)
        except Exception:  # noqa: BLE001
            return None
        tree = unwrap(tree)
        return {("/" + k.lstrip("/")): v for k, v in tree.items()} if isinstance(tree, dict) else None


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (list, dict)):
        return ""
    return str(v)


class Recorder:
    def __init__(self, reader, out_dir: str, clock=time.monotonic, wall=time.time,
                 min_free_mb: float = 200.0) -> None:
        self.reader = reader
        self.out_dir = out_dir
        self.clock = clock
        self.wall = wall
        self.min_free_mb = min_free_mb
        self.day: str | None = None
        self.files: dict = {}
        os.makedirs(out_dir, exist_ok=True)

    def _open(self, kind: str, columns: list[str]):
        day = time.strftime("%Y%m%d", time.gmtime(self.wall()))
        key = (kind, day)
        if key not in self.files:
            for k in [k for k in self.files if k[0] == kind]:
                self.files.pop(k)[0].close()
            path = os.path.join(self.out_dir, f"{kind}-{day}.csv.gz")
            new = not os.path.exists(path)
            fh = io.TextIOWrapper(gzip.open(path, "ab"), encoding="utf-8", newline="")
            w = csv.writer(fh)
            if new:
                w.writerow(["t_wall", "t_mono", "service"] + columns)
            self.files[key] = (fh, w)
        return self.files[key][1]

    def free_mb(self) -> float:
        if not hasattr(os, "statvfs"):
            return float("inf")
        st = os.statvfs(self.out_dir)
        return st.f_bavail * st.f_frsize / 1e6

    def sample(self) -> int:
        tw, tm = self.wall(), self.clock()
        rows = 0
        w = self._open("packs", PACK_COLUMNS)
        for svc in sorted(self.reader.list_services()):
            if not svc.startswith(P.BATTERY_PREFIX):
                continue
            vals = self.reader.read(svc)
            if vals is None:
                continue
            w.writerow([f"{tw:.3f}", f"{tm:.3f}", svc] + [_cell(vals.get(c)) for c in PACK_COLUMNS])
            rows += 1
        sysvals = self.reader.read(SYSTEM_SERVICE)
        if sysvals is not None:
            self._open("system", SYSTEM_COLUMNS).writerow(
                [f"{tw:.3f}", f"{tm:.3f}", SYSTEM_SERVICE] + [_cell(sysvals.get(c)) for c in SYSTEM_COLUMNS])
        return rows

    def flush(self) -> None:
        for fh, _ in self.files.values():
            fh.flush()

    def close(self) -> None:
        for fh, _ in self.files.values():
            fh.close()
        self.files.clear()

    def run(self, seconds: float, period: float = 1.0, flush_every: int = 60) -> None:
        end = self.clock() + seconds
        n = 0
        next_t = self.clock()
        try:
            while self.clock() < end:
                if n % flush_every == 0 and self.free_mb() < self.min_free_mb:
                    print(f"recorder: free space below {self.min_free_mb} MB, stopping", file=sys.stderr)
                    break
                self.sample()
                n += 1
                if n % flush_every == 0:
                    self.flush()
                next_t += period
                time.sleep(max(0.0, next_t - self.clock()))
        finally:
            self.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="battery-aggregator-recorder")
    ap.add_argument("--out", default="/data/battery-aggregator/rec")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--min-free-mb", type=float, default=200.0)
    a = ap.parse_args(argv)
    Recorder(SyncDbusReader(), a.out, min_free_mb=a.min_free_mb).run(a.hours * 3600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
