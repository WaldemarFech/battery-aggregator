# 04 – Sicherheit: Gefährdungsanalyse (FMEA), Pflichtanforderungen, Abnahmetests

Status: Entwurf (Konklave-Mitglied 3, Safety Red Team) · Stand 2026-10-04 · reine Analyse, kein Gerätezugriff
Geltungsbereich: virtueller Batterie-Dienst `battery-aggregator` auf Cerbo GX (Venus OS v3.70~61 beta, DVCC),
3 × LiFePO4 48 V (16s) parallel, ~314 / 305 / 305 Ah, je JK-BMS über dbus-serialbattery 2.0 RC.

> Grundhaltung dieses Dokuments: Der Aggregator ist eine **sicherheitsrelevante Steuerinstanz**. Er bestimmt CVL/CCL/DCL
> für Multi/Quattro und alle Laderegler. Jeder Fehler in ihm wirkt auf **alle drei Packs gleichzeitig**. Die BMS-Abschaltung
> ist die letzte Schutzebene, nicht die Regelgröße. Alles, was mit "zu verifizieren" markiert ist, ist eine Annahme, bis es
> vor Ort gemessen/getestet wurde.

---

## 1. Systemmodell und Grundannahmen

- **Physik der Parallelschaltung:** Die Packs teilen dieselbe Klemmenspannung. Stromaufteilung folgt den Innenwiderständen
  (Zellen + BMS-MOSFETs + Shunt + Kabel/Sammelschiene) und der OCV je Pack. Anteile `share_i` sind **nicht konstant**:
  sie hängen von SoC (besonders an den Kennlinien-Knien < 15 % und > 90 %), Temperatur, Stromrichtung, Stromhöhe und
  **Zeit nach einem Lastsprung** ab (ohmsch sofort, Diffusion/RC über Sekunden bis Minuten).
- **Limit-Formel des Plans:** `I_bank_max = min_i (limit_i / share_i)`. Korrekt nur, wenn `share_i` den **ungünstigsten**
  aktuellen Anteil beschreibt und alle `limit_i` aktuell sind.
- **N-1-Prinzip (Pflicht):** Fällt Pack k plötzlich weg (BMS öffnet, Sicherung fällt), verteilt sich der Strom sofort auf
  die übrigen: `share_i' ≈ share_i / (1 − share_k)`. Das veröffentlichte Limit muss **schon vorher** für diesen Fall halten:

  ```
  I_bank_N1 = min_k  min_{i≠k}  limit_i · (1 − share_k) / share_i
  ```

  Beispiel: shares 0,35/0,33/0,32, limit je 150 A → Normal-Limit 428 A, **N-1-Limit 295 A**. Bei nur noch 2 aktiven Packs
  ist N-1 = Limit des schwächeren Einzelpacks (~150 A). Ohne N-1 droht die **Kaskade**: Pack A trippt → B/C überlastet →
  B trippt → C trägt alles → C trippt → Totalausfall (off-grid: Blackout; on-grid: Hausnetz bleibt, aber Anlage steht).
- **Grenzen der Durchsetzbarkeit:** DCL wird vom Multi im ESS mit Netz durch Netzbezug eingehalten. **Off-grid/Netzausfall
  kann DCL nicht durchgesetzt werden** (Last ist Last) → dort ist nur N-1-Dimensionierung + BMS-OCP + Sicherung wirksam.
  AC-gekoppelte PV (falls vorhanden) wird über Frequenzverschiebung nur träge geregelt → CCL-Überschwinger möglich.
- **Victron-Konventionen:** `/Dc/0/Current` positiv = Laden. CCL/DCL sind beide **positive** Beträge in A. CVL in V.
  `/Soc` in %, `/Capacity` / `/InstalledCapacity` in Ah. Ungültig = `None`/leeres dbus-Array, nicht 0.
- **Lastprofil:** Tesla-Wallbox 11 kW AC ≈ 230–250 A DC aus der Bank (inkl. Wandlerverluste), Anlauf in wenigen Sekunden
  (Rampe zu verifizieren). Pro Pack normal ~80 A, bei N-1 ~125 A, bei N-2 ~250 A.

---

## 1a. Befunde aus dem Ist-Zustand (Quelle: `00_FAKTEN_IST_ZUSTAND.md`, Readback 04.10.2026)

Mehrere FMEA-Gefährdungen sind **heute schon aktiv**, nicht hypothetisch:

