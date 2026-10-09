"""GUI parity with dbus-serialbattery (display only, never feeds back into control).

The Venus GUI (gui-v2 incl. the dbus-serialbattery web build) decides which battery pages to
show by the *existence of D-Bus paths* (e.g. "Cell Voltages" needs /Voltages/Cell3, "General"
needs CVL/CCL/DCL, IO needs /Io/AllowToCharge). It also highlights the limiting row in red
when /Info/ChargeLimitation or /Info/DischargeLimitation contains one of the keywords
"Cell Voltage", "SoC", "Temp" or "MOSFET". This module derives those paths for the aggregate
from the raw pack values and the already computed BankOutputs, so the existing pages work for
the merge battery without copying any (AGPL) QML.

Pure functions, no I/O. The adapter calls `build()` after the core step inside a try/except:
an error here can never change or stop CCL/DCL/CVL publishing.
"""
from __future__ import annotations

import math
import re

# keywords the serialbattery "General" page searches for to colour a row red
KW_CELL, KW_TEMP, KW_SOC, KW_MOSFET = "Cell Voltage", "Temp", "SoC", "MOSFET"
GUI_KEYWORDS = (KW_CELL, KW_TEMP, KW_SOC, KW_MOSFET)
TEMP_SLOTS = 4                       # /System/Temperature1..4 in the serialbattery API
HIDDEN_STATES = ("offline", "gone", "untrusted", "excluded")
HISTORY_SUM = ("/History/ChargedEnergy", "/History/DischargedEnergy", "/History/TotalAhDrawn")
HISTORY_MAX = ("/History/MaximumCellVoltage", "/History/MaximumTemperature", "/History/MaximumVoltage")
HISTORY_MIN = ("/History/MinimumCellVoltage", "/History/MinimumTemperature", "/History/MinimumVoltage")


def _num(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def pack_order(cfg, packs) -> list[tuple[str, int]]:
    """(label, cells) in display order: configured packs first, then auto-added ones."""
    order = [(p.label, int(p.cells)) for p in cfg.packs]
    known = {lbl for lbl, _ in order}
    for st in sorted(packs, key=lambda s: s.label):
        if st.label not in known:
            order.append((st.label, int(cfg.cells)))
            known.add(st.label)
    return order


def visible_raw(raw: dict, packs) -> dict[str, dict]:
    """label -> raw path values of every pack whose data may be shown."""
    out = {}
    for st in packs:
        vals = raw.get(st.service) if st.service else None
        if vals and st.state not in HIDDEN_STATES:
            out[st.label] = vals
    return out


def cell_paths(order, by_label) -> tuple[dict, list[float], str]:
    """Pack-major cell layout: P1 C1..C16 = Cell1..16, P2 = Cell17..32, ..."""
    res, cells, layout, n = {}, [], [], 0
    for lbl, count in order:
        vals = by_label.get(lbl, {})
        layout.append(f"{lbl}:{n + 1}-{n + count}")
        for c in range(1, count + 1):
            n += 1
            v = _num(vals.get(f"/Voltages/Cell{c}"))
            b = vals.get(f"/Balances/Cell{c}")
            res[f"/Voltages/Cell{n}"] = v
            res[f"/Balances/Cell{n}"] = None if v is None or b is None else int(bool(b))
            if v is not None:
                cells.append(v)
    return res, cells, ",".join(layout)


def utilisation(st, charge: bool) -> float | None:
    """Measured pack current / pack limit in the given direction (display only)."""
    cur, lim = _num(st.current), _num(st.ccl if charge else st.dcl)
    if cur is None or lim is None:
        return None
    flow = cur if charge else -cur
    if flow <= 0:
        return 0.0
    return flow / lim if lim > 0 else math.inf


def _keyword(text: str) -> str | None:
    t = (text or "").lower()
    if "mosfet" in t:
        return KW_MOSFET
    if "cell" in t:
        return KW_CELL
    if "temp" in t or "cold" in t or "hot" in t:
        return KW_TEMP
    if "soc" in t:
        return KW_SOC
    return None


def _pack_cause(label: str, why: str, by_label: dict, charge: bool) -> str:
    """Cause for one pack: the aggregator's own taper reason, else the pack's own driver text."""
    kw = _keyword(why)
    if kw:
        return kw
    own = by_label.get(label, {}).get("/Info/ChargeLimitation" if charge else "/Info/DischargeLimitation")
    if isinstance(own, str) and own:
        kw = _keyword(own)
        if kw:
            return kw
        if own.startswith("Max Battery"):
            return "BMS limit"
        return own
    return "BMS limit"


def _first_label(reason: str, labels) -> str | None:
    best = None
    for lbl in labels:
        m = re.search(r"(?<![\w.])" + re.escape(lbl) + r"(?![\w])", reason)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), lbl)
    return best[1] if best else None


