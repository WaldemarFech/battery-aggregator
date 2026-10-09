"""Bank outputs (core -> adapter). Every published number is finite (SR-05)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .model import BankMode


@dataclass
class PackStatus:
    label: str
    serial: str
    service: str | None
    state: str
    role: str
    soc: float | None
    current: float | None
    share_chg: float | None
    share_dchg: float | None
    ccl: float | None
    dcl: float | None
    cvl: float | None
    soc_suspect: bool
    charge_path_open: bool
    discharge_path_open: bool
    disconnected: bool
    problems: tuple = ()


@dataclass
class BankOutputs:
    mode: BankMode
    ccl: float
    dcl: float
    cvl: float
    soc: float | None
    soc_raw: float | None
    soc_weighted: float | None
    soc_min: float | None
    soc_min_pack: str | None
    capacity_ah: float
    installed_capacity_ah: float
    consumed_ah: float
    voltage: float | None
    current: float | None
    power: float | None
    temperature: float | None
    cell_min: float | None = None
    cell_min_id: str | None = None
    cell_max: float | None = None
    cell_max_id: str | None = None
    temp_min: float | None = None
    temp_min_id: str | None = None
    temp_max: float | None = None
    temp_max_id: str | None = None
    allow_to_charge: bool = False
    allow_to_discharge: bool = False
    alarms: dict = field(default_factory=dict)
    nr_online: int = 0
    nr_offline: int = 0
    nr_blocking_charge: int = 0
    nr_blocking_discharge: int = 0
    reason_charge: str = ""
    reason_discharge: str = ""
    reason_voltage: str = ""
    current_incomplete: bool = False
    calibration_recommended: bool = False
    calibration_active: bool = False
    packs: list = field(default_factory=list)
    # diagnostics (pre-cap model values, used by tests/shadow log)
    model_ccl: float = 0.0
    model_dcl: float = 0.0
    term_ccl: float = float("inf")    # min_i (L_i - c_i)/s_i before sum/N-1/hw caps
    term_dcl: float = float("inf")
    target_ccl: float = 0.0
    target_dcl: float = 0.0
    target_cvl: float = 0.0
    k_fb_charge: float = 1.0
    k_fb_discharge: float = 1.0
    n1_ccl: float = float("inf")
    n1_dcl: float = float("inf")
    shares_charge: dict = field(default_factory=dict)
    shares_discharge: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    restart_requested: bool = False
    heartbeat: int = 0                # SR-10: incremented per loop by the adapter

    def published(self) -> dict:
        """The values that go to D-Bus (or the shadow log)."""
        return {
            "/Info/MaxChargeCurrent": round(self.ccl, 1),
            "/Info/MaxDischargeCurrent": round(self.dcl, 1),
            "/Info/MaxChargeVoltage": round(self.cvl, 2),
            "/Io/AllowToCharge": int(self.allow_to_charge),
            "/Io/AllowToDischarge": int(self.allow_to_discharge),
            "/Soc": None if self.soc is None else round(self.soc, 1),
            "/Capacity": round(self.capacity_ah, 1),
            "/InstalledCapacity": round(self.installed_capacity_ah, 1),
            "/ConsumedAmphours": round(self.consumed_ah, 1),
            "/Dc/0/Voltage": None if self.voltage is None else round(self.voltage, 2),
            "/Dc/0/Current": None if self.current is None else round(self.current, 1),
            "/Dc/0/Power": None if self.power is None else round(self.power, 0),
            "/Dc/0/Temperature": None if self.temperature is None else round(self.temperature, 1),
            "/System/MinCellVoltage": self.cell_min,
            "/System/MinVoltageCellId": self.cell_min_id,
            "/System/MaxCellVoltage": self.cell_max,
            "/System/MaxVoltageCellId": self.cell_max_id,
            "/System/MinCellTemperature": self.temp_min,
            "/System/MinTemperatureCellId": self.temp_min_id,
            "/System/MaxCellTemperature": self.temp_max,
            "/System/MaxTemperatureCellId": self.temp_max_id,
            "/System/NrOfModulesOnline": self.nr_online,
            "/System/NrOfModulesOffline": self.nr_offline,
            "/System/NrOfModulesBlockingCharge": self.nr_blocking_charge,
            "/System/NrOfModulesBlockingDischarge": self.nr_blocking_discharge,
            **{f"/Alarms/{k}": v for k, v in self.alarms.items()},
            "/Custom/Mode": self.mode.value,
            "/Custom/Heartbeat": self.heartbeat,
            "/Custom/SocRaw": self.soc_raw,
            "/Custom/LimitReason/Charge": self.reason_charge,
            "/Custom/LimitReason/Discharge": self.reason_discharge,
            "/Custom/LimitReason/Voltage": self.reason_voltage,
        }

    def check(self) -> list[str]:
        bad = []
        for name in ("ccl", "dcl", "cvl"):
            v = getattr(self, name)
            if not isinstance(v, float) or not math.isfinite(v) or v < 0:
                bad.append(name)
        return bad
