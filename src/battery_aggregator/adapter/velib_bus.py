"""Real Venus bus implementation (dbus-python + GLib + velib_python).

Implements the BusReader / BusPublisher / BusSettings protocols of bus.py without any
blocking call in the 1 s loop (SR-10):
- discovery via NameOwnerChanged + one async ListNames at start,
- values via the ItemsChanged signal (and legacy per-item PropertiesChanged), cached per
  service with the monotonic time of the last update (freshness for the core),
- a slow async GetValue('/') refresh per service as fallback (default 5 s),
- settings via async GetValue every 60 s; SetValue only when explicitly called.

Everything that depends on Venus internals is marked VERIFY and must be checked on the
Cerbo (v3.70~61) before leaving shadow mode. Imports of dbus/gi/vedbus are lazy so that
this module can be imported (and unit-tested with fakes) on a PC.
"""
from __future__ import annotations

import logging
import os
import sys
import time

from . import paths as P

log = logging.getLogger("battery_aggregator.velib")

# VERIFY: location of velib_python on Venus v3.70 (shipped with several services).
VELIB_CANDIDATES = (
    "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python",
    "/opt/victronenergy/velib_python",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "ext", "velib_python"),
)

BUSITEM = "com.victronenergy.BusItem"


def unwrap(v):
    """dbus types -> plain python. Victron encodes 'invalid' as an empty array."""
    try:
        import dbus  # noqa: F401
    except ImportError:  # PC / tests
        return v
    import dbus
    if isinstance(v, dbus.Array):
        return None if len(v) == 0 else [unwrap(x) for x in v]
    if isinstance(v, dbus.Dictionary):
        return {str(k): unwrap(x) for k, x in v.items()}
    if isinstance(v, (dbus.Double,)):
        return float(v)
    if isinstance(v, (dbus.Boolean,)):
        return bool(v)
    if isinstance(v, (dbus.Int16, dbus.Int32, dbus.Int64, dbus.UInt16, dbus.UInt32, dbus.UInt64, dbus.Byte)):
        return int(v)
    if isinstance(v, (dbus.String, dbus.ObjectPath)):
        return str(v)
    return v


def import_velib(extra: str | None = None):
    for p in ((extra,) if extra else ()) + VELIB_CANDIDATES:
        if p and os.path.isdir(p) and p not in sys.path:
            sys.path.insert(1, p)
    from vedbus import VeDbusService  # noqa: WPS433  (VERIFY: API of the installed velib)
    return VeDbusService


class ServiceCache:
    """Values of all watched services, updated from signals (pure python, testable)."""

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self.values: dict[str, dict] = {}
        self.updated: dict[str, float] = {}
        self.owner_to_name: dict[str, str] = {}

    def add(self, name: str, owner: str | None) -> None:
        self.values.setdefault(name, {})
        if owner:
            self.owner_to_name[owner] = name

    def remove(self, name: str) -> None:
        self.values.pop(name, None)
        self.updated.pop(name, None)
        for o, n in list(self.owner_to_name.items()):
            if n == name:
                del self.owner_to_name[o]

    def full_update(self, name: str, tree: dict) -> None:
        """Reply of GetValue on '/': {'Dc/0/Voltage': 48.5, ...} (VERIFY: no leading slash)."""
        if name not in self.values:
            return
        self.values[name] = {("/" + k.lstrip("/")): unwrap(v) for k, v in tree.items()}
        self.updated[name] = self.clock()

    def items_changed(self, sender: str, items: dict) -> None:
        """ItemsChanged(a{sa{sv}}): {'/Dc/0/Voltage': {'Value': .., 'Text': ..}, ...}."""
        name = self.owner_to_name.get(sender, sender)
        if name not in self.values:
            return
        for path, props in items.items():
            if isinstance(props, dict) and "Value" in props:
                self.values[name][str(path)] = unwrap(props["Value"])
        self.updated[name] = self.clock()

    def property_changed(self, sender: str, path: str, props: dict) -> None:
        self.items_changed(sender, {path: props})

    # BusReader protocol
    def list_services(self) -> list[str]:
        return list(self.values)

    def read(self, service: str) -> dict | None:
        v = self.values.get(service)
        return dict(v) if v else None

    def last_update(self, service: str) -> float | None:
        return self.updated.get(service)