| Befund | Bezug | Bewertung |
|--------|-------|-----------|
| Controlling BMS = automatisch = Pack 3; Alarm `MultipleBatteries`=1. DVCC sieht nur Pack 3 (CVL/CCL/DCL, Temperatur, Zellen). | C13, F3 | **Aktiver Zustand von F3.** Zell-OV, Temperatur und Limit-Reduktion von Pack 1/2 wirken nicht auf Multi/MPPT; nur deren eigene BMS-Abschaltung schützt. |
| Pack-DCL 43,6 / 92 / 59 A, effektive Bank-DCL **59 A nur aus Pack 3**. | E2, E5, C4 | Pack 1 will 43,6 A. Bei gleichmäßiger Aufteilung (~20 A/Pack) wird das eingehalten. Überschritten wird es, sobald Pack 1 > 74 % des Bankstroms trägt (43,6/59): nach Wegfall von Pack 2+3 (N-2) oder wenn sich die Aufteilung verschiebt (Pack 2 mit ~3,03 V/Zelle liegt am unteren Knie und liefert dort überproportional wenig). Schwerer wiegt: die **Absicht** des Pack-1-BMS (abregeln, weil zu leer) kommt beim Multi nicht an. Pack 1 wird unter seinem eigenen Limit-Regime entladen, bis zur harten BMS-UV-Abschaltung → E2/E3. |
| Zum Vergleich mit der geplanten Formel (gleiche shares 1/3): Normal = min(43,6·3; 92·3; 59·3) = **130,8 A**, N-1 = **87,2 A** (Pack 2 oder 3 fällt weg, Pack 1 trägt 0,5). | SR-01 | Das Aggregat würde die Bank-DCL von 59 auf 87 A **anheben**. Bei den gemessenen shares ist das nur zulässig, wenn der Pack-1-Anteil belegt ist (Kap. 8, Messung 3). Bis dahin gilt: Bank-DCL ≤ heutige 59 A (Inbetriebnahme-Deckel, SR-21). |
| SoC unkalibriert (`SocResetLastReached` nie erreicht); Pack 2 zeigt 1 % bei 3,03 V/Zelle, aber DCL 92 A. | E8, C7 | SoC-basierte Taper in serialbattery stützen sich auf falsche SoCs, die Pack-Limits sind also teils willkürlich, sie müssen aber trotzdem eingehalten werden. Ein SoC-gewichteter Bank-SoC wäre heute Unsinn. Schutz muss über **Zellspannungen** laufen. Vor Produktivbetrieb mindestens eine Vollladung je Pack (SoC-Sync) unter Zellüberwachung. |
| Historische Zellspitzen **3,837 V (Pack 3)** und 3,689 V (Pack 1); CVL 56,8 V = 3,55 V/Zelle Mittel, kein Balancing aktiv. | E4, C17 | **E4 hat bereits stattgefunden** (> 3,65 V). Bei 3,55 V-Mittel mit Imbalance läuft die höchste Zelle davon. Pflicht: CCL-Taper nach max Zellspannung aller Packs (SR-06), CVL-Absenkung prüfen (z. B. 55,2 V), Balancing-Parameter der JKs prüfen. |
| ESS Min-SoC = 0 (Owner: so lassen); BatteryLowVoltage 46,4 V (2,9 V/Zelle). | C7, E5 | Entladung endet erst über DCL-Taper bzw. die BMS-UV. Die Aggregator-DCL nach min Zellspannung/min Pack-SoC ist damit die **einzige** weiche Schutzebene vor dem harten BMS-Trip. Sie muss auch ohne Min-SoC-Puffer greifen. |
| CustomName überall gleich, GX-Namen ≠ BMS-Namen, Seriennummern eindeutig. | C11, SR-12 | Bestätigt: Identität **nur** über Seriennummer. |
| Verwaiste Instanz 0 „Alle Batterien“ (früherer Aggregator) und viele verwaiste Instanzen 4–99. | C11, C13, F4 | Instanzkollisionen und Fehlauswahl sind wahrscheinlich. Feste, neue DeviceInstance wählen und die Altlasten im Settings-Baum dokumentieren. Battery Monitor/Controlling BMS danach explizit setzen. |
| Venus v3.70~61 **Beta**, v3.90-beta5 wird angeboten. serialbattery 2.0 **RC**. | F4, F5 | Auto-Update **aus**. Kein Update ohne Re-Abnahme (SR-16). |
| MPPTs (RS 100 A, 150/35) unter DVCC, SVS an. Keine User-Limits. | C18 | Ladequellen sind DVCC-gesteuert. Inventar auf AC-PV ergänzen. |
| **Kein SSH/Root** auf dem Cerbo. | Kap. 7.2, 8 | Tests T-F1/F2/F4/F6, der Pfad-Dump und die Watchdog-Prüfung sind ohne Owner-Freigabe (SSH-Key/Access-Level) nicht durchführbar. Das blockiert die Inbetriebnahme. Read-only-Beobachtung per MQTT bleibt möglich. |

---

## 2. Bewertungsskala

| S | Schwere | Bedeutung |
|---|---------|-----------|
| 4 | Kritisch | Brand-/Zellschadensrisiko, Zellüberladung, Laden unter 0 °C, Kaskaden-Totalausfall, Kurzschlussenergie |
| 3 | Hoch | BMS-Notabschaltung im Betrieb, Blackout off-grid, dauerhafte Alterung, falsche Limits über längere Zeit |
| 2 | Mittel | Kapazitäts-/Komfortverlust, unnötiges Abregeln, Fehlalarme, falsche Anzeige |
| 1 | Gering | Kosmetik, Logging |

Entdeckung (D): **A** = automatisch im Aggregator, **V** = Venus/Multi erkennt, **B** = nur BMS (letzte Ebene), **M** = nur manuell/nie.

---

## 3. FMEA – Elektrisch/physikalisch

