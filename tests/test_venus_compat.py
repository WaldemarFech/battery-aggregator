"""Venus OS 3.70 / serialbattery 2.0 rc compatibility (found on the owner's Cerbo, 2026-10-04)."""
from battery_aggregator.adapter import paths as P
from battery_aggregator.config import Config


def _matches(proc: str) -> bool:
    return any(str(proc).endswith(n) for n in P.PACK_PROCESS_NAMES)


def test_serialbattery_2_rc_dbushelper_is_a_pack():
    assert _matches("/data/apps/dbus-serialbattery/dbushelper.py")
    assert _matches("/data/etc/dbus-serialbattery/dbus-serialbattery.py")
    assert not _matches("/opt/victronenergy/dbus-systemcalc-py/dbus_systemcalc.py")


def test_product_name_and_id_configurable_and_not_serialbattery_id():
    cfg = Config.from_dict({"product_name": "Merge-Batterie (3 Packs)"})
    assert cfg.product_name == "Merge-Batterie (3 Packs)"
    assert cfg.product_id != 0xBA77
