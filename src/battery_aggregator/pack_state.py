"""Per-pack lifecycle state machine (design 5.2/5.3).

QUARANTINE -> ACTIVE -> STALE -> OFFLINE(HOLD) -> GONE, plus UNTRUSTED and EXCLUDED.
Identity is the BMS serial; the service name is only the current transport.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import PackSnapshot, PackState, ValidPack, _flag, serial_ident, signature, validate


@dataclass
class PackTrack:
    serial: str
    label: str
    capacity_ah: float | None
    cells: int
    ocp_charge_a: float | None = None
    ocp_discharge_a: float | None = None
    state: PackState = PackState.QUARANTINE
    state_since: float = 0.0
    service: str | None = None
    snap: PackSnapshot | None = None
    valid: ValidPack | None = None
    last_valid: ValidPack | None = None
    last_valid_t: float | None = None
    problems: tuple = ()
    sig: tuple | None = None
    sig_change_t: float = 0.0
    unhealthy_since: float | None = None
    offline_since: float | None = None
    fresh_since: float | None = None
    quarantine_s: float = 30.0
    failures: list = field(default_factory=list)
    admitted: bool = False          # was ACTIVE at least once -> member of reference set
    excluded: bool = False
    # charge/discharge path evidence (design 4.6) and electrical disconnect (S30)
    chg_open: bool = False
    dchg_open: bool = False
    disconnected: bool = False
    chg_ev_since: float | None = None
    dchg_ev_since: float | None = None
    disc_ev_since: float | None = None
    hv_latched: bool = False
    plausible: bool = True          # voltage consistent with bus median (quarantine check)
    # SoC plausibility / calibration
    rest_since: float | None = None
    soc_suspect: bool = False
    t_last_full: float | None = None   # wall clock (persisted)
    # current-measurement plausibility (S12)
    zero_i_since: float | None = None
    warn_current: bool = False

    def capacity(self) -> float:
        if self.capacity_ah:
            return self.capacity_ah
        if self.last_valid and self.last_valid.capacity_ah:
            return self.last_valid.capacity_ah
        return 0.0


def _goto(t: PackTrack, state: PackState, now: float, events: list, why: str) -> None:
    if t.state != state:
        events.append(("state", t.label, f"{t.state.value}->{state.value}: {why}"))
        t.state = state
        t.state_since = now
    if state == PackState.ACTIVE:
        t.admitted = True


def _fail(t: PackTrack, now: float, cfg) -> None:
    t.failures = [f for f in t.failures if now - f <= cfg.flap_window_s]
    t.failures.append(now)


def _quarantine_len(t: PackTrack, now: float, cfg) -> float:
    recent = [f for f in t.failures if now - f <= cfg.flap_window_s]
    n = len(recent)
    return min(cfg.quarantine_s * (2 ** max(0, n - 1)), cfg.quarantine_max_s)


def _enter_quarantine(t: PackTrack, now: float, cfg, events: list, why: str) -> None:
    t.quarantine_s = _quarantine_len(t, now, cfg)
    if t.quarantine_s > cfg.quarantine_s:
        events.append(("warn", t.label, f"pack {t.label} unstable, quarantine {t.quarantine_s:.0f}s"))
    t.fresh_since = now
    t.unhealthy_since = None
    t.offline_since = None
    _goto(t, PackState.QUARANTINE, now, events, why)


def ingest(t: PackTrack, snap: PackSnapshot | None, now: float, cfg, conflict: str | None = None) -> None:
    """Take a new snapshot (None = service currently absent; keep the last one).

    conflict: identity conflict (M1, two different packs report this serial) -> the data
    cannot be attributed to one pack and is treated as invalid (UNTRUSTED, conservative).
    """
    if snap is None:
        return
    first = t.snap is None
    t.snap = snap
    t.service = snap.service
    valid, problems = validate(snap, t.cells, cfg)
    if conflict:
        valid = None
        problems = [conflict] + problems
    t.valid = valid
    t.problems = tuple(problems)
    if valid is not None:
        t.last_valid = valid
        t.last_valid_t = now
    sig = signature(snap)
    if first or sig != t.sig:
        t.sig = sig
        t.sig_change_t = now


def disconnected_flag(t: PackTrack) -> bool:
    """Driver explicitly reports /Connected = 0 (M6)."""
    return t.snap is not None and _flag(t.snap.connected) is False


def data_age(t: PackTrack, now: float, cfg) -> float:
    """Age of the last read; a timestamp from the future is treated as infinitely old (M6)."""
    if t.snap is None:
        return float("inf")
    ts = t.snap.timestamp
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts != ts:
        return float("inf")
    age = now - ts
    return float("inf") if age < -cfg.future_timestamp_tol_s else max(0.0, age)


def healthy(t: PackTrack, now: float, cfg) -> bool:
    """Fresh = /Connected not 0 AND data age <= stale_after_s AND values not frozen (M6)."""
    if t.snap is None or disconnected_flag(t):
        return False
    return data_age(t, now, cfg) <= cfg.stale_after_s and (now - t.sig_change_t) <= cfg.frozen_after_s


def update_state(t: PackTrack, now: float, cfg, bus_median: float | None, events: list) -> None:
    if t.excluded:
        _goto(t, PackState.EXCLUDED, now, events, "excluded by owner")
        return
    if t.state == PackState.EXCLUDED:
        _enter_quarantine(t, now, cfg, events, "exclusion lifted")
    if t.snap is None:
        return
    ok = healthy(t, now, cfg)
    s = t.state

    if disconnected_flag(t) and s not in (PackState.OFFLINE, PackState.GONE):
        # M6: the driver itself says the BMS is gone -> no STALE grace, OFFLINE at once
        if s in (PackState.ACTIVE, PackState.STALE):
            _fail(t, now, cfg)
        t.offline_since = now
        t.unhealthy_since = t.unhealthy_since if t.unhealthy_since is not None else now
        _goto(t, PackState.OFFLINE, now, events, "/Connected = 0")
        return

    if t.valid is None:  # safety field invalid / schema problem
        if s in (PackState.ACTIVE, PackState.STALE):
            _fail(t, now, cfg)
        if s != PackState.UNTRUSTED:
            _goto(t, PackState.UNTRUSTED, now, events, "; ".join(t.problems[:3]) or "invalid")
        return

    if s == PackState.UNTRUSTED:
        if ok:
            _enter_quarantine(t, now, cfg, events, "valid data again")
        else:
            t.offline_since = now
            _goto(t, PackState.OFFLINE, now, events, "valid but not fresh")
        return

    if s == PackState.ACTIVE:
        if not ok:
            _fail(t, now, cfg)
            t.unhealthy_since = now
            _goto(t, PackState.STALE, now, events, "no fresh data")
        return

    if s == PackState.STALE:
        if ok:
            recent = [f for f in t.failures if now - f <= cfg.flap_window_s]
            t.unhealthy_since = None
            if len(recent) >= 2:
                _enter_quarantine(t, now, cfg, events, "returned (flapping)")
            else:
                _goto(t, PackState.ACTIVE, now, events, "fresh again")
            return
        comm_age = data_age(t, now, cfg)
        held = now - (t.unhealthy_since if t.unhealthy_since is not None else now)
        if comm_age > cfg.offline_after_s or held >= cfg.offline_after_s - cfg.stale_after_s:
            t.offline_since = now
            _goto(t, PackState.OFFLINE, now, events, "stale too long")
        return

    if s == PackState.OFFLINE:
        if ok:
            _enter_quarantine(t, now, cfg, events, "returned")
        elif t.offline_since is not None and now - t.offline_since >= cfg.gone_after_s:
            _goto(t, PackState.GONE, now, events, "offline beyond hold time")
        return

    if s == PackState.GONE:
        if ok:
            _enter_quarantine(t, now, cfg, events, "returned after gone")
        return

    if s == PackState.QUARANTINE:
        if not ok:
            if t.unhealthy_since is None:
                t.unhealthy_since = now
                _fail(t, now, cfg)
                t.quarantine_s = _quarantine_len(t, now, cfg)
            t.fresh_since = None
            if now - t.unhealthy_since >= cfg.offline_after_s - cfg.stale_after_s:
                t.offline_since = now
                _goto(t, PackState.OFFLINE, now, events, "lost during quarantine")
            return
        t.unhealthy_since = None
        if t.fresh_since is None:
            t.fresh_since = now
        if bus_median is not None and abs(t.valid.voltage - bus_median) > cfg.disconnect_dv:
            t.plausible = False
            t.fresh_since = now  # implausible vs bus -> restart admission timer (S26)
            return
        t.plausible = True
        if now - t.fresh_since >= t.quarantine_s:
            _goto(t, PackState.ACTIVE, now, events, f"admitted after {t.quarantine_s:.0f}s")


def role(t: PackTrack, now: float, cfg) -> str:
    """How the limit engine treats the pack.

    active    -> member with live values
    uncertain -> member in world A with held/unconfirmed values, absent in world B
    blind     -> electrically present but unknown: only renormalises the others
    none      -> not part of the bank (GONE/EXCLUDED)
    """
    s = t.state
    if s == PackState.ACTIVE:
        return "active"
    if s == PackState.STALE:
        # M6: a stale pack is treated like an offline one (held values may only tighten,
        # degraded sum factor, blind-charge temperature policy) until it is fresh again.
        return "blind"
    if s == PackState.QUARANTINE:
        if t.valid is not None and t.plausible and healthy(t, now, cfg):
            return "uncertain"
        return "blind"
    if s in (PackState.OFFLINE, PackState.UNTRUSTED):
        return "blind"
    return "none"


def data_for(t: PackTrack) -> ValidPack | None:
    """Values used for a pack: live if valid, else last valid (hold)."""
    return t.valid if t.valid is not None else t.last_valid


def route_snapshots(self, snapshots, now: float, events: list) -> dict:
    """Map snapshots to pack identities (M1). `self` is the Core (tracks, cfg, svc_serial, ...).

    - Identity is the BMS serial; placeholders ("", "0", "unknown", ...) are no identity.
    - A service that already delivered a real serial keeps that binding while its serial is
      briefly missing (no flapping through STALE/QUARANTINE) - unless another service
      delivers that serial in the same step.
    - A service without any known serial is ignored (never merged into another pack).
    - Two services with the same serial but different live values are two packs: the
      serial is marked as identity conflict and its data is not trusted (UNTRUSTED).
    """
    self.conflicts = {}
    cand: dict[str, list[PackSnapshot]] = {}
    unbound: list[PackSnapshot] = []
    for s in snapshots:
        service = getattr(s, "service", "?")
        serial = serial_ident(getattr(s, "serial", None))
        if serial is None:
            unbound.append(s)
            continue
        cand.setdefault(serial, []).append(s)
    for s in unbound:
        service = getattr(s, "service", "?")
        serial = self.svc_serial.get(service)
        if serial is not None and serial not in cand:
            cand[serial] = [s]
            key = f"serial-missing:{service}"
            if key not in self.warned:
                self.warned.add(key)
                events.append(("warn", "bank", f"service {service} reports no serial, "
                                               f"kept bound to {serial}"))
            continue
        key = f"noserial:{service}"
        if key not in self.warned:
            self.warned.add(key)
            events.append(("warn", "bank", f"service {service} has no serial, ignored"))
    by_serial: dict[str, PackSnapshot] = {}
    for serial, lst in cand.items():
        newest = max(lst, key=lambda x: x.timestamp if isinstance(x.timestamp, (int, float)) else -1e18)
        if len(lst) > 1:
            events.append(("warn", "bank", f"duplicate serial {serial}: "
                                           + " / ".join(sorted(str(x.service) for x in lst))))
            if len({signature(x) for x in lst}) > 1:
                self.conflicts[serial] = f"identity conflict: serial {serial} on {len(lst)} services"
        by_serial[serial] = newest
        if serial_ident(getattr(newest, "serial", None)) is not None:
            for svc, ser in list(self.svc_serial.items()):
                if ser == serial and svc != newest.service:
                    del self.svc_serial[svc]
            self.svc_serial[newest.service] = serial
    for serial, s in by_serial.items():
        if serial not in self.tracks:
            if self.cfg.packs and not self.cfg.auto_add_unknown:
                if serial not in self.warned:
                    self.warned.add(serial)
                    events.append(("warn", "bank", f"unknown pack serial {serial} on {s.service} ignored (whitelist)"))
                continue
            used = {t.label for t in self.tracks.values()}
            k = len(self.tracks) + 1
            while f"P{k}" in used:
                k += 1
            label = f"P{k}"
            self.tracks[serial] = PackTrack(serial, label, None, self.cfg.cells,
                                            quarantine_s=self.cfg.quarantine_s, state_since=now)
            events.append(("info", label, f"new pack serial {serial} on {s.service}"))
    return by_serial
