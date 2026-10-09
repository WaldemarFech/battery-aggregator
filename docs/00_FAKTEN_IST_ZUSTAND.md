# Ist-Zustand Batterie-System (Readback 04.10.2026 06:58–07:05Z, nur lesend per MQTT, EMS-Session)

## Packs (3× JK-BMS 16S, dbus-serialbattery 2.0.20250324rc, /data/apps/dbus-serialbattery, Py 3.12, ProductId 47735)
| Instanz | GX-Name | BMS-Name | HW/SW | Port | Kapazität | SoC | Zyklen | Zellen | DCL | CCL | CVL |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | rechtsUnten | links2up | JK 10.XW S10.10 | ttyUSB2 | 305 Ah | 8 % | 249 | 2,987–3,086 V | 43,6 A | 200 A | 56,8 V |
| 2 | linksUnten | links3up | JK 15A S15.34 | ttyUSB3 | 314 Ah | 1 % (3,03 V/Zelle → SoC-Drift, nie voll geladen) | 274 | – | 92 A | 200 A | 56,8 V |
| 3 | linksOben | links1up | JK 11.XW S11.261 | ttyUSB1 | 305 Ah | 11–14 % | 316 | – | 59 A | 200 A | 56,8 V |

- Kein Balancing aktiv, keine Alarme. SocResetLastReached bei allen nie erreicht (SoC unkalibriert).
- Historische Zellspitzen: 3,837 V (Inst. 3), 3,689 V (Inst. 1).
- CustomName überall gleich „SerialBattery(JKBMS)“; GX-Namen ≠ BMS-DeviceNames; Seriennummern eindeutig.

## Venus / DVCC
- Cerbo GX, Venus v3.70~61 (Beta); Backup-Image v3.70~26; online angeboten v3.90-beta5.
- DVCC an, Alarm „MultipleBatteries“=1. Controlling BMS automatisch = Inst. 3; Battery Monitor = battery/3.
- Effektive Limits am Multi: CVL 56,8 V, CCL 171 A, **DCL 59 A nur aus Pack 3**, BatteryLowVoltage 46,4 V.
- → Kernproblem sichtbar: Pack 1 darf laut eigenem BMS nur 43,6 A entladen, die Bank-Grenze kommt aber von Pack 3 (59 A); Packs 1/2 entladen unter dem System-SoC.
- MPPTs: CVL 56,8 V, 100 A (RS) bzw. 35 A (150/35). SVS an, STS aus, SCS aus. Keine User-Limits. ESS State 10, ActiveSocLimit 0, **Min-SoC 0 (Owner: so lassen)**.

## Zugang
- **Kein Root/SSH auf dem Cerbo** (Port 22 im LAN offen, Keys abgelehnt; von CT20050 Timeout). Für Installation später nötig: Owner hinterlegt SSH-Key bzw. gibt Root frei (Settings → General → Access level/SSH).

## Frühere Aggregator-Versuche (Indizien)
- Verwaister Settings-Eintrag com.victronenergy.battery/0 „Alle Batterien“ ohne Gerät → vermutlich früherer Aggregat-/Virtual-Battery-Service auf Instanz 0.
- Viele verwaiste Batterie-Instanzen (4–19, 21, 23–28, 99) → häufige Neuinstallationen/Instanzwechsel.
- Aktuell läuft kein Aggregator; serialbattery-Instanzen seit 28.09. 21:17 stabil.
- Crash-Belege, config.ini, /data/rc.local, Logs: ohne SSH unbekannt.

Quellen: interne Planungsnotizen (DBUS_AGGREGATE_BATTERIES_PLAN, ESS_MIN_SOC_AENDERUNG).
