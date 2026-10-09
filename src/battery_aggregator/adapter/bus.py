"""Bus interface used by the adapter, plus an in-memory FakeBus for tests.

The real implementation (velib_python VeDbusItemImport / VeDbusService on GLib) lives on
the device and is intentionally not part of this package yet (see docs/05_SHADOW_MODE.md).
"""
from __future__ import annotations

from typing import Protocol


class BusReader(Protocol):
    def list_services(self) -> list[str]: ...
    def read(self, service: str) -> dict | None: ...      # path -> value, None if gone


class BusPublisher(Protocol):
    def register(self, service: str, device_instance: int, product_name: str, product_id: int = 0xBA78) -> None: ...
    def publish_to(self, service: str, path: str, value) -> None: ...
    # optional: def publish_many(self, service: str, items: dict) -> None


class BusSettings(Protocol):
    def get_setting(self, path: str): ...
    def set_setting(self, path: str, value) -> None: ...


class FakeBus:
    """In-memory bus. Records every write so tests can prove read-only behaviour (SR-14)."""

    def __init__(self) -> None:
        self.services: dict[str, dict] = {}
        self.settings: dict[str, object] = {}
        self.registered: dict[str, dict] = {}
        self.published: dict[str, dict] = {}
        self.writes: list[tuple[str, str, object]] = []
        self.fail_reads: set[str] = set()

    # reader
    def list_services(self) -> list[str]:
        return list(self.services) + list(self.registered)

    def read(self, service: str) -> dict | None:
        if service in self.fail_reads:
            raise TimeoutError(f"dbus timeout on {service}")
        v = self.services.get(service)
        return dict(v) if v is not None else None

    # publisher
    def register(self, service: str, device_instance: int, product_name: str, product_id: int = 0xBA78) -> None:
        self.registered[service] = {"DeviceInstance": device_instance, "ProductName": product_name,
                                    "ProductId": product_id}
        self.published.setdefault(service, {})

    def publish_to(self, service: str, path: str, value) -> None:
        if service not in self.registered:
            raise RuntimeError(f"{service} not registered")
        self.writes.append((service, path, value))
        self.published[service][path] = value

    def publish_many(self, service: str, items: dict) -> None:
        for path, value in items.items():
            self.publish_to(service, path, value)

    # settings
    def get_setting(self, path: str):
        return self.settings.get(path)

    def set_setting(self, path: str, value) -> None:
        self.writes.append(("com.victronenergy.settings", path, value))
        self.settings[path] = value
