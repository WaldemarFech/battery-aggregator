"""Replay recorder CSV through the core on a PC (design 8.4) and compare with what Venus did.

    python -m battery_aggregator.tools.replay --config config.json packs-20261010.csv.gz \
        [--system system-20261010.csv.gz] --out replay.csv
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import sys
from collections import OrderedDict

from ..adapter.service import build_snapshot
from ..config import Config
from ..core import Core


TEXT_COLUMNS = {"/Serial", "/CustomName", "/ProductName", "/Mgmt/ProcessName"}


def _open(path: str):
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", newline="")
    return open(path, encoding="utf-8", newline="")


def _val(s: str):
    if s == "":
        return None
    try:
        f = float(s)
        return int(f) if f.is_integer() and "." not in s and "e" not in s.lower() else f
    except ValueError:
        return s


def read_frames(path: str):
    """Yield (t_mono_second, t_wall, [snapshots]) grouped per second."""
    frames: OrderedDict = OrderedDict()
    with _open(path) as fh:
        for row in csv.DictReader(fh):
            tm = float(row.pop("t_mono"))
            tw = float(row.pop("t_wall"))
            svc = row.pop("service")
            vals = {k: (v or None) if k in TEXT_COLUMNS else _val(v) for k, v in row.items()}
            key = int(tm)
            frames.setdefault(key, (tw, []))[1].append(build_snapshot(svc, vals, tm))
    for key, (tw, snaps) in frames.items():
        yield float(key), tw, snaps


def read_system(path: str | None) -> dict:
    if not path:
        return {}
    res = {}
    with _open(path) as fh:
        for row in csv.DictReader(fh):
            res[int(float(row["t_mono"]))] = row
    return res


def replay(cfg: Config, packs_csv: str, system_csv: str | None = None):
    core = Core(cfg)
    system = read_system(system_csv)
    rows = []
    for t, tw, snaps in read_frames(packs_csv):
        o = core.step(snaps, t, tw)
        s = system.get(int(t), {})
        rows.append({"t_mono": t, "t_wall": tw, "mode": o.mode.value, "ccl": round(o.ccl, 1),
                     "dcl": round(o.dcl, 1), "cvl": round(o.cvl, 2),
                     "model_ccl": round(o.model_ccl, 1), "model_dcl": round(o.model_dcl, 1),
                     "soc": None if o.soc is None else round(o.soc, 1),
                     "reason_dcl": o.reason_discharge, "reason_ccl": o.reason_charge,
                     "venus_ccl": s.get("/Info/MaxChargeCurrent", ""),
                     "venus_dcl": s.get("/Info/MaxDischargeCurrent", ""),
                     "venus_cvl": s.get("/Info/MaxChargeVoltage", ""),
                     "bank_current": s.get("/Dc/Battery/Current", "")})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="battery-aggregator-replay")
    ap.add_argument("packs_csv")
    ap.add_argument("--system", default=None)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default="-")
    a = ap.parse_args(argv)
    rows = replay(Config.load_json(a.config), a.packs_csv, a.system)
    fh = sys.stdout if a.out == "-" else open(a.out, "w", encoding="utf-8", newline="")
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["t_mono"])
    w.writeheader()
    w.writerows(rows)
    if fh is not sys.stdout:
        fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
