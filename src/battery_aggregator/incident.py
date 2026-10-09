"""Pack-loss incident recorder (owner 2026-10-08). Display/analysis only, never feeds control.

Keeps a rolling history of every pack (SoC, current, voltage, CCL/DCL, all cell voltages) and,
when a pack leaves ACTIVE for stale/offline/quarantine/untrusted, writes one JSON file with
the history of ALL packs into <dir>/ (atomic, rate limited, at most `keep` files). An external
relay (any external script watching that directory) picks the files up and can notify the operator so the
analyst can analyse: which cell, offset low (does it also hit first when charging full?),
recommendation (recharge / balance / DC cabling / calibration).
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import deque

BAD_STATES = ("stale", "offline", "quarantine", "untrusted")


def _num(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    x = float(x)
    return round(x, 4) if math.isfinite(x) else None


def cell_stats(vals: dict, cells: int) -> dict:
    v = [(_num(vals.get(f"/Voltages/Cell{c}")), c) for c in range(1, cells + 1)]
    ok = [(x, c) for x, c in v if x is not None]
    if not ok:
        return {"cells": [x for x, _ in v]}
    lo, hi = min(ok), max(ok)
    return {"min_v": lo[0], "min_cell": lo[1], "max_v": hi[0], "max_cell": hi[1],
            "delta_mv": round(1000 * (hi[0] - lo[0]), 1), "cells": [x for x, _ in v]}


class IncidentRecorder:
    def __init__(self, directory: str, cells: int = 16, sample_every_s: float = 5.0,
                 history_s: float = 600.0, min_gap_s: float = 600.0, keep: int = 50,
                 wall=time.time) -> None:
        self.dir = directory
        self.cells = cells
        self.every = sample_every_s
        self.hist: deque = deque(maxlen=max(2, int(history_s / sample_every_s)))
        self.min_gap_s = min_gap_s
        self.keep = keep
        self.wall = wall
        self.prev_state: dict[str, str] = {}
        self.last_sample: float | None = None
        self.last_write: dict[str, float] = {}
        self.written: list[str] = []

    def _sample(self, raw: dict, out, now: float) -> dict:
        packs = {}
        for st in out.packs:
            vals = raw.get(st.service) if st.service else None
            entry = {"state": st.state, "soc": _num(st.soc), "current": _num(st.current),
                     "ccl": _num(st.ccl), "dcl": _num(st.dcl)}
            if vals:
                entry["voltage"] = _num(vals.get("/Dc/0/Voltage"))
                entry.update(cell_stats(vals, self.cells))
            packs[st.label] = entry
        return {"t": round(self.wall(), 1), "mono": round(now, 1), "bank_ccl": _num(out.ccl),
                "bank_dcl": _num(out.dcl), "bank_current": _num(out.current), "mode": out.mode.value,
                "reason_charge": out.reason_charge, "reason_discharge": out.reason_discharge, "packs": packs}

    def observe(self, raw: dict, out, now: float) -> str | None:
        """Call once per tick. Returns the path of a written incident file, else None."""
        cur = self._sample(raw, out, now)
        if self.last_sample is None or now - self.last_sample >= self.every:
            self.hist.append(cur)
            self.last_sample = now
        path = None
        for st in out.packs:
            before = self.prev_state.get(st.label)
            self.prev_state[st.label] = st.state
            if before == "active" and st.state in BAD_STATES:
                last = self.last_write.get(st.label)
                if last is not None and now - last < self.min_gap_s:
                    continue
                self.last_write[st.label] = now
                path = self._write(st, before, cur) or path
        return path

    def _write(self, st, before: str, cur: dict) -> str | None:
        ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self.wall()))
        name = f"{ts}_{st.label}_{st.state}.json"
        data = {"kind": "pack_loss", "pack": st.label, "serial": st.serial, "service": st.service,
                "from": before, "to": st.state, "problems": list(st.problems), "now": cur,
                "history": list(self.hist),
                "questions": ["which cell triggered (min/max cell id, delta)?",
                              "offset low: does the same cell also hit first when charging full?",
                              "recommendation: recharge / balance / DC cabling / calibration?"]}
        try:
            os.makedirs(self.dir, exist_ok=True)
            p = os.path.join(self.dir, name)
            with open(p + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(data, fh, separators=(",", ":"))
            os.replace(p + ".tmp", p)
            files = sorted(f for f in os.listdir(self.dir) if f.endswith(".json"))
            for old in files[:-self.keep]:
                os.remove(os.path.join(self.dir, old))
            self.written.append(p)
            return p
        except OSError:
            return None