def _sum_text(head: str, labels, by_label: dict, charge: bool) -> str:
    """'Summe 3 Packs: P2 Cell Voltage 3.452V' - the most limiting pack and its own cause."""
    m = re.search(r"sum (\d+) packs", head)
    n = m.group(1) if m else "?"
    lims = {lbl: float(x) for lbl, x in re.findall(r"(\S+) ([\d.]+)A(?:,| \(|$)", head.split(": ", 1)[-1])
            if lbl in labels}
    if not lims:
        return f"Summe {n} Packs"
    lbl = min(lims, key=lims.get)
    vals = by_label.get(lbl, {})
    own = vals.get("/Info/ChargeLimitation" if charge else "/Info/DischargeLimitation")
    own = own if isinstance(own, str) and own else "BMS"
    cell = _num(vals.get("/System/MaxCellVoltage" if charge else "/System/MinCellVoltage"))
    if own.startswith("Max Battery"):
        return f"Summe {n} Packs (BMS-Max)"
    return f"Summe {n} Packs: {lbl} {own}" + (f" {cell:.3f}V" if cell is not None else "")


def limitation(reason: str, value: float, packs, by_label: dict, charge: bool) -> str:
    """Short, GUI-keyword-carrying text: which pack limits and why."""
    r = reason or ""
    labels = [p.label for p in packs]
    head = r.split(" -> closed loop")[0]
    low = head.lower()
    if r.startswith("overload:"):
        m = re.match(r"overload: (\S+) (\S+)/(\S+)A -> bank (\S+)A", r)
        return (f"Ueberlast {m.group(1)} {m.group(2)}/{m.group(3)} A -> Bank {m.group(4)} A"
                if m else f"Ueberlast {value:.0f}A")
    elif low.startswith(("sum ", "hardware cap (sum ")):
        text = _sum_text(head, labels, by_label, charge)
        if low.startswith("hardware cap"):
            text = "HW-Cap; " + text
    elif r.startswith("restart hold"):
        text = "Restart hold"
    elif low.startswith(("init:", "failsafe:", "fault")):
        text = "FAILSAFE " + ("no charging" if charge else "emergency DCL")
    elif "blind" in low:
        lbl = _first_label(head, labels)
        text = f"{lbl} blind" + (f" {KW_TEMP}" if "temperature" in low else "")
    elif low.startswith("n-1"):
        text = f"N-1 reserve {_first_label(head, labels) or ''}".rstrip()
    elif low.startswith("sr-21"):
        text = "Commissioning cap"
    elif low.startswith("hardware cap"):
        text = "Inverter/HW cap"
    elif low.startswith("sum of pack limits"):
        text = "Sum of pack limits"
    elif low.startswith("degraded"):
        text = "Degraded (pack missing)"
    else:
        lbl = _first_label(head, labels)
        m = re.search(r"\(([^)]*)\)", head)
        text = f"{lbl} {_pack_cause(lbl, m.group(1) if m else '', by_label, charge)}" if lbl else head
    if " -> closed loop" in r:
        util = [(u, p) for p in packs if (u := utilisation(p, charge)) is not None]
        if util:
            u, p = max(util, key=lambda x: x[0])
            text = f"{p.label} {_pack_cause(p.label, '', by_label, charge)} PI u{u:.2f}"
        else:
            text += " PI"
    return f"{text} {value:.0f}A".strip()


def _charge_mode(order, by_label, mode: str) -> str | None:
    modes = [(lbl, by_label[lbl].get("/Info/ChargeMode")) for lbl, _ in order if lbl in by_label]
    modes = [(lbl, m) for lbl, m in modes if isinstance(m, str) and m]
    if not modes:
        return None
    uniq = {m for _, m in modes}
    text = modes[0][1] if len(uniq) == 1 else " / ".join(f"{lbl} {m.split(',')[0]}" for lbl, m in modes)
    return text if mode == "normal" else f"[{mode}] {text}"


def _agg(by_label, path, fn):
    vals = [v for v in (_num(d.get(path)) for d in by_label.values()) if v is not None]
    return fn(vals) if vals and len(vals) == len(by_label) else None


def _fmt(x, nd=1, unit=""):
    return "--" if x is None else f"{x:.{nd}f}{unit}"


