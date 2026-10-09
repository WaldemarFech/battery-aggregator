"""Recorder/replay roundtrip, velib cache logic, commissioning inputs, scripts (dry-run)."""
import json
import os
import shutil
import subprocess
import sys

import pytest
from bench import CAP, SERIAL, cfg
from test_scenarios_e import _bus_values

from battery_aggregator import Config
from battery_aggregator.adapter import FakeBus
from battery_aggregator.adapter.velib_bus import ServiceCache
from battery_aggregator.main import check_config
from battery_aggregator.tools.recorder import PACK_COLUMNS, Recorder
from battery_aggregator.tools.replay import replay

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_recorder_replay_roundtrip(tmp_path):
    bus = FakeBus()
    for i, l in enumerate(("P1", "P2", "P3")):
        v = _bus_values(SERIAL[l])
        v.update({"/InstalledCapacity": CAP[l], "/Info/MaxDischargeCurrent": {"P1": 43.6, "P2": 92.0, "P3": 59.0}[l],
                  "/Soc": {"P1": 8, "P2": 1, "P3": 13}[l]})
        bus.services[f"com.victronenergy.battery.ttyUSB{i}"] = v
    bus.services["com.victronenergy.system"] = {"/Info/MaxDischargeCurrent": 59.0, "/Dc/Battery/Current": -20.0}
    t = [0.0]
    rec = Recorder(bus, str(tmp_path), clock=lambda: t[0], wall=lambda: 1.8e9 + t[0])
    for k in range(40):
        t[0] = float(k)
        for svc, v in bus.services.items():
            if "/Dc/0/Voltage" in v:
                v["/Dc/0/Voltage"] = 48.5 + 0.001 * (k % 2)
        assert rec.sample() == 3
    rec.close()
    files = sorted(os.listdir(tmp_path))
    assert any(f.startswith("packs-") for f in files) and any(f.startswith("system-") for f in files)
    assert "/Serial" in PACK_COLUMNS
    packs = str(tmp_path / [f for f in files if f.startswith("packs-")][0])
    system = str(tmp_path / [f for f in files if f.startswith("system-")][0])
    rows = replay(cfg(), packs, system)
    assert len(rows) == 40
    assert rows[-1]["mode"] == "normal"
    assert rows[-1]["venus_dcl"] == "59.0"
    assert rows[-1]["soc"] == pytest.approx(7.3, abs=0.05)


def test_service_cache_signal_logic():
    t = [0.0]
    c = ServiceCache(clock=lambda: t[0])
    c.add("com.victronenergy.battery.ttyUSB0", ":1.42")
    c.full_update("com.victronenergy.battery.ttyUSB0", {"Dc/0/Voltage": 48.5, "Soc": 50})
    assert c.read("com.victronenergy.battery.ttyUSB0")["/Dc/0/Voltage"] == 48.5
    t[0] = 3.0
    c.items_changed(":1.42", {"/Dc/0/Voltage": {"Value": 48.6, "Text": "48.6V"}})
    assert c.read("com.victronenergy.battery.ttyUSB0")["/Dc/0/Voltage"] == 48.6
    assert c.last_update("com.victronenergy.battery.ttyUSB0") == 3.0
    c.items_changed(":1.99", {"/Dc/0/Voltage": {"Value": 1}})           # unknown sender ignored
    c.remove("com.victronenergy.battery.ttyUSB0")
    assert c.read("com.victronenergy.battery.ttyUSB0") is None and c.list_services() == []


def test_commissioning_inputs_block_live_mode(tmp_path, capsys):
    c = Config(shadow=True)
    assert not c.validate()                                             # shadow ok without inputs
    assert any("ccl_hw_a" in m for m in c.commissioning_missing())
    live = Config(shadow=False)
    assert any("commissioning input missing" in e for e in live.validate())
    assert not cfg(shadow=False).validate()                             # bench config is complete
    ex = os.path.join(ROOT, "config", "config.example.json")
    assert check_config(ex) == 0
    out = capsys.readouterr().out
    assert "COMMISSIONING INPUT MISSING" in out
    p = tmp_path / "c.json"
    d = json.load(open(ex))
    d["shadow"] = False
    p.write_text(json.dumps(d))
    assert check_config(str(p)) == 2


def test_no_hw_cap_falls_back_to_bms_limits():
    from bench import Bench
    b = Bench(cfg(ccl_hw_a=None, dcl_hw_a=None))
    b.warm()
    o = b.run(2)
    assert o.model_ccl == pytest.approx(250 * 0.65 / 0.36, abs=0.5)     # N-1 binds, no hw cap
    assert o.reason_charge.startswith("N-1")


