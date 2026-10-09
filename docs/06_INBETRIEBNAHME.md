# 06 – Inbetriebnahme-Runbook (sobald SSH/Root am Cerbo freigegeben ist)

Stand 2026-10-04 · alles vorbereitet ohne Gerätezugriff. Jede `VERIFY`-Stelle muss vor `activate.sh` belegt sein.

## 0. Voraussetzung (Owner)
Settings → General → Access level „Superuser“, SSH an, Public Key hinterlegt. Auto-Firmware-Update aus (SR-16).

## 1. Bestandsaufnahme (nur lesen)
```
dbus -y | grep battery                                   # Dienste
for s in $(dbus -y | grep com.victronenergy.battery.tty); do dbus -y $s / GetValue > /data/pathdump-$s.txt; done
dbus -y com.victronenergy.settings /Settings/SystemSetup/BmsInstance GetValue
dbus -y com.victronenergy.settings /Settings/SystemSetup/BatteryService GetValue
dbus -y com.victronenergy.system / GetValue > /data/pathdump-system.txt
python3 --version; ls /opt/victronenergy/*/ext/velib_python 2>/dev/null | head; ls -l /service | head
```
Abgleich mit `src/battery_aggregator/adapter/paths.py`; JK-Parameter (OCP inkl. Verzögerung, OV/UV, LT) je Pack
ablesen → `config.json` (`packs[].ocp_*`, `ccl_hw_a`, `dcl_hw_a`, Seriennummern) – D4 in `DECISIONS.md`.

## 2. Installation im Schattenbetrieb
```
scp battery-aggregator-<ver>.tar.gz root@cerbo:/data/tmp/ && ssh root@cerbo
cd /data/tmp && tar xzf battery-aggregator-<ver>.tar.gz && cd battery-aggregator-<ver>
sh scripts/install.sh --dry-run && sh scripts/install.sh
tail -F /data/log/battery-aggregator/current | tai64nlocal
```
Paket am PC: `git archive --format=tar.gz --prefix=battery-aggregator-<ver>/ -o battery-aggregator-<ver>.tar.gz HEAD`.
`install.sh` macht: Python-/Compile-Check, `selftest` auf dem Ziel-Python, Release nach
`/data/battery-aggregator/releases/<ver>`, `current`-Symlink, `config.json` aus Beispiel (nie überschrieben, `shadow=true`),
Settings-Backup, daemontools-Dienst, Boot-Hook in `/data/rc.local` (nur anhängen).

## 3. Recorder (1 Hz, read-only, parallel)
```
cd /data/battery-aggregator/current && nohup python3 -m battery_aggregator.tools.recorder --hours 336 >/dev/null 2>&1 &
```
Am PC: `python -m battery_aggregator.tools.replay --config config.json packs-YYYYMMDD.csv.gz --system system-YYYYMMDD.csv.gz --out replay.csv`
→ Vergleich „was hätte der Aggregator veröffentlicht“ vs. „was Venus anwandte“.

## 4. Live schalten (Owner, nach Kriterien in `05_SHADOW_MODE.md`)
`sh /data/battery-aggregator/current/scripts/activate.sh --dry-run`, dann ohne `--dry-run`. Verweigert sich, solange
Inbetriebnahme-Eingaben fehlen. Danach Live-Abnahme S01/S02/S22/S31, FMEA T-F1…T-F11.

## 5. Rückweg
`rollback.sh --to-shadow` (Settings zurück, Schattenmodus) · `--to-version <ver>` · `--disable` (Dienst aus,
Boot-Hook raus, Settings zurück). Es wird nichts unter `/data/battery-aggregator` gelöscht.

## VERIFY-Liste (vor `activate.sh`)
| # | Punkt | Wo |
|---|---|---|
| V1 | velib_python-Pfad und `VeDbusService`-API (`register=False` + `register()`, `__contains__`, `add_path` nach Register) | `adapter/velib_bus.py` |
| V2 | `GetValue` auf `/` liefert Baum ohne führenden Slash; `ItemsChanged` auf `/` von serialbattery 2.0 RC | `velib_bus.py` |
| V3 | Pfade Seriennummer, FET-Status, `SocResetLastReached`, `NrOfCellsPerBattery` | `adapter/paths.py` |
| V4 | `AllowToCharge/Discharge` = FET oder nur Absicht (→ `allow_flag_is_fet`) | `config.py` |
| V5 | Settings-Pfade/-Format `BmsInstance`, `BatteryService` (`com.victronenergy.battery/512`) | `paths.py`, `activate.sh` |
| V6 | systemcalc-Pfade für den Recorder (`/Info/Max*`, `/ActiveBmsService`) | `tools/recorder.py` |
| V7 | `/service`-Mechanik + `/data/rc.local` in v3.70, `multilog`, `svc`, `dbus`-CLI vorhanden | `scripts/` |
| V8 | DVCC-Verhalten bei Wegfall des Aggregat-Dienstes (BMS lost → Laden aus?) – T-F1 | Vor-Ort-Test |
| V9 | freie ProductId für den virtuellen Dienst, `/run` als tmpfs für die Heartbeat-Datei | `velib_bus.py`, `main.py` |