| ID | Fehler | Ursache | Wirkung | D | Gegenmaßnahme | S |
|----|--------|---------|---------|---|---------------|---|
| E1 | Ausgleichsstrom beim Wiederzuschalten eines Packs | Pack war offline (BMS-Trip, Wartung), SoC/OCV weicht ab; JK ohne Precharge | Inrush 50–300 A (ΔU 1–3 V über ~10–20 mΩ Schleife) zwischen Packs, unsichtbar für Multi; OCP-Trip, MOSFET-Stress, Funkenbildung am Schalter | B (A teilweise) | Kein automatisches Wiederzuschalten durch Software; Aggregator meldet ΔU zum Bankmittel und gibt "Zuschaltfreigabe" erst bei ΔU < Schwelle (vor Ort festlegen, Richtwert ≤ 0,1 V im Flachbereich, ≤ 0,05 V nahe Knie); Precharge-Prozedur/Widerstand im Runbook; nach Reconnect 60 s erhöhte Sicherheitsmarge auf Limits | 4 |
| E2 | BMS öffnet MOSFETs unter Last | OCP, Zell-OV/UV, Temperatur, Kommunikations-/Firmwarefehler | Strom springt **sofort** auf die übrigen Packs (µs), Aggregator/Multi reagieren erst nach Sekunden | B | **N-1-Limit** (Kap. 1) dauerhaft veröffentlichen; BMS-OCP-Verzögerungen dokumentieren; DCL-Reserve für Tesla-Sprung | 4 |
| E3 | Kaskadentrip | Folge aus E2 ohne N-1-Reserve oder bei bereits N=2 | Totalausfall, off-grid Blackout, ggf. Reconnect-Inrush (E1) beim Hochfahren | B | N-1 auch bei N=2 (dann Einzelpack-Limit); bei N=1 Notbetrieb mit stark reduziertem Limit + Alarm | 4 |
| E4 | Zell-Überspannung im vollsten Pack bei unauffälliger Bankspannung | Packs/Zellen unterschiedlich balanciert; Bank-CVL = Mittelwert-Sicht | Einzelzelle > 3,65 V, Alterung/Plating; BMS trennt Laden → Ladestrom verteilt sich auf übrige (E2 im Ladebetrieb) | A (wenn Zelldaten frisch) | CVL = **min** aller Pack-CVL; zusätzlich CCL-Abregelung nach **max Zellspannung über alle Packs** (Taper ab z. B. 3,45 V, 0 A ab 3,55 V – Werte festlegen); Zelldaten-Frische Pflicht | 4 |
| E5 | Zell-Unterspannung im leersten Pack | SoC-Drift, Imbalance, flache Kennlinie | BMS trennt Entladen → E2 im Entladebetrieb | A | DCL-Taper nach **min Zellspannung aller Packs** (spannungsbasiert, nicht nur SoC); Min-SoC-Logik nach Kap. 4 C7 | 3 |
| E6 | Laden bei Kälte | Ein Pack (Garage, Außenwand) < 0…5 °C, Bank-Mittelwert ok | Lithium-Plating, irreversibler Schaden, Brandrisiko | A + B | CCL = 0 wenn **irgendein** aktiver Pack min-Temp < Schwelle (z. B. 5 °C, Taper bis 10 °C); fehlender Temperaturwert eines Packs = CCL 0 (fail-safe); JK-Low-Temp-Charge-Protection zusätzlich aktiv prüfen | 4 |
| E7 | Laden bei Übertemperatur | Pack heiß nach Tesla-Ladung, schlechte Belüftung | Alterung, OT-Trip | A + B | CCL/DCL-Taper nach max Temp aller Packs | 3 |
| E8 | Fehlerhafte SoC je Pack | JK-Coulomb-Counter driftet, Kapazitätsparameter falsch, Synchronisation bei 100 % unterschiedlich, flache Kennlinie | Aggregierter SoC falsch, ESS entlädt zu tief / lädt nie voll; Balancing zu selten | A teilw. | SoC nur als Komfortgröße; Schutz über Zellspannungen; JK-Nennkapazitäten gegen 314/305/305 Ah prüfen; regelmäßiger Vollladezyklus (Sync); Plausibilisierung SoC vs. OCV in Ruhe | 2 |
| E9 | Stromsensor-Offset im JK | Shunt-Offset ±0,5–2 A, Temperaturdrift | Falsche shares bei kleinen Strömen (Division durch ~0), SoC-Drift | A | shares nur aus Messungen mit |I_bank| > Mindeststrom (z. B. 20 A) lernen; Offset je Pack in Ruhe kalibrieren; Σ Pack-Ströme gegen Multi/Shunt-Strom plausibilisieren | 2 |
| E10 | Share-Schätzung hinkt Lastsprung hinterher | Filterzeitkonstante; dynamische Aufteilung ≠ statische; Knie-Effekte | Unterschätzter share_i → zu hohes Bank-Limit genau beim Tesla-Start | A | Konservativer share: **max** aus (gefiltertem Wert, Kurzfenster-Max, Floor = 1/N + Marge); asymmetrische Filter (schnell hoch, langsam runter); share nie < z. B. 0,8·(1/N_aktiv) | 3 |
| E11 | Ungleiche Kabel-/Sammelschienenwiderstände | Unterschiedliche Kabellängen/Klemmen, lose Verbindung | Dauerhaft ungleiche shares, ein Pack trägt mehr → altert schneller; lose Klemme → Hitze | A (Trend) | shares-Trend überwachen; Alarm bei Drift > X %/Monat; Kabelwiderstand vor Ort messen; Drehmoment-Check | 3 |
| E12 | Kurzschluss/Fehler in einem Pack | Zelldefekt, Isolationsfehler | Andere Packs speisen den Fehler; Software irrelevant | M | **Hardware-Pflicht:** Sicherung je Pack (Class-T/NH, Abschaltvermögen für Bank-Kurzschlussstrom), Trennschalter je Pack | 4 |
| E13 | Charge-FET offen, Entlade-Pfad über Body-Diode | JK trennt nur Laden | Pack entlädt weiter über Diode (Verlustwärme), zeigt aber "online" | A | Status je Pack (ChargeFET/DischargeFET/AllowToCharge/AllowToDischarge) auswerten; Pack mit offenem Charge-FET zählt für Lade-shares als **weggefallen** | 3 |

---

