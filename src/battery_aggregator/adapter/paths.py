"""D-Bus path contract towards dbus-serialbattery (design 4.1, test 8.5).

Paths marked VERIFY must be confirmed against a path dump of serialbattery 2.0 RC on the
Cerbo before the adapter leaves shadow mode.
"""
from __future__ import annotations

# snapshot field -> D-Bus path
PACK_PATHS = {
    "voltage": "/Dc/0/Voltage",
    "current": "/Dc/0/Current",
    "temperature": "/Dc/0/Temperature",
    "temp_min": "/System/MinCellTemperature",
    "temp_max": "/System/MaxCellTemperature",
    "temp_min_id": "/System/MinTemperatureCellId",
    "temp_max_id": "/System/MaxTemperatureCellId",
    "soc": "/Soc",
    "installed_capacity": "/InstalledCapacity",
    "cvl": "/Info/MaxChargeVoltage",
    "ccl": "/Info/MaxChargeCurrent",
    "dcl": "/Info/MaxDischargeCurrent",
    "allow_charge": "/Io/AllowToCharge",
    "allow_discharge": "/Io/AllowToDischarge",
    "cell_min": "/System/MinCellVoltage",
    "cell_max": "/System/MaxCellVoltage",
    "cell_min_id": "/System/MinVoltageCellId",
    "cell_max_id": "/System/MaxVoltageCellId",
    "cells": "/System/NrOfCellsPerBattery",
    "serial": "/Serial",                       # VERIFY: serialbattery may expose the BMS serial elsewhere
    "charge_fet": "/Io/ChargeFet",              # VERIFY: name/presence in serialbattery 2.0 RC
    "discharge_fet": "/Io/DischargeFet",        # VERIFY
    "soc_calibrated": "/Info/SocResetLastReached",  # VERIFY: semantics (timestamp vs flag)
    "connected": "/Connected",                  # M6: 0 = driver lost the BMS -> pack offline at once
}

# Required for a pack to be trusted; a missing one makes the pack UNTRUSTED (S34).
REQUIRED = ("voltage", "current", "cvl", "ccl", "dcl", "cell_min", "cell_max", "serial")

OPTIONAL = tuple(k for k in PACK_PATHS if k not in REQUIRED)

ALARM_PREFIX = "/Alarms/"

OWN_SERVICE = "com.victronenergy.battery.aggregate"
SHADOW_SERVICE = "com.victronenergy.battaggregator_shadow"   # not a battery service class
BATTERY_PREFIX = "com.victronenergy.battery."
PACK_PROCESS_NAMES = ("dbus-serialbattery.py", "dbus-serialbattery", "dbushelper.py")  # 2.0 rc reports dbushelper.py
SETTINGS_SERVICE = "com.victronenergy.settings"
SETTING_BATTERY_SERVICE = "/Settings/SystemSetup/BatteryService"   # VERIFY in v3.70
SETTING_BMS_INSTANCE = "/Settings/SystemSetup/BmsInstance"         # VERIFY in v3.70
