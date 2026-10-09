"""Restart hold (design 5.3a): no charge stop / CVL step across a service restart.

After a restart all packs sit in QUARANTINE for quarantine_s, so the bank is in INIT and would
publish CCL 0 / CVL failsafe until they are admitted. If the state file holds the outputs the
previous instance published, and they are fresh (wall-clock age <= restart_hold_max_age_s),
those values are published instead for at most restart_hold_s:

- every step they are clamped by the live model (pack CCL/DCL incl. cell, temperature and alarm
  derating, SR-21 caps, live CVL incl. the cell controller) and only ever ratchet DOWN;
- a seen pack that turns blind (lost, untrusted, implausible, /Connected=0), a pack alarm >= 2 or
  a blind-charge block ends the hold at once and for good (INIT values from then on);
- the window ends after restart_hold_s whatever happens, or when the packs are admitted (the
  core then seeds its ramps from the live target, so there is no step at the hand-over).

A missing BMS service would make systemcalc stop DVCC control altogether ("BMS lost"), so not
publishing during admission is worse than holding (DECISIONS D7).
"""
from __future__ import annotations

import math

from .model import CHARGE, DISCHARGE, BankMode

KEYS = ("ccl", "dcl", "cvl")


def parse_record(rec, cfg) -> dict | None:
    """Validated persisted outputs or None (foreign/corrupt data is ignored)."""
    if not isinstance(rec, dict):
        return None
    out = {}
    for k in KEYS + ("wall",):
        v = rec.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            return None
        out[k] = float(v)
    if not cfg.cells * cfg.cvl_min_v_per_cell <= out["cvl"] <= cfg.cvl_max_v():
        return None
    return out


def hold_cancel_reason(tracks, roles, blind_block) -> str | None:
    """Reasons that end the hold at once: the live picture is worse than the held one."""
    if blind_block:
        return blind_block
    for t in tracks:
        if t.snap is not None and roles[t.label] == "blind":
            return f"{t.label} {t.state.value} (no trusted live data)"
        v = t.valid
        if v is not None and any(lvl >= 2 for _, lvl in v.alarms):
            return f"{t.label} alarm " + ",".join(p for p, lvl in v.alarms if lvl >= 2)
    return None


class RestartHold:
    def __init__(self, cfg, record: dict | None = None) -> None:
        self.cfg = cfg
        self.rec = parse_record(record, cfg)
        self.t0: float | None = None
        self.active = False
        self.vals: dict[str, float] = {}

    def begin(self, now: float, wall: float | None, events: list) -> None:
        """First step after start: arm only with fresh persisted outputs."""
        self.t0 = now
        cfg = self.cfg
        if self.rec is None or cfg.restart_hold_s <= 0:
            return
        age = None if wall is None else wall - self.rec["wall"]
        if age is None or not 0.0 <= age <= cfg.restart_hold_max_age_s:
            events.append(("info", "bank", f"restart hold skipped: persisted outputs "
                                           f"{'age unknown' if age is None else f'{age:.0f}s old'}"))
            return
        self.active = True
        self.vals = {k: self.rec[k] for k in KEYS}
        events.append(("info", "bank", f"restart hold: CVL {self.vals['cvl']:.2f}V CCL "
                                       f"{self.vals['ccl']:.1f}A DCL {self.vals['dcl']:.1f}A "
                                       f"(persisted {age:.0f}s ago, max {cfg.restart_hold_s:.0f}s)"))

    def stop(self, why: str, events: list) -> None:
        if self.active:
            self.active = False
            events.append(("info", "bank", f"restart hold ended: {why}"))

    def apply(self, now: float, mode, live: dict, live_cvl: float, cancel: str | None,
              events: list) -> dict[str, float] | None:
        """Held values clamped by the live model (`live` = {CHARGE: A, DISCHARGE: A} targets
        with every guard applied, `live_cvl`), or None when not holding."""
        if not self.active:
            return None
        if mode != BankMode.INIT:
            self.stop("packs admitted" if mode in (BankMode.NORMAL, BankMode.DEGRADED)
                      else mode.value, events)
            return None
        if cancel:
            self.stop(cancel, events)
            return None
        if now - self.t0 >= self.cfg.restart_hold_s:
            self.stop(f"window {self.cfg.restart_hold_s:.0f}s over, packs not admitted", events)
            return None
        for k, v in (("ccl", live.get(CHARGE)), ("dcl", live.get(DISCHARGE)), ("cvl", live_cvl)):
            if v is None or not math.isfinite(v):
                v = 0.0
            self.vals[k] = min(self.vals[k], max(0.0, v))   # ratchet: only ever down
        return dict(self.vals)