## 4. FMEA – Regelung / Venus / DVCC

| ID | Fehler | Ursache | Wirkung | D | Gegenmaßnahme | S |
|----|--------|---------|---------|---|---------------|---|
| C1 | Zu langsame Wirkung von CCL/DCL | Aggregator-Zyklus + dbus + systemcalc/DVCC-Zyklus + VE.Bus + Multi-Rampe (Summe mehrere s, zu messen) | Limits greifen erst nach Sekunden; Tesla-Sprung überlastet kurzfristig | M (messen) | Reaktionszeit messen (T3); Limit so wählen, dass BMS-OCP-Verzögerung > Gesamtreaktionszeit; N-1 + Marge statt "auf Kante" | 3 |
| C2 | Oszillation | Limit ↔ Stromaufteilung ↔ share-Schätzung koppeln; Taper an Zellspannung + Multi-Rampe | Pendelnde Ladeleistung, Relais/Lüfter-Klappern, Stress | A | Rate-Limiter: Limits **sofort runter, langsam hoch** (z. B. max +5 A/s); Hysterese an allen Schwellen; share-Filter getrennt von Limit-Filter | 2 |
| C3 | Veraltete Daten als gültig | serialbattery hängt, RS485 weg, dbus-Wert ändert sich nicht, Venus hat keine Zeitstempel | Aggregator rechnet mit letzten (evtl. hohen) Limits eines Packs, der real getrennt ist | A | Frische-Erkennung je Pack: `/UpdateIndex` o. ä. (Vorhandenheit in serialbattery 2.0 **zu verifizieren**), sonst Änderung von Spannung/Zellwerten/Zeitstempel; stale > T_stale (z. B. 10 s) → Pack gilt als **weggefallen** (N-1-Rechnung ohne ihn, seine shares = 0, Rest wird hochskaliert) | 4 |
| C4 | Pack offline, aber Bank-Limit unverändert | Offline-Pack einfach aus `min()` gestrichen | Bank-Limit **steigt** fälschlich (weniger Terme im min), obwohl physisch weniger Packs tragen | A | Bei Wegfall: shares der übrigen neu normieren **und** N-1 über die verbleibenden; Limit darf bei Pack-Verlust **nie steigen** (Invariante, Test) | 4 |
| C5 | Unbekannter Zustand: Pack kommuniziert nicht, ist aber physisch verbunden | Nur Kommunikationsausfall | Ströme der Bank fließen weiter durch ihn, aber Limits/Temperatur/Zellen unbekannt | A | Unbekannter Pack = **Worst Case**: keine Zell-/Temp-Information → CCL konservativ (Temperatur unbekannt → kein Laden unter Saisonbedingung; Policy festlegen), Alarm "Pack X blind" | 4 |
| C6 | Vorzeichenfehler | JK/serialbattery-Konvention ≠ Victron; eigene Summierung | shares negativ, Division liefert negative/unendliche Limits; Laden als Entladen gewertet | A | Konvention einmal zentral normieren; Plausibilität: sign(Σ I_pack) = sign(I_Multi); Tests mit beiden Richtungen | 3 |
| C7 | ESS-Min-SoC auf aggregiertem SoC | Gewichteter SoC 20 %, schwächster Pack real 8 % | Schwächster Pack erreicht UV → Trip → E2 | A | Veröffentlichter SoC: kapazitätsgewichtet (Anzeige) **plus** DCL-Taper nach min Pack-SoC und min Zellspannung; Option "konservativer SoC = min Pack-SoC" konfigurierbar; Owner-Entscheidung dokumentieren | 3 |
| C8 | Einheiten-/Skalierungsfehler | mA vs A, Ah vs Wh, %·10, Zellspannung mV | Limits um Faktor 10/1000 falsch | A | Wertebereichsprüfung jedes Eingangs (z. B. Zellspannung 2,0–4,0 V, Pack-Spannung 40–60 V, Temp −30…+80 °C, Limit 0–500 A); außerhalb → Wert ungültig | 4 |
| C9 | NaN/None/Inf-Propagation | Fehlende dbus-Werte, Division durch share=0, `min([])` | NaN veröffentlicht → Venus-Verhalten undefiniert; Exception → Prozess-Absturz | A | Jede Rechnung über Validierungsfunktion; Division nur mit share ≥ Floor; leere Menge → Limit 0; **NaN/None am Ausgang nie veröffentlichen** → stattdessen 0 A Laden, Entladen gemäß Notpolicy | 4 |
| C10 | Uhrsprung | NTP-Sync nach Boot, Zeitumstellung, RTC leer | Stale-Erkennung/Filter falsch (alles stale oder nie stale), Rate-Limiter springt | A | Ausschließlich `time.monotonic()` für Zeitlogik; Wanduhr nur für Logs | 3 |
| C11 | serialbattery-Neustart benennt Dienste um | ttyUSB-Neuvergabe nach Reconnect/Reboot, DeviceInstance-Neuvergabe | Pack doppelt (alter + neuer Dienst) → shares Summe > 1, Kapazität doppelt; oder Pack-Zuordnung (314 vs 305 Ah) vertauscht | A | Identität über BMS-Seriennummer/HW-Version/Custom-Name, **nicht** tty/Dienstname; Dienst-Verschwinden via NameOwnerChanged verfolgen; Duplikat-Erkennung (gleiche Seriennummer) → älteren verwerfen; erwartete Pack-Anzahl konfiguriert (3) → Abweichung = Alarm | 3 |
| C12 | Aggregator liest eigenen Dienst | Discovery über `com.victronenergy.battery.*` ohne Ausschluss | Rückkopplung, Summen verdoppelt | A | Eigenen Dienst und andere virtuelle Batterien explizit ausschließen (Allowlist nach Produkt/Seriennummer) | 3 |
| C13 | Venus wählt falschen Battery-Monitor/Controlling BMS | Einstellung "Automatisch"; nach Update/Reset; DeviceInstance des Aggregators geändert | DVCC nutzt Einzelpack-Limits/-CVL oder Multi-SoC | V | Battery Monitor **und** Controlling BMS fest auf Aggregator; feste DeviceInstance; Selbsttest: Aggregator prüft per dbus, ob er der aktive BMS ist, sonst Alarm | 3 |
| C14 | Mehrere BMS-Dienste mit CCL/DCL sichtbar | Einzel-serialbattery-Dienste bleiben aktiv sichtbar | Venus-Alarm "mehrere BMS"/Verwirrung, Auswahl unklar | V | Verhalten in v3.70 prüfen; ggf. Einzel-Dienste so konfigurieren, dass sie nicht als Controlling BMS wählbar sind (Option in serialbattery prüfen) | 2 |
| C15 | AllowToCharge/AllowToDischarge falsch aggregiert | OR statt AND; fehlender Wert = true | Laden trotz Sperre eines Packs | A | **AND** über alle aktiven Packs; fehlend/unbekannt = false (Laden) | 4 |
| C16 | Alarm-Aggregation unvollständig | Nur Bank-Werte geprüft | Pack-Alarme (Zell-OV, Temp, Imbalance) unsichtbar in VRM | A | `/Alarms/*` = Maximum über alle Packs + eigene Alarme (Pack fehlt, stale, share-Drift, ΔU zu groß, N<3) | 2 |
| C17 | CVL zu hoch / Absorption endlos | CVL aus Mittelwert oder höchstem Pack; kein Float-Wechsel | Dauerhaft hohe Zellspannung, Alterung, E4 | A | CVL = min(Pack-CVL); Absorptionszeit/Float-Logik nur an **einer** Stelle (serialbattery je Pack **oder** Aggregator, nicht beide gegeneinander) | 3 |
| C18 | Ladequellen außerhalb DVCC | AC-gekoppelte PV, externe Lader ohne DVCC-Anbindung | CCL wird überschritten | M | Inventar aller Ladequellen; nicht steuerbare Leistung als Konstante vom CCL-Budget abziehen | 3 |

