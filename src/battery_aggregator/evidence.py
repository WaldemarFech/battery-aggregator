"""Charge/discharge path state, electrical disconnect and high-cell latch (design 4.6, S05/S12/S14/S30).

B1 (review): the path state comes ONLY from explicit BMS information - the exported FET
state, or AllowToCharge/Discharge when configured to carry FET semantics ([VERIFY]).
Current flow is never used to infer that a path is open: a pack reading ~0 A may have a
dead shunt, an equal voltage or a slow driver, and removing it from the limit calculation
would lift the bank limit above what that pack can take. Unknown = closed (conservative:
the pack keeps constraining the bank with its own limit and share). An explicit "open"
that is contradicted by measured current in that direction is ignored (treated closed).
"""
from __future__ import annotations

from .model import CHARGE, DISCHARGE


def update_hv_latch(t, cfg, events: list) -> None:
    v = t.valid
    if v is None:
        return
    if v.cell_max >= cfg.v_cell_hard or v.alarm("HighCellVoltage") >= 2:
        if not t.hv_latched:
            events.append(("warn", t.label, f"high cell {v.cell_max:.3f}V: charge blocked (latched)"))
        t.hv_latched = True
    elif t.hv_latched and v.alarm("HighCellVoltage") < 2 and v.cell_max < cfg.v_cell_release:
        t.hv_latched = False
        events.append(("info", t.label, "high-cell latch released"))


def _explicit_path_state(v, d: str, cfg) -> bool | None:
    """True = path closed (conducting), False = open, None = not reported."""
    fet = v.charge_fet if d == CHARGE else v.discharge_fet
    if fet is not None:
        return fet
    allow = v.allow_charge if d == CHARGE else v.allow_discharge
    if cfg.allow_flag_is_fet and allow is not None:
        return allow
    return None


def _path(t, d: str, v, i_bank: float, now: float, cfg, events: list) -> None:
    attr, since_attr = ("chg_open", "chg_ev_since") if d == CHARGE else ("dchg_open", "dchg_ev_since")
    was = getattr(t, attr)
    closed = _explicit_path_state(v, d, cfg)
    now_open = closed is False
    if now_open:
        sign = 1.0 if d == CHARGE else -1.0
        if sign * v.current >= cfg.fet_close_a:
            # BMS says open but the pack carries current in that direction: do not trust
            # the flag, keep the pack in the limit calculation (conservative).
            now_open = False
            if getattr(t, since_attr) is None:
                setattr(t, since_attr, now)
                events.append(("warn", t.label, f"{d} path reported open but pack carries "
                                                f"{v.current:+.1f}A: treated as closed"))
        else:
            setattr(t, since_attr, None)
    else:
        setattr(t, since_attr, None)
    setattr(t, attr, now_open)
    if now_open != was:
        events.append(("path", t.label, f"{d} path {'open' if now_open else 'closed'}"))


def update_paths(tracks: list, i_bank: float, medians: dict, now: float, cfg, events: list) -> None:
    for t in tracks:
        v = t.valid
        if v is None or t.state.value != "active":
            continue
        update_hv_latch(t, cfg, events)
        _path(t, CHARGE, v, i_bank, now, cfg, events)
        _path(t, DISCHARGE, v, i_bank, now, cfg, events)
        # electrical disconnect (S30): voltage off the bus and no current
        med = medians.get(t.label)
        off_bus = med is not None and abs(v.voltage - med) > cfg.disconnect_dv
        if off_bus and abs(v.current) < cfg.fet_evidence_pack_a:
            if t.disc_ev_since is None:
                t.disc_ev_since = now
            elif now - t.disc_ev_since >= cfg.fet_evidence_s and not t.disconnected:
                t.disconnected = True
                events.append(("warn", t.label, f"pack {t.label} electrically disconnected "
                                                f"(dV {v.voltage - med:+.2f}V, I~0)"))
        else:
            t.disc_ev_since = None
            if t.disconnected and (not off_bus or abs(v.current) >= cfg.fet_close_a):
                t.disconnected = False
                events.append(("info", t.label, "pack reconnected to bus"))
        # current measurement plausibility (S12): path closed, bank flowing, pack reads ~0 A
        flowing = abs(i_bank) >= cfg.fet_evidence_bank_a
        closed = not (t.chg_open if i_bank > 0 else t.dchg_open)
        if flowing and closed and abs(v.current) < 0.05 and not t.disconnected:
            if t.zero_i_since is None:
                t.zero_i_since = now
            elif now - t.zero_i_since >= cfg.fet_evidence_s and not t.warn_current:
                t.warn_current = True
                events.append(("warn", t.label, f"current measurement of {t.label} implausible (0 A while bank "
                                                f"{i_bank:+.0f}A)"))
        else:
            t.zero_i_since = None
            if abs(v.current) >= 0.05:
                t.warn_current = False