SH = shutil.which("sh")


@pytest.mark.skipif(SH is None, reason="no POSIX sh")
@pytest.mark.parametrize("script", ["install.sh", "activate.sh", "rollback.sh", "boot.sh", "lib.sh"])
def test_scripts_syntax(script):
    r = subprocess.run([SH, "-n", os.path.join(ROOT, "scripts", script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(SH is None, reason="no POSIX sh")
def test_install_and_rollback_dry_run(tmp_path):
    root = tmp_path / "dev"
    (root / "data").mkdir(parents=True)
    env = dict(os.environ, ROOT=str(root).replace("\\", "/"), PY=sys.executable.replace("\\", "/"))
    r = subprocess.run([SH, os.path.join(ROOT, "scripts", "install.sh").replace("\\", "/"), "--dry-run"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELFTEST PASSED" in r.stdout and "DRY-RUN: ln -sfn" in r.stdout
    assert not (root / "data" / "battery-aggregator").exists()          # dry-run touched nothing
    r = subprocess.run([SH, os.path.join(ROOT, "scripts", "rollback.sh").replace("\\", "/"), "--disable",
                        "--dry-run"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr


FAKE_DBUS = """#!/bin/sh
# fake Venus dbus-cli: dbus -y SERVICE PATH GetValue|SetValue [VALUE]; strings print quoted
echo "$*" >> "$FAKE_DBUS_DIR/log"
key="$(echo "$3" | tr '/' '_')"
case "$4" in
    GetValue) cat "$FAKE_DBUS_DIR/$key" 2>/dev/null ;;
    SetValue) case "$5" in *[!0-9-]*) printf "'%s'\n" "$5" ;; *) printf '%s\n' "$5" ;; esac \
                  > "$FAKE_DBUS_DIR/$key" ;;
esac
"""


def _rollback_env(tmp_path, backup: str):
    root = tmp_path / "dev"
    base = root / "data" / "battery-aggregator"
    (base / "backup").mkdir(parents=True)
    (base / "config.json").write_text('{"shadow": false}')
    (base / "backup" / "settings-20261004-1200.txt").write_text(backup)
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "dbus").write_text(FAKE_DBUS, newline="\n")
    os.chmod(fake / "dbus", 0o755)
    state = tmp_path / "dbusstate"
    state.mkdir()
    env = dict(os.environ, ROOT=str(root).replace("\\", "/"), PY=sys.executable.replace("\\", "/"),
               FAKE_DBUS_DIR=str(state).replace("\\", "/"))
    env["PATH"] = str(fake) + os.pathsep + env["PATH"]
    return env, state, base


@pytest.mark.skipif(SH is None, reason="no POSIX sh")
def test_rollback_restores_settings_without_quotes(tmp_path):
    """Live finding: rollback wrote BatteryService back as the literal "'com.victronenergy.battery/3'"
    (the quotes dbus-cli prints around strings). It must write the bare value and verify it."""
    env, state, base = _rollback_env(tmp_path, "BmsInstance=3\nBatteryService='com.victronenergy.battery/3'\n")
    r = subprocess.run([SH, os.path.join(ROOT, "scripts", "rollback.sh").replace("\\", "/"), "--to-shadow"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    log = (state / "log").read_text()
    assert "BatteryService SetValue com.victronenergy.battery/3\n" in log
    assert "'" not in "".join(l for l in log.splitlines() if "SetValue" in l)
    assert "BmsInstance SetValue 3\n" in log
    assert "BatteryService restored to com.victronenergy.battery/3 (read back)" in r.stdout
    assert "BmsInstance restored to 3 (read back)" in r.stdout
    assert json.loads((base / "config.json").read_text())["shadow"] is True


@pytest.mark.skipif(SH is None, reason="no POSIX sh")
def test_rollback_never_writes_unclean_backup_values(tmp_path):
    env, state, _ = _rollback_env(tmp_path, "BmsInstance=\nBatteryService='a b; rm -rf /'\n")
    r = subprocess.run([SH, os.path.join(ROOT, "scripts", "rollback.sh").replace("\\", "/"), "--to-shadow"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    log = (state / "log").read_text() if (state / "log").exists() else ""
    assert "SetValue" not in log
    assert "not restored, set it manually" in r.stdout