---

## 5. FMEA – Ausfall des Aggregators selbst

| ID | Fehler | Ursache | Wirkung | D | Gegenmaßnahme | S |
|----|--------|---------|---------|---|---------------|---|
| F1 | Prozess stürzt ab | Exception, OOM | dbus-Dienst verschwindet. Bei fest gewähltem Controlling BMS: Venus meldet BMS-Verlust (Fehler #67 "BMS connection lost", Verhalten in v3.70 **zu verifizieren**: Laden gesperrt, Entladen?). Bei "Automatisch": Fallback auf einen Einzelpack-Dienst | V | Fest auf Aggregator konfigurieren, damit Absturz → **Laden aus** (fail-safe); daemontools-Supervisor startet neu (≤ 10 s); Crash-Zähler, ab X Neustarts/h Alarm | 3 |
| F2 | Prozess hängt, Dienst bleibt registriert | Blockierender synchroner dbus-Aufruf auf toten serialbattery-Dienst (dbus-Default-Timeout 25 s), Deadlock, Endlosschleife | **Gefährlichster Fall:** Venus liest weiterhin die letzten CCL/DCL/CVL – unbegrenzt lange, ohne Fehler | M | Interner Watchdog: Hauptschleife setzt Heartbeat; separater Thread/Timer prüft → bei Ausbleiben > 2 Zyklen **Prozess hart beenden** (os._exit), damit Dienst verschwindet (F1-Pfad); nur asynchrone dbus-Zugriffe (Signale/VeDbusItemImport), keine sync GetValue in der Schleife; `/UpdateIndex` am eigenen Dienst hochzählen; externer Zweit-Watchdog (Cron/Script) prüft Heartbeat-Datei | 4 |
| F3 | Fallback auf Einzelpack | Controlling BMS = Automatisch, Aggregator weg | Bankstrom ≤ Einzelpack-Limit (stromseitig meist konservativ), aber CVL/Temperatur/Zellen der anderen zwei Packs **nicht** berücksichtigt → nur deren BMS schützt | V | Fallback bewusst vermeiden (F1); falls gewollt: dokumentieren, dass dann die JK-Schutzschwellen allein schützen und korrekt eingestellt sein müssen | 3 |
| F4 | Venus-Firmware-Update löscht Installation | rootfs wird ersetzt, nur `/data` bleibt; Service-Links/Abhängigkeiten weg | Aggregator fehlt nach Update → F1-Pfad (fail-safe wenn fest konfiguriert); oder inkompatible Venus-API → Absturzschleife | V | Installer unter `/data`, Reinstall über `/data/rc.local` (wie serialbattery); Versionspinning + Kompatibilitätscheck beim Start (Venus- und serialbattery-Version gegen Allowlist, sonst Start im **Safe-Mode**: CCL 0, DCL konservativ, Alarm); Abnahmetest nach jedem Update (T-Suite) | 3 |
| F5 | Beta-Software ändert Verhalten | Venus v3.70 beta, serialbattery 2.0 RC: Pfade/Semantik ändern sich | Stille Fehlinterpretation | M | Pfad-Schema beim Start validieren (erwartete Pfade vorhanden, Typen plausibel); Update nur mit Re-Abnahme | 3 |
| F6 | Fehlkonfiguration | Falsche Kapazität, falsche Pack-Anzahl, Tippfehler in Schwellwerten | Falsche Limits | A teilw. | Konfig-Schema-Validierung mit harten Grenzen; Schwellwerte außerhalb Sicherheitsbereich → Start verweigert; Konfig-Diff im Log | 3 |
| F7 | Cerbo-Reboot/Stromausfall | Wartung, Absturz | Während Boot keine DVCC-Limits; Multi läuft mit eigenen Grenzen | V | VE.Bus-Grundeinstellungen (Ladestrom, Abschaltspannungen) **eigenständig sicher** konfigurieren – unabhängig von DVCC; Startphase: erst nach N gültigen Zyklen Limits > 0 veröffentlichen | 3 |
| F8 | Aggregator schreibt in BMS/Einstellungen | Fehlbedienung, Feature-Creep | Unbeabsichtigtes Öffnen/Schließen von MOSFETs (E1/E2) | M | Aggregator ist **read-only** gegenüber BMS/serialbattery; jede Schreibfunktion verboten (Code-Review-Gate) | 4 |

---

## 6. Verbindliche Sicherheitsanforderungen (SR)

Muss-Anforderungen; jede hat mindestens einen Abnahmetest in Kap. 7.

| SR | Anforderung |
|----|-------------|
| SR-01 | **N-1:** Veröffentlichtes CCL und DCL halten für jeden Pack auch nach plötzlichem Wegfall des höchstbelasteten Packs dessen `limit_i` und seine BMS-OCP mit Marge ein (Formel Kap. 1). Gilt auch bei N=2. |
| SR-02 | **Monotonie bei Pack-Verlust:** Wegfall/Stale eines Packs darf CCL/DCL nie erhöhen. |
| SR-03 | **Konservative shares:** share_i = max(gefiltert, Kurzfenster-Max, Floor); Lernen nur bei |I_bank| ≥ Mindeststrom; schnell steigend, langsam fallend. |
| SR-04 | **Fail-safe-Eingänge:** Fehlender/ungültiger/veralteter/außerhalb-Bereich-Wert eines Packs ⇒ Pack gilt als weggefallen (für Lastverteilung) **und** als unbekannt-gefährlich (für Temperatur/Zellen: kein Laden ohne gültige Temperatur aller physisch verbundenen Packs). |
| SR-05 | **Ausgangsvalidierung:** Niemals NaN/None/Inf/negativ veröffentlichen. CCL, DCL ∈ [0, I_max_konfig]; CVL ∈ [CVL_min, min(Pack-CVL)]. Bei Validierungsfehler: CCL = 0, DCL = Notwert (konfigurierbar, klein), Alarm. |
| SR-06 | **CVL = min** der Pack-CVL; CCL-Taper nach max Zellspannung über alle Packs; DCL-Taper nach min Zellspannung über alle Packs. |
| SR-07 | **Temperatur:** CCL = 0, wenn irgendein Pack min-Temp < T_charge_min (Default 5 °C) oder Temperatur unbekannt; Taper bei Hitze nach max-Temp. JK-eigene Kälte-Ladesperre zusätzlich aktiv. |
| SR-08 | **AllowToCharge/AllowToDischarge** = logisches UND aller Packs; unbekannt = false. |
| SR-09 | **Rate-Limiting:** Limits sinken sofort, steigen höchstens mit konfigurierter Rampe; Hysterese an allen Schwellen. |
| SR-10 | **Watchdog:** Hängt die Hauptschleife > 2 Zyklen, beendet sich der Prozess selbst (Dienst verschwindet). Eigener `/UpdateIndex` wird pro Zyklus inkrementiert. Keine blockierenden dbus-Aufrufe in der Schleife. |
| SR-11 | **Fail-safe bei Aggregator-Ausfall:** Venus ist so konfiguriert, dass Ausfall/Fehlen des Aggregators zu **Ladesperre** führt (Controlling BMS fest), nicht zu stillem Fallback. Verhalten vor Ort nachgewiesen (T-F1). |
| SR-12 | **Identität:** Packs über BMS-Seriennummer identifiziert; Duplikate erkannt; erwartete Pack-Anzahl konfiguriert; Abweichung = Alarm + N-1-Rechnung mit tatsächlicher Anzahl. |
| SR-13 | **Zeit:** Nur monotone Uhr für Steuerlogik. |
| SR-14 | **Read-only:** Keine Schreibzugriffe auf BMS, serialbattery-Einstellungen oder MOSFETs. |
| SR-15 | **Reconnect-Überwachung:** ΔU je Pack zur Bank wird überwacht; Zuschalten außerhalb Freigabefenster → Alarm; nach Reconnect erhöhte Marge für T_reconnect. Kein automatisches Zuschalten. |
| SR-16 | **Persistenz/Updates:** Installation überlebt Firmware-Update via `/data`; Start prüft Versionen gegen Allowlist, sonst Safe-Mode. |
| SR-17 | **Safe-Mode-Definition:** CCL = 0, DCL = konservativer Festwert (≤ Einzelpack-Dauerstrom × Marge), CVL = konservativ, Alarm aktiv. |
| SR-18 | **Hardware (außerhalb Software, aber Voraussetzung für Inbetriebnahme):** Sicherung + Trenner je Pack; symmetrische Verkabelung; JK-Schutzparameter (OV, UV, OCP, OT, LT) je Pack dokumentiert und konsistent; VE.Bus-Grundeinstellungen eigenständig sicher. |
| SR-19 | **Logging:** Jede Limit-Änderung mit Grund (welcher Pack/Term war bindend) loggen; Ringpuffer für Post-Mortem nach Trip-Ereignissen (1 Hz, ≥ 24 h). |
| SR-20 | **Debug-Instrumentierung** hinter einem Schalter, nicht entfernen (Projektkonvention). |
| SR-21 | **Inbetriebnahme-Deckel:** Bis die shares gemessen sind (Kap. 8, Punkt 3), darf der Aggregator keine höheren Bank-Limits veröffentlichen als heute effektiv (DCL ≤ 59 A, CCL ≤ 171 A). Er darf sie zusätzlich aus den Pack-Limits und Zellspannungen weiter **absenken**. Der Deckel wird erst nach T-F7/T-F9 per Konfig angehoben. **Seit D6 (Regelkreis, `limit_control = pi`): gilt nur, solange eine Pack-Strommessung fehlt/veraltet ist; mit Live-Messung schützt der Regelkreis (02 §4.2a).** |
| SR-22 | **Kein Produktivbetrieb mit unkalibriertem SoC** als Schutzgröße. Solange `SocResetLastReached` nie erreicht wurde, wirken die Taper ausschließlich über Zellspannungen. |

---

## 7. Abnahmetests

### 7.1 Offline/Software (ohne Gerät, CI-Pflicht)

Reine Rechenkern-Tests mit simulierten dbus-Eingaben (Rechenkern muss dafür ohne dbus importierbar sein).

| Test | Prüft | Erwartung |
|------|-------|-----------|
| T-S01 | N-1-Formel mit Beispiel 0,35/0,33/0,32 × 150 A | 295 A ± Rundung; Normal-Limit wird nicht veröffentlicht |
| T-S02 | Pack-Wegfall (Dienst verschwindet, stale, Wert None) | CCL/DCL steigen nie (Property-Test über zufällige Szenarien) |
| T-S03 | share ≈ 0 / I_bank ≈ 0 / alle Ströme 0 | Keine Division-durch-0, Floor greift, kein NaN |
| T-S04 | NaN, None, Inf, Strings, negative Limits, mA statt A, Zellspannung 3450 statt 3,45 | Wert verworfen, Pack = unbekannt, Ausgabe valide |
| T-S05 | Vorzeichen: Laden und Entladen, gemischte Richtungen (Ausgleichsstrom: ein Pack lädt, einer entlädt) | Korrekte shares; Ausgleichsstrom erkannt und gemeldet |
| T-S06 | Temperatur: ein Pack 2 °C, andere 20 °C; ein Pack ohne Temperatur | CCL = 0 in beiden Fällen |
| T-S07 | Zell-OV nur in einem Pack | CCL-Taper greift, CVL = min |
| T-S08 | Uhrsprung ±1 h während Lauf | Kein Stale-Sturm, keine Rampen-Sprünge |
| T-S09 | Dienst-Umbenennung (ttyUSB0 → ttyUSB3), Duplikat gleicher Seriennummer | Pack einmal gezählt, Zuordnung stabil |
| T-S10 | Eigener Dienst in Discovery-Liste | Ausgeschlossen |
| T-S11 | Watchdog: Hauptschleife künstlich blockiert | Prozess beendet sich in < 3 Zyklen |
| T-S12 | Rampe: Limit-Sprung 0 → 300 A | Anstieg ≤ konfigurierte Rampe; Abfall sofort |
| T-S13 | Konfig außerhalb Grenzen / Versions-Allowlist verletzt | Start verweigert bzw. Safe-Mode |
| T-S14 | AllowToCharge eines Packs false/unbekannt | Bank AllowToCharge false |

### 7.2 Vor Ort (mit Owner, nur bei kleinen Strömen beginnen, jeweils mit Rückfallplan)

| Test | Durchführung | Erwartung / Abnahmekriterium |
|------|--------------|------------------------------|
| T-F1 | `kill -9` Aggregator bei ~20 A Laden | Venus zeigt BMS-Verlust; Laden stoppt in ≤ X s (messen); Supervisor startet neu; kein Fallback auf Einzelpack (oder dokumentiert) |
| T-F2 | `kill -STOP` Aggregator (Hängen simulieren) | Externer/interner Watchdog erkennt; Dienst verschwindet oder Alarm in ≤ X s. **Ohne Watchdog muss dieser Test fehlschlagen – Nachweis, dass das Risiko real ist** |
| T-F3 | RS485/UART eines BMS abziehen bei ~20 A | Pack nach T_stale als weggefallen; Limits sinken; Alarm "Pack blind" |
| T-F4 | serialbattery eines Packs neu starten (`svc -t`) | Kein Doppelpack, Zuordnung stabil, keine Limit-Spitze |
| T-F5 | Cerbo-Reboot | Keine Limits > 0 vor N gültigen Zyklen; danach Normalbetrieb |
| T-F6 | Simuliertes Firmware-Update (Reinstall-Pfad) bzw. echtes Update in Wartungsfenster | Reinstall funktioniert oder Safe-Mode/BMS-Verlust (fail-safe) |
| T-F7 | Pack-Wegfall unter Last: Trennschalter eines Packs bei **kleinem** Strom (≤ 30 A) öffnen, dann stufenweise höher bis maximal zum dokumentierten N-1-Limit | Übrige Packs bleiben unter limit_i; kein Folge-Trip; Aggregator-Limits sinken |
| T-F8 | Reconnect-Test: Pack mit definiertem ΔU (0,05 / 0,1 / 0,2 V) zuschalten, Ausgleichsstrom mit Zange messen | Ermittlung zulässiger ΔU-Schwelle für SR-15; Alarm bei Überschreitung |
| T-F9 | Tesla-11-kW-Start bei SoC ~50 % und ~15 % | Pack-Spitzenströme < limit_i; shares-Schätzung folgt; DCL-Reaktion dokumentiert |
| T-F10 | Lade-Endphase bis Zell-Taper | Kein Pack > Zell-OV-Schwelle; keine Oszillation (Limit-Plot) |
| T-F11 | Kälte (sofern saisonal möglich) bzw. Temperatursensor-Simulation über Konfig-Testmodus | CCL = 0 |

---

## 8. Vor Ort zuerst messen (Voraussetzung für Parametrierung)

Ohne diese Messungen sind alle Schwellen im Aggregator geraten. Reihenfolge = Priorität.

0. **Voraussetzung:** Owner gibt SSH/Root am Cerbo frei (heute nicht vorhanden). Bis dahin nur MQTT-Readback.
1. **Inventar & Konfiguration:** JK-Modelle/Firmware, Schutzparameter je Pack (OV/UV-Zelle, OCP Laden/Entladen inkl.
   **Verzögerungszeiten**, OT, LT-Ladesperre), eingestellte Nennkapazität je JK, serialbattery-Konfig (CVCM/CCCM/DCCM,
   max. Ströme, Poll-Intervall), Venus-Einstellungen (Battery Monitor, Controlling BMS, DVCC, ESS-Min-SoC), VE.Bus-
   Grundeinstellungen, alle Ladequellen (MPPT, AC-PV, externe Lader), Sicherungen und Kabelquerschnitte/-längen je Pack.
2. **dbus-Bestandsaufnahme:** vollständiger Pfad-Dump der drei serialbattery-Dienste (Vorhandensein `/UpdateIndex`,
   Seriennummer, `/Io/AllowTo*`, FET-Status, Zell- und Temperaturpfade, Vorzeichen), Update-Rate je Pfad.
3. **Stromaufteilung (shares):** je Pack Strom bei Bankstrom ≈ 0, ±20, ±50, ±100, ±200 A (Laden **und** Entladen),
   jeweils bei SoC ~20 %, ~50 %, ~90 %; zusätzlich **Zeitverlauf nach Lastsprung** (Tesla-Start) mit ≥ 1 Hz,
   besser schneller (Zangenamperemeter mit Logging, da BMS-Abtastrate ggf. zu langsam).
4. **Stromsensor-Offset:** je Pack Anzeige bei Ruhe (Bank stromlos bzw. Σ gegen Multi-DC-Strom/Referenzshunt);
   Abweichung Σ I_pack vs. Referenz bei mehreren Lastpunkten.
5. **Innenwiderstand je Pack:** aus Lastsprung ΔU/ΔI_pack (Pack-Klemmen, nicht Bank) bei 2 SoC-Punkten; zusätzlich
   Spannungsabfall Kabel/Sicherung/Schalter je Strang bei bekanntem Strom (mΩ-Ermittlung, Hotspot-Suche mit Wärmebild).
6. **BMS-Limit-Verhalten:** Verlauf der von serialbattery publizierten CCL/DCL/CVL über eine Vollladung und eine tiefe
   Entladung; Verhalten bei Zell-Balancing, bei OV-Annäherung, bei Temperaturänderung.
7. **Reaktionskette:** Zeit von CCL/DCL-Änderung am Aggregator bis Stromänderung am Multi/MPPT (Logging mit Zeitstempeln,
   mehrere Sprünge, auf- und abwärts).
8. **Reconnect-Verhalten:** JK-Precharge vorhanden? Ausgleichsstrom bei definiertem ΔU (T-F8); Verhalten nach JK-Trip
   (Auto-Reconnect-Zeit, Bedingungen).
9. **Venus-Fallback:** tatsächliches Verhalten bei Fehlen des Controlling BMS in v3.70 (T-F1) – Laden? Entladen? Alarm?
10. **Temperaturen:** Sensorposition je Pack, Temperaturdifferenz zwischen Packs über Tag/Jahreszeit (Kälte-Risiko).

---

## 9. Offene Entscheidungen für den Owner

1. **N-1 auch bei N=2?** Empfehlung: ja (sonst Kaskadenrisiko bei Tesla-Last); Konsequenz: mit 2 Packs nur
   Einzelpack-Leistung (~150 A ≈ 7 kW, Tesla wird ggf. vom Netz gedeckt).
2. **Veröffentlichter SoC:** kapazitätsgewichtet (mehr nutzbare Kapazität, Schutz über DCL-Taper) vs. min-Pack-SoC
   (konservativ). Empfehlung: gewichtet + Taper nach min-Pack-SoC/Zellspannung.
3. **Verhalten off-grid** (falls Notstromfunktion): DCL nicht durchsetzbar → maximale AC-Last am Multi auf N-1-Strom
   begrenzen (VE.Bus-Konfig) oder akzeptiertes Restrisiko dokumentieren.
4. **Fallback-Strategie bei Aggregator-Ausfall:** Ladesperre (empfohlen, fail-safe) vs. Fallback auf Einzelpack (Verfügbarkeit).
5. **Alternative prüfen:** existierender Community-Dienst (z. B. dbus-aggregate-batteries) als Referenz/Vergleich, um
   bekannte Fehlerbilder nicht neu zu erfinden – nicht ungeprüft übernehmen.