def debug_text(out, order, by_label, cfg, version: str) -> str:
    """Multi-line overview for the GUI 'Driver Debug Data' box: packs, all cells, config."""
    lines = [f"Merge battery {version} | mode {out.mode.value} | {out.nr_online} online, "
             f"{out.nr_offline} offline",
             f"CCL {out.ccl:.1f}A: {out.reason_charge}",
             f"DCL {out.dcl:.1f}A: {out.reason_discharge}",
             f"CVL {out.cvl:.2f}V: {out.reason_voltage}",
             "Pack SoC I CCL/DCL u_chg/u_dis state"]
    by_st = {p.label: p for p in out.packs}
    for lbl, count in order:
        st = by_st.get(lbl)
        if st is None:
            lines.append(f"{lbl} not seen")
            continue
        uc, ud = utilisation(st, True), utilisation(st, False)
        lines.append(f"{lbl} {_fmt(st.soc, 1, '%')} {_fmt(st.current, 1, 'A')} "
                     f"{_fmt(st.ccl, 0)}/{_fmt(st.dcl, 0)}A {_fmt(uc, 2)}/{_fmt(ud, 2)} {st.state}"
                     + (" " + ",".join(st.problems) if st.problems else ""))
        vals = by_label.get(lbl)
        if vals:
            cells = [_num(vals.get(f"/Voltages/Cell{c}")) for c in range(1, count + 1)]
            bal = [bool(vals.get(f"/Balances/Cell{c}")) for c in range(1, count + 1)]
            ok = [c for c in cells if c is not None]
            if ok:
                lines.append(f"{lbl} cells {min(ok):.3f}-{max(ok):.3f}V diff {1000 * (max(ok) - min(ok)):.0f}mV"
                             + (" balancing" if any(bal) else ""))
                for k in range(0, count, 8):
                    seg = " ".join("--" if c is None else f"{c:.3f}" + ("*" if b else "")
                                   for c, b in zip(cells[k:k + 8], bal[k:k + 8]))
                    lines.append(f" C{k + 1}-{min(k + 8, count)}: {seg}")
    lines.append(f"Config (read-only): {len(cfg.packs)} packs configured, expected "
                 f"{cfg.expected_pack_count}, inverter CCL cap {cfg.inverter_ccl_cap_a}A, N-1 {cfg.n1_mode}, "
                 f"control {cfg.limit_control}, shadow {cfg.shadow}, enforce settings "
                 f"{cfg.enforce_system_settings}")
    return "\n".join(lines)


def build(raw: dict, out, cfg, version: str = "", debug: bool = True) -> dict:
    """Extra serialbattery-compatible paths for the aggregate service. raw: service -> values.
    debug=False skips the (expensive) multi-line debug text; the adapter builds it every 5 s."""
    order = pack_order(cfg, out.packs)
    by_label = visible_raw(raw, out.packs)
    res, cells, layout = cell_paths(order, by_label)
    res["/System/NrOfCellsPerBattery"] = int(cfg.cells)
    res["/Voltages/Sum"] = _agg(by_label, "/Voltages/Sum", lambda v: round(sum(v) / len(v), 3))
    res["/Voltages/Diff"] = round(max(cells) - min(cells), 3) if cells else None
    res["/Balancing"] = int(any(bool(d.get("/Balancing")) for d in by_label.values()))
    res["/Io/AllowToBalance"] = int(any(bool(d.get("/Io/AllowToBalance")) for d in by_label.values()))
    res["/Custom/Cells/Layout"] = layout
    res["/Info/ChargeLimitation"] = limitation(out.reason_charge, out.ccl, out.packs, by_label, True)
    res["/Info/DischargeLimitation"] = limitation(out.reason_discharge, out.dcl, out.packs, by_label, False)
    res["/Info/ChargeMode"] = _charge_mode(order, by_label, out.mode.value)
    res["/Info/MaxChargeCellVoltage"] = _agg(by_label, "/Info/MaxChargeCellVoltage", min)
    if debug:
        res["/Info/ChargeModeDebug"] = debug_text(out, order, by_label, cfg, version)
    res["/System/MOSTemperature"] = _agg(by_label, "/System/MOSTemperature", max)
    temps = [(lbl, _num(by_label[lbl].get("/System/MaxCellTemperature"))) for lbl, _ in order if lbl in by_label]
    for i in range(TEMP_SLOTS):
        lbl, t = temps[i] if i < len(temps) else (None, None)
        res[f"/System/Temperature{i + 1}"] = t
        res[f"/System/Temperature{i + 1}Name"] = f"{lbl} max" if lbl else None
    avg = _agg(by_label, "/CurrentAvg", sum)
    res["/CurrentAvg"] = None if avg is None else round(avg, 2)
    res["/TimeToGo"] = (round(out.capacity_ah / -avg * 3600) if avg is not None and avg < -0.5
                        and out.capacity_ah > 0 else None)
    for p in HISTORY_SUM:
        res[p] = _agg(by_label, p, lambda v: round(sum(v), 2))
    for p in HISTORY_MAX:
        res[p] = _agg(by_label, p, max)
    for p in HISTORY_MIN:
        res[p] = _agg(by_label, p, min)
    res["/History/CanBeCleared"] = 0
    # never publish non-finite numbers (e.g. sum of 1e308 history values)
    return {p: (None if isinstance(v, float) and not math.isfinite(v) else v) for p, v in res.items()}
