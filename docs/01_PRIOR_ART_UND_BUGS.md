# 01 – Prior Art und Bug-Forensik: Batterie-Aggregation unter Venus OS

Stand: 2026-10-04. Conclave-Mitglied 1 (Prior Art & Bug-Forensik). Reine Lese-Recherche (Web + GitHub-API; Repos nur lokal im Scratchpad geklont und gelesen). Kein Geraet beruehrt.

Owner-Kontext: Victron ESS, Cerbo GX, Venus OS v3.70~61 beta, DVCC, 3x LiFePO4 48 V parallel, je ein JK-BMS ueber dbus-serialbattery 2.0 RC. Venus nimmt nur EIN BMS als Systembatterie (aktuell Pack 3); CVL/CCL/DCL und SoC kommen daher nur von einem Pack.

Kennzeichnung: **[Code]** = im Quelltext selbst gelesen, **[Issue]** = GitHub-Issue/PR-Text gelesen, **[Community]** = Forenaussage, nicht verifiziert, **[Annahme]** = Schluss von mir.

---

## 1. Kurzfazit

1. Es gibt zwei ernsthafte Open-Source-Aggregatoren: **Dr-Gigavolt/dbus-aggregate-batteries** (MIT, aktiv, Polling, bewusst "crash on failure") und **pulquero/BatteryAggregator** (MIT, aktiv, event-basiert, Per-Pack-Stromanteile, optionaler Reconnect-Timeout). Rest (drurew, Rikkert-RS-Fork, dbus-mqtt-battery + Node-RED) ist Nische, Fork oder Workaround.
2. **dbus-serialbattery 2.x hat keine eigene Aggregation.** Die Default-Config sagt: "If you have multiple batteries, you need a battery aggregator." [Code: `dbus-serialbattery/config.default.ini` Z.13]
3. **"Aggregator stirbt, wenn ein Pack offline geht":** bei Dr-Gigavolt **Design, kein Bug** (feste `NR_OF_BATTERIES`, Lesefehler -> `sys.exit(1)`, Neustart findet N-1 Packs -> erneut Exit -> Service weg von D-Bus). Der Maintainer lehnt Aenderungen ausdruecklich ab (Issues #132, #162, PR #166). Bei pulquero: dynamische Mitgliedschaft, aber stille Haenger (#119, offen), historische None-Crashes (#63/#68/#73) und `reconnectTimeout` Default 0.
4. **"20 A fuer die ganze Bank":** Mehrere Ursachen (Abschnitt 4.2). Ist-Zustand des Owners: Venus liest Limits nur vom selektierten BMS (Pack 3). Dr-Gigavolt nicht-CAN: `min(CCL_i) * N`; pulquero: `min_i(CCL_i * Ratio_i)` nur ueber ladefaehige Packs. Beide schaetzen den Stromanteil; Venus kann nicht pro Pack steuern, das Aggregat-Limit ist immer ein Bank-Limit.
5. **Empfehlung (Abschnitt 6):** Kein Fork von Dr-Gigavolt. Falls Reparatur, dann pulquero als Basis. Fuer die Owner-Anforderungen (Pack faellt aus -> Bank laeuft weiter, Limits pro Pack korrekt) ist ein **kleiner Neubau mit uebernommener Fachlogik und Testfaellen** die robusteste Option.

---

## 2. Vergleichstabelle

| Aspekt | Dr-Gigavolt/dbus-aggregate-batteries | pulquero/BatteryAggregator | drurew/Battery-Aggregator | dbus-serialbattery 2.x | mr-manuel/dbus-mqtt-battery + Node-RED | Rikkert-RS-Fork |
|---|---|---|---|---|---|---|
| Zweck | Virtuelle Batterie aus N parallelen serialbattery-Packs | Virtuelle Batterie, Merge beliebiger Battery-Services, Virtual Batteries | SoC/Spannung fuer CAN-Batterien (SuperB), eigener Imbalance-Limiter | Treiber pro BMS, keine Aggregation | MQTT->D-Bus-Treiber, Aggregation extern | alter Fork des Dr-Gigavolt-Projekts |
| Lizenz | MIT | MIT | NOASSERTION (LICENSE-Datei vorhanden) | AGPL-3.0 | MIT | MIT |
| Stand (`pushed_at`) | 2026-09-23, v4.3.20260923-beta, 90 Sterne, 10 offene Issues | 2026-06-21, v3.49, 49 Sterne, 4 offene Issues | 2026-07-07, 2 Sterne | 2026-09-29, Treiber 2.1.20260729dev, 274 Sterne | 2026-02-02 | 2023-05-16, stale |
| Umfang | 1624 Z. `dbus-aggregate-batteries.py` + dbusmon.py + settings.py, config.ini | 1111 Z. `battery_service.py` + kleine Module, JSON-Config, Tests kaum (`hooks_test.py`) | 331 Z. `bms_aggregator.py` | gross | klein | wie Original |
| Discovery | Einmalig beim Start (`list_names()`, Filter `com.victronenergy.battery` + ProductName); muss **exakt** `NR_OF_BATTERIES` finden, sonst Retry bis `SEARCH_TRIALS`, dann Exit [Code] | Dynamisch ueber DbusMonitor-Callbacks `deviceAdded/Removed`; Ausschluss-Liste per Config [Code] | Feste Service-Liste in config.ini [Code] | n/a | n/a | wie Original |
| Update-Modell | GLib-Timer 1 s Polling; Event-Umbau offen (#165) | Event-getrieben, Aggregator je D-Bus-Pfad (`set/unset` pro Service) | 1 s Poll | n/a | n/a | wie Original |
| CVL | Nicht-OWN: `min(CVL_i)`; mit `KEEP_MAX_CVL` und ohne "Cell OVP": `max`. Optional eigene Ladekurve/Balancing/Dynamic-CVL [Code] | Modi `max_when_balancing` (Default), `min_when_balancing`, `max_always`, `max_when_floating`, `dvcc`; bevorzugt Packs, die nicht floaten [Code] | Fester Konfigwert [Code] | pro Pack | extern | wie Original |
| CCL/DCL | Nicht-CAN: `min(CCL_i) * N`; CAN: `sum`. Eigenmodus: Config-Max x Interpolation ueber Zellspannung; blockiert ein Pack -> 0 [Code Z.1144-1153, 1377ff] | `min_i(CCL_i * Ratio_i)` nur ueber `AllowToCharge != 0`; Ratio `count`/`capacity`/`ir` (gelernte Innenwiderstaende); danach Deckel durch DVCC-User-Limit [Code Z.698-830] | Config-Nennstrom, reduziert nach SoC-Spreizung (100/85/66/33 %) | pro Pack | extern | wie Original |
| SoC | Kapazitaetsgewichtet oder eigener Coulomb-Counter (`OWN_SOC`) | Kapazitaetsgewichtet; seit v3.48 ohne Packs mit DischargeAllowed=0 (#120); Shunt-SoC via `primaryServices` | `min(SoC)`; Wechsel auf Mittelwert am 2026-07-07 revertiert | pro Pack | extern | wie Original |
| Zellspannung min/max | Min/Max ueber alle Packs inkl. Zell-ID/Pack-Name [Code] | #116 (geschl.): anfangs ohne Pack-Unterscheidung | nur Imbalance | pro Pack | extern | wie Original |
| Alarme | `max()` ueber Packs; `_max` schluckt Exceptions und liefert `None`; InternalFailure-Pfad war falsch (#167, gefixt) | Jeder Einzelalarm wird Aggregat-Alarm (Maintainer in #108) | max + eigene Imbalance-Alarme | pro Pack | extern | wie Original |
| Pack verschwindet | **Exit** nach `READ_TRIALS` + `TIME_BEFORE_RESTART`; Neustart findet N-1 -> Exit -> Aggregat weg. Absicht (#132, #162) | `deviceRemoved` -> `unset` in allen Aggregatoren, Rest laeuft; optional `reconnectTimeout` (Default 0) [Code] | `get_bms_value` faengt Exceptions [Code, nicht tief] | Treiber-Restart nimmt Service ~47-50 s vom Bus (Messung in PR #166) | extern | wie Original |
| Pack kommt zurueck | Nur nach Prozess-Neustart, wenn alle N gefunden werden | `deviceAdded` nimmt Pack wieder auf; innerhalb des Timeouts kein Zustandswechsel | folgt Config | Service erscheint neu | extern | wie Original |
| Venus-Kompat. | #106 (Venus 3.70~26, geschl.), #177 (Raspi Venus 3.8); v4.0 fuer neues Venus/GUI v2 | #106 (v3.60~72, CPU), #112 (GUI v2), #115 (alle geschl.); SetupHelper oder manuell | Cerbo/SetupHelper | Venus 3.x | beliebig | alt |
| Wartung | Aktiv, Hauptmaintainer + Contributor (cgoudie, atillack); blockt Design-Aenderungen | Aktiv, Einzelmaintainer | gering | Aktiv (mr-manuel) | gering | tot |

---

## 3. Details

### 3.1 Dr-Gigavolt/dbus-aggregate-batteries
https://github.com/Dr-Gigavolt/dbus-aggregate-batteries (gelesen: `dbus-aggregate-batteries.py`, `functions.py`, `settings.py`, `config.default.ini`).

**Architektur.** Ein Python-Prozess unter daemontools. `_find_batteries()` per GLib-Timer, bis genau `NR_OF_BATTERIES` Services passen; erst dann `_update()` (1 s). `settings.py` erzwingt `NR_OF_BATTERIES >= 2`. Strom optional von Victron-Geraeten (Multi, MPPT, SmartShunt, `CURRENT_FROM_VICTRON`).

**Aggregation [Code].**
- Strom/Leistung: Summe. Spannung/Temperatur: Summe / `NR_OF_BATTERIES` (bei fehlendem Pack verfaelscht).
- Min/Max-Zellspannung: Dictionary "CustomName: ZellId" -> `max/min`.
- Alarme: `Functions._max(list)`, liefert bei **jeder** Exception (z. B. `None` in Liste) `None`.
- CVL/CCL/DCL im Nicht-OWN-Modus siehe Tabelle; `AllowToCharge/Discharge/Balance` ueber `_min`.
- Eigenmodus `OWN_CHARGE_PARAMETERS`: Ladekurve je Monat, Balancing, Dynamic-CVL; **schreibt `/Settings/CGwacs/OvervoltageFeedIn` per D-Bus** (Eingriff in Systemsettings).

**Fehlerphilosophie.** Maintainer (#162): Crash+Neustart sei "bullet-proven technique for Victron programs". (#132): "what you observe is the correct safety behavior". mr-manuel dort: "This is also the default behavior of a Victron native system". Gegenseite (cgoudie, PR #166, am Ende selbst zurueckgezogen): systemcalc erkennt ein BMS ueber `get_value(service,'/Info/MaxChargeVoltage') is not None`; verschwindet der Service, geht DVCC in den "BMS lost"-Pfad. Verifiziert in `dbus-systemcalc-py/delegates/dvcc.py` (ca. Z.1256-1263: `update_solarcharger_control_flags(0,0,None)`, `/Dc/Battery/ChargeVoltage = None`). Haelt der Aggregator stale Werte, bleibt er als BMS registriert und DVCC wendet veraltete Limits an.

**Bekannte Crash-/Fehlerbilder.**
- **#132 (geschl., 2026-02):** Owner schaltete ein BMS fuer Wartung ab. Log: `ERROR:root:Required number of batteries not found. Exiting...` -> Aggregat weg -> DVCC "selected BMS lost" -> Inverter aus. Im Exit zusaetzlich `AttributeError: 'VeDbusItemExport' object has no attribute '_path'` in velib_python `__del__` (Log-Muell). Antwort: korrektes Sicherheitsverhalten.
- **#162 (geschl., "by design"):** Serial-Pfad liest `/Dc/0/Voltage|Current|Power` mit `+=` ohne None-Check -> `TypeError: unsupported operand type(s) for +=: 'int' and 'NoneType'` -> Retry -> `sys.exit(1)` -> `com.victronenergy.battery.aggregate` verschwindet -> VE.Bus "Low Battery"-Alarm moeglich. Der CAN-Pfad hat einen `ValueError`-Guard (Inkonsistenz).
- **PR #166 (nicht gemerged):** Messung einer Fremdinstallation: Treiber-Restart nahm den Service 47 s vom Bus; Aggregator verbrauchte 10 Read-Trials, beendete sich, der Neustart suchte 10 s nach 2 Batterien, fand 1, beendete sich erneut. Kernpunkt: `DbusMonitor.get_value` liefert fuer verschwundene Services `None` statt zu werfen.
- **PR #170 / Commit 068fbc5 (gemerged):** **Stiller Dauerhaenger.** Pack liefert `None` fuer `/Info/MaxChargeVoltage` -> `_min([13.8, None])` schluckt `TypeError` und liefert `None` -> als CVL publiziert -> Logging `"%.1f" % None` wirft ausserhalb des try -> Ausnahme verlaesst den GLib-Timeout-Callback -> GLib entfernt die Quelle -> Treiber aktualisiert nie wieder, Service bleibt mit alten Werten auf dem Bus; `READ_TRIALS` greift nie. Auf Cerbo GX reproduziert. Fix: None wird zum Read-Trial-Fehler.
- **#103 (geschl.):** Aggregation "stoppt nach Stunden" bei 2x JK ueber Bluetooth; dbus-spy zeigte nur ~10 Zeilen. Maintainer: Ursache ausserhalb (Wireless); nach seriellen Adaptern ok. Maintainer berichtet in #162 ein TX-Signalintegritaetsproblem (1 kOhm Pull-up an USB-Seriell-Adapter, galvanisch getrennt) als Ursache wiederholter BMS-Abbrueche bei Hitze. Lehre: kabelgebunden, galvanisch getrennt.
- **#177 (offen, 2026-10-03):** `KEEP_MAX_CVL=True`, Aggregat-CVL niedriger als beide Einzel-CVLs (Venus 3.8). Zusammen mit Commit "Bugfix KEEP_MAX_CVL" (8811ca6) und #169: CVL-Aggregation ist fehleranfaellig, wenn Float/Bulk je Pack divergieren.
- **#142 (geschl.):** Nach Parallelschaltung ca. 60 A Entladung trotz DCL 200 A im Log; Maintainer: DVCC/Thermik pruefen. Log-CCL/DCL sagt nicht, was Venus tatsaechlich anwendet.
- **#130/#131 (geschl.):** Blockiert ein Pack das Laden (Temperatur), wird im Eigenmodus das gesamte Bank-CCL 0; ein Pack entlud allein bis 8 % SoC, das andere stand bei 63 %. CCL_SAFE/DCL_SAFE vom Maintainer abgelehnt.
- **#149/#134 (geschl.):** Verschiedene Pack-Limits: Maintainer empfiehlt "N * Imin" bzw. gleiche Packs. Keine Per-Pack-Summen ausser CAN-`sum`.
- Offen: **#161** None-Temperaturen, **#145/#165/#163** Tests, Event statt Poll, Publish-Deduplizierung.

**Lizenz/Wartung.** MIT. Regelmaessige Releases, externe Contributor liefern viele Fixes; Maintainer lehnt komplexere Fehlertoleranz ab.

### 3.2 pulquero/BatteryAggregator
https://github.com/pulquero/BatteryAggregator (gelesen: `battery_service.py`, README, Issue-Liste).

**Architektur.** `BatteryAggregatorService` mit `DbusMonitor` und Callbacks `deviceAdded/deviceRemoved/valueChanged`. Je D-Bus-Pfad ein Aggregator (`set(service, value)` / `unset(service)`); aktive Pfade (`/Info/MaxChargeCurrent|Voltage|DischargeCurrent`, `/Io/AllowTo*`) triggern gezielte Neuberechnung (`_updateCCL/_updateCVL/_updateDCL/_updateSoc`). JSON-Config: `excludedServices`, `primaryServices` (Shunt-SoC/Strom), `auxiliaryServices`, `virtualBatteries`, `currentRatioMethod`, `cvlMode`, `cclMode`, `capacity`, `reconnectTimeout`.

**Pack weg/zurueck [Code `BatteryReconnection`].** `_battery_removed`: mit `reconnectTimeout == 0` (Default) sofort `_disconnect_battery`, sonst Timer; kommt der Service vor Ablauf zurueck (`_battery_added`), wird der Timer abgebrochen und es gibt keinen Zustandswechsel. Genau die Semantik, die bei Dr-Gigavolt fehlt. Packzahl dynamisch (`/System/NrOfBatteries`).

**CCL/DCL [Code Z.698-830].**
- Nur ladefaehige (`/Io/AllowToCharge != 0`) bzw. entladefaehige Packs zaehlen.
- Je Pack `CCL_i * ratio_i`, Aggregat = `min`. Ratio: `count` -> Anzahl; `capacity` -> Gesamtkap./Kap._i; `ir` -> IR_i/IR_total aus Betriebs-Samples (Fallback `capacity`, dann `count`).
- Modell: Jedes Pack darf nur seinen erwarteten Stromanteil der Bank bekommen; das engste bestimmt die Bank. Korrekte Vorstellung fuer parallele Packs ohne Einzelsteuerung. Schwaeche: Anteil geschaetzt, nicht gemessen.
- DVCC-Vermischung: Bei aktivem DVCC und gesetztem "Limit charge current" wird das Aggregat-CCL auf den User-Wert gedeckelt. Issues #99, #76, #107; `cclMode` (`bms+dvcc`, `bms`, `dvcc`) wurde als Loesung vorgeschlagen und ist im Code vorhanden.

**Bekannte Crash-/Fehlerbilder.**
- **#63, #68, #73 (geschl.):** `unsupported operand type(s) for -: 'float' and 'NoneType'`, `UnboundLocalError: is_charging`, Crash in `_adjust_ir` (Strom eines Packs `null`) im `valueChangedCallback` -> `ve_utils.exit_on_error` ("CRITICAL ... there was an exception ... exit") -> Neustart. Victron-uebliche exit-on-error-Strategie fuer unbehandelte Ausnahmen, nicht fuer fehlende Packs.
- **#119 (offen, 2026-05):** "crash? drop from DBUS": Prozess laeuft (`ps`), im Victron-GUI ist die Verbindung weg, nichts im Log. Kernel-Log des Nutzers: `ftdi_sio ttyUSB2: failed to get modem status: -71`. Gleicher Fehlertyp wie Dr-Gigavolts stiller Haenger (Prozess lebt, Service tot); kein Watchdog.
- **#118 (geschl.):** Start scheitert, wenn ein ausgeschlossenes Geraet fehlt. **#67 (geschl.):** Aggregator loggt Nullen, bis das erste Pack online ist. **#70 (geschl.):** Shunt wird nach USB-Neuenumeration mitaggregiert (`ttyUSBn` nicht stabil).
- **#72/#90 (geschl.):** "CCL wie fuer ein Pack" bzw. "Limits addieren sich nicht mehr, wenn Packs vom BMS abgeschaltet waren" -> Ratio-Modus/Blockierlogik schwer zu durchschauen.
- **#108 (geschl.):** Zwei Daly-BMS: Lade-/Entlade-Oszillation mit DVCC; Float bei einem Einzelalarm.
- **#120 (v3.48):** Pack ohne Entladeerlaubnis zog Aggregat-SoC auf 0 % herunter; jetzt ausgeschlossen.

**Lizenz/Wartung.** MIT; v3.49; Einzelmaintainer, antwortet in Tagen; Tests duenn. Install per SetupHelper oder Skript.

### 3.3 drurew/Battery-Aggregator
https://github.com/drurew/Battery-Aggregator. 331 Zeilen, auf 3x SuperB Epsilon (CAN) zugeschnitten. `min(SoC)`, mittlere Spannung, Summenstrom, **feste Konfig-CCL**, nach SoC-Spreizung reduziert (150 -> 128/99/50 A); liest keine Pack-Limits. Fuer JK/serialbattery ungeeignet, nur Ideengeber fuer die Imbalance-Staffelung. Lizenzfeld "NOASSERTION".

### 3.4 dbus-serialbattery 2.x
https://github.com/mr-manuel/venus-os_dbus-serialbattery (AGPL-3.0). Kein Aggregator.
- `config.default.ini` Z.13: Aggregator noetig bei mehreren Batterien.
- CHANGELOG: Treiber schreibt `None` nicht mehr in `/Info/MaxChargeCurrent|MaxDischargeCurrent`, damit Konsumenten wie dbus-aggregate-batteries nicht mit `TypeError` crashen; `enable.sh` vermeidet ~30 s Ausfall beim Boot, "which broke downstream consumers like dbus-aggregate-batteries which poll dbus once at startup"; Fix eines dbus-Verbindungslecks bei Systemen mit mehreren Batterien (PR #402).
- Konsequenz: Treiber-Restarts (~50 s) und fehlende Werte beim Start sind Normalfall und muessen vom Aggregator toleriert werden.
- CCL/DCL/CVL kommen **pro Pack** aus dem Treiber (Reduktion nach Zellspannung/Temperatur/SoC). 20-A-Werte entstehen, wenn ein Pack in einer Reduktionszone ist.

### 3.5 Venus-Seite: dbus-systemcalc-py
https://github.com/victronenergy/dbus-systemcalc-py (`delegates/dvcc.py`, `delegates/batteryservice.py`; MIT).
- **Genau ein BMS:** `BatteryService._set_bms` waehlt im Automatikmodus `sorted(...)[0]` (nach Instanz) *ein* Service mit `/Info/MaxChargeVoltage is not None`. Native Zusammenfuehrung gibt es nur fuer `com.victronenergy.battery.lynxparallel`. Weitere BMS bleiben unbeachtet. [Code]
- DVCC liest CVL/CCL/DCL nur vom gewaehlten BMS. Verlust -> "BMS lost"-Pfad (siehe oben). [Code]
- **[Community, unverifiziert]:** Suchtreffer behaupten, bei mehreren serialbattery-Instanzen werde der kleinste DCL/CCL vom MultiPlus angewandt, auch wenn das Pack nicht als Batteriemonitor gewaehlt ist (Thread "Batteries of different capacities JKBMS ..."). Vom systemcalc-Code nicht gestuetzt; muesste lesend am Geraet geprueft werden (nicht Teil dieses Auftrags).
- [Community] Aggregator-Neustart waehrend Ladung laesst den Batteriemonitor kurz verschwinden; Strom kann danach kurz zu hoch sein, bis das richtige CCL gesetzt ist.

### 3.6 Weitere
- **mr-manuel/dbus-mqtt-battery + Node-RED** (https://github.com/mr-manuel/venus-os_dbus-mqtt-battery, Discussion #15): Aggregation extern; alle Logik im Flow, keine Fehlerbehandlung out-of-the-box.
- **Rikkert-RS-Fork**: seit 2023 tot, ignorieren.
- Victron-Community "Battery Aggregator Service - Modifications": https://community.victronenergy.com/t/battery-aggregator-service/50839 (nicht im Detail ausgewertet).

---

## 4. Root-Cause-Analyse der Owner-Fehlerbilder

### 4.1 "Wenn eine Batterie offline geht, funktioniert das ganze Merge-Tool nicht mehr"
Kausalkette bei **Dr-Gigavolt** [Code + #132/#162/#166]:
1. Treiber restartet oder BMS-Kommunikation faellt aus -> Service verschwindet (~47-50 s) oder publiziert `None`.
2. `DbusMonitor.get_value` liefert `None` fuer verschwundene Services (kein Fehler).
3. `+=`/`*` auf `None` -> `TypeError` -> `_readTrials` -> nach `READ_TRIALS` (Default 10) und `TIME_BEFORE_RESTART` -> `sys.exit(1)`.
4. daemontools startet neu; `_find_batteries()` verlangt `batteriesCount == NR_OF_BATTERIES`, findet N-1, wiederholt bis `SEARCH_TRIALS`, `sys.exit(1)`. Schleife, solange das Pack fehlt.
5. Aggregat-Service weg -> "BMS lost" -> DVCC stoppt Solarlader-Control, VE.Bus kann "Low Battery" melden, Inverter-Abschaltung (#132).
Zusaetzlich: Fehler ausserhalb des try im GLib-Callback fuehren zum **stillen Dauerhaenger** (Commit 068fbc5).

Bei **pulquero** laeuft der Aggregator bei Pack-Verlust weiter, aber: unbehandelte None-Pfade (#63/#68/#73) beenden den Prozess; stille Haenger ohne Watchdog (#119); `reconnectTimeout` Default 0.

Architektur-Grundursachen:
- **Fester Soll-Zustand (N Packs)** statt dynamischer Mitgliedschaft.
- **Kein Zustandsmodell pro Pack** (online / stale / offline / blockiert); `None` ist implizites Signal und wird in `_min/_max` still verschluckt.
- **Prozess-Tod = Verschwinden des Aggregat-Service**, und Venus wertet das als BMS-Verlust.
- **Keine Trennung "Eingabedaten ungueltig" vs. "Programmfehler".**
- **Polling-Callback ohne aeusseren Schutz** (GLib entfernt die Quelle bei Exception).

### 4.2 "Ein Pack-Limit (z. B. 20 A) wird auf die ganze Bank angewandt"
1. **Ist-Zustand ohne Aggregator:** Venus sieht nur Pack 3 als BMS; dessen CCL/DCL/CVL gilt fuer die Bank [systemcalc-Code].
2. **Dr-Gigavolt nicht-CAN:** `min(CCL_i) * N`. 20 A am engsten Pack -> 60 A fuer 3 Packs. Nur korrekt bei gleicher Stromaufteilung; bei verschiedenen Innenwiderstaenden/SoC teilt sich der Strom ungleich [Annahme]. Blockiert ein Pack (CCL=0 bzw. `NrOfModulesBlockingCharge>0`), wird das **ganze** Bank-CCL 0 (#130). CAN-Pfad nutzt `sum`.
3. **pulquero:** `min_i(CCL_i*Ratio_i)`; Ratio `count` ist die Anzahl **ladefaehiger** Packs, nicht der installierten (#90: Limits "addieren sich nicht mehr", wenn Packs vom BMS getrennt waren). `ir` lernt den Anteil; bis genug Samples gibt es Fallback `capacity`.
4. **DVCC-Vermischung:** pulquero deckelt das Aggregat-CCL mit dem GUI-DVCC-Limit (#99/#107). Das GUI-Limit gilt fuer Multi-Ladung und ist ein anderes Konzept als Pack-Schutz.
5. **Venus-Grenze:** Strom kann nicht pro Pack begrenzt werden. "Per-Pack-Handling" heisst: pro Pack den zulaessigen Anteil berechnen und daraus das Bank-Limit ableiten. Offen bleibt der **gemessene** Pack-Anteil (Ist-Strom je BMS) - das nutzt keines der Projekte.
6. `min` allein ist zu konservativ (Ausfall -> alles 0), `sum` allein zu aggressiv (Gleichverteilung wird ignoriert), share-gewichtet ist richtig, braucht aber Messdaten.

---

## 5. Limit-Aggregationsstrategien im Vergleich

| Strategie | Wer | Vorteil | Risiko |
|---|---|---|---|
| `min` unskaliert | Venus Single-BMS-Fall | trivial | unterschaetzt Bank massiv |
| `min * N` | Dr-Gigavolt (nicht-CAN) | konservativ, einfach | falsches N bei Ausfall; Blockade eines Packs -> 0; ungleiche Aufteilung nicht abgedeckt |
| `sum` | Dr-Gigavolt (CAN), manuell | maximal | ueberlastet schwaches Pack bei ungleicher Aufteilung |
| `min(CCL_i * Ratio_i)` (count/capacity/IR) | pulquero | richtiges Modell, dynamisch | Anteil geschaetzt, DVCC-Deckel vermischt |
| fest + SoC-Spreizungsstaffel | drurew | einfach | ignoriert Pack-Limits |

---

## 6. Basis reparieren oder neu bauen?

**Dr-Gigavolt**
- Pro: reif, viele Features (Eigenmodus, Balancing, Shunts), MIT, aktive Contributor, Issue-Historie belegt Praxis mit JK/serialbattery.
- Contra: Ausfallverhalten ist Philosophie des Maintainers (PR #166 abgelehnt) -> dauerhafter Fork noetig, Upstream-Merges konfliktieren; `NR_OF_BATTERIES` durchzieht Berechnungen (Division, Min*N, Suche); 1600-Zeilen-Monolith mit globalem State; `_min/_max` verschlucken Fehler; GLib-Callback ohne aeusseren try; Eigenmodus schreibt Systemsettings.
- Urteil: **nicht als Basis geeignet**, aber Quelle fuer Feldwissen (Fehlerkatalog, ProductName-Filter, CVL-/Balancing-Logik, Zell-Min/Max-Aggregation).

**pulquero**
- Pro: dynamische Mitgliedschaft, Reconnect-Timer (passt zu ~50 s Treiber-Restart), Per-Pack-Ratio, nur ladefaehige Packs, Shunt-SoC via `primaryServices`, MIT, offener Maintainer.
- Contra: `reconnectTimeout=0` Default, kein Watchdog (#119), DVCC-Limit vermischt sich, Einzelalarm = Aggregat-Alarm, IR-Lernmodus intransparent, kaum Tests, Venus-3.7x/GUI-v2-Kompatibilitaet war Thema.
- Urteil: **beste Basis, falls repariert werden soll.** Reparaturumfang: (a) sinnvoller `reconnectTimeout`-Default (z. B. 120 s), (b) Watchdog fuer stille Haenger, (c) None-/Fehlerpfade zentral abfangen statt Prozess beenden, (d) CCL ohne DVCC-Vermischung, (e) Plausibilitaetscheck Pack-Anteil.

**Argumente fuer Neubau (empfohlen, bewusst klein)**
- Owner-Anforderungen (Ausfallrobustheit, Per-Pack-Limits, sauberer Stale-Zustand) sind Architekturthemen und in keinem Projekt Kernziel.
- Venus-Verhalten ist per Code-Analyse klar (ein BMS, BMS-lost-Pfad); ein kleines Programm (~500 Zeilen) ist machbar: pro Pack Zustandsautomat (online / stale < T / offline / blocked), Aggregation nur ueber gueltige Packs, explizite Degradation (Bank-Limits aus Restpacks; nie 0 allein durch fehlendes Pack, echte Blockade/Alarm beachten), Service bleibt bei Pack-Verlust auf dem Bus (kein `sys.exit`), Watchdog fuer Event-Loop, Fehlerklassen "Datenfehler" vs. "Programmfehler" (nur Letzterer darf Exit).
- Tests per Fault-Injection (None, Service weg 50 s/200 s, Pack kommt zurueck, Pack blockiert) ohne Venus-Hardware; PR #166 enthielt dafuer eine Harness mit gestubbten `dbus/vedbus/gi/GLib/dbusmon` (Muster uebernehmbar).

**Empfohlene Entscheidung (Mitglied 1):** Neubau mit uebernommener Fachlogik; pulquero als funktionale Referenz (Ratio-/Reconnect-Konzept), Dr-Gigavolt als Quelle fuer CVL-/Balancing-Fakten und Fehlerkatalog. Als Zwischenloesung evtl. pulquero mit `reconnectTimeout` und `cclMode: "bms"`, aber nur nach Offline-Fault-Tests (nicht Teil dieser Recherche).

---

## 7. Offene Fragen / Risiken

1. **Stale-Politik:** Wie lange gelten letzte Werte eines Packs, wie verhaelt sich die Bank in der Zwischenzeit? Sicherheit (voller Pack unbemerkt weitergeladen) vs. Verfuegbarkeit (Kuehlschrank/Inverter). Beide Maintainer-Argumente sind valide; Owner-Entscheidung. Mitigation: JK-BMS schuetzt per Hardware, Bank-CCL/CVL konservativ reduzieren, wenn ein Pack stale ist.
2. **Pack-Anteil messen:** Pack-Strom `/Dc/0/Current` je Treiber lesen, Ungleichverteilung als Alarm.
3. **BMS-lost-Semantik in Venus 3.70~61 beta** lesend pruefen (BMS-Auswahl, `lynxparallel`, `/Control/Dvcc`).
4. **Service-Namen:** nicht `ttyUSBn` (instabil, pulquero #70), sondern `/Serial`, `/CustomName`, `/DeviceInstance`.
5. **Treiber-Abhaengigkeit:** dbus-serialbattery 2.0 RC vs. 2.1-dev; Restart-Fenster (~50 s) und Boot-Fenster (~30 s) beachten.
6. **Lizenz:** dbus-serialbattery ist AGPL; wir importieren keinen Code, nur D-Bus-Pfade. Dr-Gigavolt/pulquero MIT -> Uebernahme mit Attribution moeglich.
7. Datumsangaben stammen aus GitHub-Metadaten (Systemdatum 2026-10-04). Nicht jedes Issue vollstaendig gelesen, Kommentare auf 3-4 je Issue begrenzt.

---

## 8. Quellen

Repos
- https://github.com/Dr-Gigavolt/dbus-aggregate-batteries
- https://github.com/pulquero/BatteryAggregator
- https://github.com/drurew/Battery-Aggregator
- https://github.com/mr-manuel/venus-os_dbus-serialbattery
- https://github.com/mr-manuel/venus-os_dbus-mqtt-battery und https://github.com/mr-manuel/venus-os_dbus-mqtt-battery/discussions/15
- https://github.com/Rikkert-RS/VenusOS_dbus-aggregate-batteries
- https://github.com/victronenergy/dbus-systemcalc-py

Issues/PRs (Dr-Gigavolt)
- https://github.com/Dr-Gigavolt/dbus-aggregate-batteries/issues/132
- https://github.com/Dr-Gigavolt/dbus-aggregate-batteries/issues/162
- https://github.com/Dr-Gigavolt/dbus-aggregate-batteries/pull/166
- https://github.com/Dr-Gigavolt/dbus-aggregate-batteries/pull/170
- https://github.com/Dr-Gigavolt/dbus-aggregate-batteries/issues/130 , /131 , /134 , /142 , /149 , /103 , /177

Issues (pulquero)
- https://github.com/pulquero/BatteryAggregator/issues/119 , /90 , /99 , /107 , /72 , /73 , /63 , /68 , /108 , /118 , /120

Community
- https://community.victronenergy.com/t/batteries-of-different-capacities-jkbms-non-inverter-style-plus-mr-manuel-dbus-serialbattery-setup/44899
- https://community.victronenergy.com/t/battery-aggregator-service/50839
- https://communityarchive.victronenergy.com/questions/260143/battery-aggregator.html
- https://louisvdw.github.io/dbus-serialbattery/faq/