class VenusBus:
    """dbus-python implementation. Construct only on the device (needs a GLib main loop)."""

    def __init__(self, velib_path: str | None = None, refresh_s: float = 5.0,
                 settings_every_s: float = 60.0, clock=time.monotonic) -> None:
        import dbus
        from dbus.mainloop.glib import DBusGMainLoop
        from gi.repository import GLib
        DBusGMainLoop(set_as_default=True)
        self.dbus = dbus
        self.GLib = GLib
        # VERIFY: Venus services live on the system bus; DBUS_SESSION_BUS_ADDRESS for PC tests
        self.bus = dbus.SessionBus() if "DBUS_SESSION_BUS_ADDRESS" in os.environ else dbus.SystemBus()
        self.cache = ServiceCache(clock)
        self.VeDbusService = import_velib(velib_path)
        self.services: dict[str, object] = {}
        self.settings: dict[str, object] = {}
        self._watch()
        GLib.timeout_add(int(refresh_s * 1000), self._refresh_all)
        GLib.timeout_add(int(settings_every_s * 1000), self._refresh_settings)
        self._refresh_settings()

    # ------------------------------------------------------------------ discovery / signals
    def _wanted(self, name: str) -> bool:
        return name.startswith(P.BATTERY_PREFIX) and name not in (P.OWN_SERVICE,)

    def _watch(self) -> None:
        self.bus.add_signal_receiver(self._name_owner_changed, signal_name="NameOwnerChanged",
                                     dbus_interface="org.freedesktop.DBus")
        self.bus.add_signal_receiver(self._items_changed, signal_name="ItemsChanged",
                                     dbus_interface=BUSITEM, path="/", sender_keyword="sender")
        # legacy per-item signal (older services) - VERIFY whether serialbattery 2.0 still emits it
        self.bus.add_signal_receiver(self._prop_changed, signal_name="PropertiesChanged",
                                     dbus_interface=BUSITEM, sender_keyword="sender", path_keyword="path")
        self.bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus").ListNames(
            dbus_interface="org.freedesktop.DBus", reply_handler=self._names, error_handler=self._err)

    def _names(self, names) -> None:
        for n in names:
            n = str(n)
            if self._wanted(n):
                self._add(n)

    def _add(self, name: str) -> None:
        self.cache.add(name, None)
        self.bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus").GetNameOwner(
            name, dbus_interface="org.freedesktop.DBus",
            reply_handler=lambda owner, n=name: self.cache.add(n, str(owner)), error_handler=self._err)
        self._refresh(name)

    def _name_owner_changed(self, name, old, new) -> None:
        name = str(name)
        if not self._wanted(name):
            return
        if new:
            self.cache.remove(name)
            self.cache.add(name, str(new))
            self._refresh(name)
        else:
            log.info("service gone: %s", name)
            self.cache.remove(name)

    def _items_changed(self, items, sender=None) -> None:
        self.cache.items_changed(str(sender), {str(k): unwrap(v) for k, v in items.items()})

    def _prop_changed(self, props, sender=None, path=None) -> None:
        self.cache.property_changed(str(sender), str(path), unwrap(props))

    def _refresh(self, name: str) -> None:
        try:
            obj = self.bus.get_object(name, "/", introspect=False)
            obj.GetValue(dbus_interface=BUSITEM, timeout=2.0,
                         reply_handler=lambda tree, n=name: self.cache.full_update(n, unwrap(tree)),
                         error_handler=self._err)
        except Exception as exc:  # noqa: BLE001 - service may vanish any time
            log.warning("refresh %s failed: %s", name, exc)

    def _refresh_all(self) -> bool:
        for n in self.cache.list_services():
            self._refresh(n)
        return True

    def _refresh_settings(self) -> bool:
        for path in (P.SETTING_BMS_INSTANCE, P.SETTING_BATTERY_SERVICE):
            try:
                obj = self.bus.get_object(P.SETTINGS_SERVICE, path, introspect=False)
                obj.GetValue(dbus_interface=BUSITEM, timeout=2.0,
                             reply_handler=lambda v, p=path: self.settings.__setitem__(p, unwrap(v)),
                             error_handler=self._err)
            except Exception as exc:  # noqa: BLE001
                log.warning("settings read %s failed: %s", path, exc)
        return True

    @staticmethod
    def _err(e) -> None:
        log.debug("dbus async error: %s", e)

    # ------------------------------------------------------------------ BusReader
    def list_services(self) -> list[str]:
        return self.cache.list_services()

    def read(self, service: str) -> dict | None:
        return self.cache.read(service)

    def last_update(self, service: str) -> float | None:
        return self.cache.last_update(service)

    # ------------------------------------------------------------------ BusPublisher
    def register(self, service: str, device_instance: int, product_name: str, product_id: int = 0xBA78) -> None:
        from .. import __version__
        # VERIFY: newer velib wants register=False + explicit register() after adding paths
        try:
            s = self.VeDbusService(service, self.dbus.SystemBus(private=True), register=False)
            late = True
        except TypeError:
            s = self.VeDbusService(service, self.dbus.SystemBus(private=True))
            late = False
        s.add_path("/Mgmt/ProcessName", "battery-aggregator")
        s.add_path("/Mgmt/ProcessVersion", __version__)
        s.add_path("/Mgmt/Connection", "aggregate of serialbattery packs")
        s.add_path("/DeviceInstance", device_instance)
        s.add_path("/ProductId", product_id)      # configurable; 0xBA77 collides with dbus-serialbattery
        s.add_path("/ProductName", product_name)
        s.add_path("/FirmwareVersion", __version__)
        s.add_path("/HardwareVersion", None)
        s.add_path("/Connected", 1)
        s.add_path("/Custom/Heartbeat", 0)
        if late:
            s.register()
        self.services[service] = s

    def publish_to(self, service: str, path: str, value) -> None:
        s = self.services[service]
        if path not in s:                          # VERIFY: VeDbusService.__contains__
            s.add_path(path, value)
        else:
            s[path] = value

    def publish_many(self, service: str, items: dict) -> None:
        """Values of one tick. Existing paths keep the proven per-item update (PropertiesChanged,
        unchanged DVCC path); NEW paths are added inside a velib ServiceContext so that one
        ItemsChanged announces them. A plain add_path() after register() emits no signal, so
        dbus-flashmq (MQTT -> GUI v2 web, VRM) never learns paths whose value stays constant
        (found 2026-10-05: /Io/AllowToCharge, /Alarms/*, /System/NrOfModulesOffline missing on
        MQTT although published on D-Bus)."""
        s = self.services[service]
        new = {p: v for p, v in items.items() if p not in s}
        for path, value in items.items():
            if path not in new:
                s[path] = value
        if not new:
            return
        if hasattr(s, "__enter__"):
            try:
                with s as ctx:
                    for path, value in new.items():
                        ctx.add_path(path, value)
                return
            except Exception as exc:  # noqa: BLE001 - never lose a tick over the announcement
                log.warning("ServiceContext add_path failed (%s), plain add_path fallback", exc)
        for path, value in new.items():            # old velib without ServiceContext
            if path in s:
                s[path] = value
            else:
                s.add_path(path, value)

    # ------------------------------------------------------------------ BusSettings
    def get_setting(self, path: str):
        return self.settings.get(path)

    def set_setting(self, path: str, value) -> None:
        obj = self.bus.get_object(P.SETTINGS_SERVICE, path, introspect=False)
        obj.SetValue(value, dbus_interface=BUSITEM, timeout=2.0,
                     reply_handler=lambda *a: log.info("setting %s=%r written", path, value),
                     error_handler=lambda e: log.error("setting %s failed: %s", path, e))
