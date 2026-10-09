# battery-aggregator – Szenarienkatalog (Konklave-Mitglied 2)

Stand: 2026-10-04 · Bezug: `02_DESIGN.md` (Formeln/§-Verweise), `00_FAKTEN_IST_ZUSTAND.md` (reale Werte),
`04_SICHERHEIT_FMEA.md` (N-1). Jede Zeile wird ein parametrisierter Core-Test bzw. Simulator-Lauf
(Szenario-ID = Test-ID). Zahlen sind Erwartungswerte des Cores **vor** Ratenbegrenzung, sofern nicht anders genannt.

## Gemeinsame Ausgangslage (reale Werte vom 04.10.)

| Pack | GX-Name | C | CCL | DCL | CVL | SoC (BMS) | Zellen |
|---|---|---|---|---|---|---|---|
| P1 | rechtsUnten | 305 Ah | 200 A | 43,6 A | 56,8 V | 8 % | 2,987–3,086 V |
| P2 | linksUnten | 314 Ah | 200 A | 92 A | 56,8 V | 1 % | ≈ 3,03 V |
| P3 | linksOben | 305 Ah | 200 A | 59 A | 56,8 V | 11–14 % | ≈ 3,03 V |

Annahmen, sofern nicht anders angegeben: geschätzte Anteile ŝ = 0,33 / 0,34 / 0,33,
konservativ s_eff = 0,35 / 0,36 / 0,35; Prior s_prior = 0,330 / 0,340 / 0,330;
`ccl_hw = dcl_hw = 300 A` (Beispiel, Owner legt fest); `ocp_hw = 250 A` je Pack (Beispiel);
`n1_mode = hw`; `v_cell_target = 3,45 V`; `dcl_emergency = 90 A`; Takt 1 s.
**Heute (Venus ohne Aggregator):** DCL 59 A (nur P3), CCL 171 A, SoC folgt P3.

Legende Testart: **U** = Core-Unit-Test, **P** = Property-Test, **S** = Simulator (closed loop),
**R** = Replay echter Daten, **L** = Live-Abnahme auf dem Cerbo.

---

## A. Normalbetrieb und Stromaufteilung (R2, R4)

**S01 – Normal laden, alle gesund.** Bank lädt mit 150 A.
Erwartet: CCL_model = min(200/0,35; 200/0,36; 200/0,35) = 555 A, Σ = 600 A → **CCL = 300 A** (ccl_hw).
LimitReason „Hardwaregrenze“. CVL = 56,8 V bzw. Zellregler (S11). Modus NORMAL, keine Alarme. Test U, S, L.

**S02 – Reale Entladegrenze (Kernproblem heute).** DCL 43,6 / 92 / 59 A.
Erwartet: DCL = min(124,6; 255,6; 168,6) = **124,6 A**, LimitReason „P1 43,6 A / s 0,35“.
Pro Pack bei 124,6 A Bank: P1 ≈ 41 A (≤ 43,6), P2 ≈ 42 A, P3 ≈ 41 A. N-1-hw-Grenze
min_k min_{i≠k} 250·(1−s_k)/s_i ≈ 451 A → nicht bindend.
Gegenprobe: `min(DCL_i)` = 43,6 A (zu streng), `Σ` = 194,6 A (P1 bekäme ≈ 66 A → Überlast), heute 59 A
(willkürlich). Test U, S, R (gegen Readback), L.

**S03 – Ein Pack drosselt Laden auf 20 A (Owner-Kernszenario R4).** P3 CCL 20 A, ŝ_P3 = 0,33.
Erwartet: **CCL = 20/0,35 ≈ 57 A** (nicht 20 A, nicht 420 A). P3 bekommt ≈ 19–20 A, P1/P2 je ≈ 19 A.
Absenkung im selben Takt. Test U, S.

**S04 – Drosselndes Pack ist voller.** Wie S03, aber gemessen ŝ_P3 = 0,15 (σ klein → s_eff 0,17).
Erwartet: **CCL ≈ 117 A**; mit weiter steigender Pack-Spannung von P3 fällt ŝ_P3, CCL steigt
rampenbegrenzt (+5 A/s, ≤ +10 %/10 s, 30 s Haltezeit nach letzter Absenkung). Test S.

**S05 – BMS P3 öffnet Lade-FET.** Nach S03 meldet P3 FET aus bzw. I_P3 < 1 A für ≥ 10 s bei Bankstrom ≥ 20 A.
Erwartet: P3 raus aus P_chg und aus CVL_follow; s renormiert ≈ 0,51/0,53 (+UCB) →
CCL_model ≈ 377 A, Σ 400 A → Ziel 300 A; Anstieg von 57 A auf 300 A über die Rampe (≈ 3 min).
`/System/NrOfModulesBlockingCharge = 1`. Test U, S.

**S06 – Summe begrenzt (hohe Limits).** Alle DCL 200 A (hypothetisch voll geladen).
Erwartet: DCL = min(555, 600, 300) = **300 A** (dcl_hw), nie mehr als Σ DCL_i. Test U, P.

**S07 – Vorzeichenwechsel Laden → Entladen (Wolke über PV).** Bank wechselt von +80 A auf −60 A.
Erwartet: getrennte Schätzer je Richtung; DCL sofort aus Entlade-Schätzer (S02-Werte), kein Reset,
keine Sprünge im CCL. Schätzer friert für |I_bank| < 15 A ein. Test U, S.

**S08 – Kreisstrom in Ruhe.** I_bank ≈ 0; P2 +12 A, P1 −6 A, P3 −6 A (Ausgleich nach Ladeende).
Erwartet: keine Schätzer-Aktualisierung (|I_bank| < 15 A); Offsets c_i werden bei nächster
gültiger Phase berücksichtigt; Rückkopplung prüft P2: 12 A < CCL_P2 → keine Aktion; Limits unverändert.
Test U, S.

**S09 – Kreisstrom überschreitet ein Pack-CCL bei entladender Bank.** P2 kalt, CCL_P2 = 5 A; Bank
entlädt 10 A; P2 nimmt 12 A auf.
Erwartet: Rückkopplung kann den Bank-CCL nicht wirksam senken (Bank entlädt) → Ereignis
„Kreisstrom > CCL P2“, Warnung `/Alarms/LowChargeTemperature` (vom BMS) bleibt sichtbar; keine
Fehlreaktion (z. B. DCL-Erhöhung). Ehrliche Grenze: Hardware-BMS schützt. Test S.

**S10 – Modellfehler: Pack überschreitet sein Limit trotz Modell.** P1 trägt real 0,45 statt 0,35
(Kabel-/Sicherungswiderstand anders); Bank 124,6 A → P1 = 56 A > 43,6 A.
Erwartet: nach 3 s `r_P1 = 1,28` → k_fb = 0,95/1,28 ≈ 0,74 → DCL ≈ 92 A; P1 ≈ 41 A. Schätzer lernt
ŝ_P1 → 0,45; k_fb erholt sich erst, wenn alle r_i < 0,9 seit 30 s. Kein Pendeln. Test S, P.

**S11 – Verrauschte Anteile.** ŝ_P1 springt pro Sample zwischen 0,2 und 0,5.
Erwartet: σ_P1 groß → s_eff_P1 bis ≈ 0,6 → DCL ≈ 73 A (konservativ), Ausgabe stabil durch
Haltezeit/Totband (≤ 1 Änderung je 30 s nach oben). Test P, S.

**S12 – s ≈ 0 bei geschlossenem Pfad (defekter Shunt meldet 0 A).** P2 meldet konstant 0,0 A, FET an.
Erwartet: s_eff_P2 = Floor 0,17 (nie 0 → nie Division durch 0, nie unbegrenzt); Warnung
„Strommessung P2 unplausibel“ (Σ-Konsistenz, ggf. gegen Referenz-Shunt). Test U, P.

## B. Spannung, Zellen, Temperatur

**S13 – Zelle bei 3,55 V in P3 während Absorption.** Bus 55,4 V, max. Zelle P3 3,55 V, sonst ≤ 3,42 V.
Erwartet: Zellregler CVL_cell = 55,4 − 16·0,10 = **53,8 V** (sofort, < serialbattery-CVL 56,8 V);
CCL_P3' = 200·30 % = 60 A → Bank-CCL ≈ 171 A; kein Hart-0. Wenn max. Zelle < 3,45 V: CVL steigt
0,05 V/10 s nach 60 s Haltezeit. LimitReason „Zellregler P3.Cxx 3,55 V“. Test U, S.

**S14 – Zelle ≥ 3,60 V (vgl. historische Spitzen 3,837 V P3, 3,689 V P1).** P3 Zelle 3,62 V, FET-Status bekannt „an“.
Erwartet: CCL_P3' = 0 → **Bank-CCL = 0** sofort, CVL_cell gesenkt. Sobald P3-Lade-FET offen
(BMS-OVP) → P3 aus P_chg und CVL_follow → Bank lädt P1/P2 per Rampe weiter. Test U, S.

**S15 – HighCellVoltage-Alarm = 2, FET-Status nicht verfügbar.**
Erwartet: CCL = 0; Strom-Evidenz unmöglich (Bank lädt nicht) → kein Deadlock-Trick, CCL bleibt 0,
bis Alarm zurück und max. Zelle < 3,50 V; dann Rampe. Dokumentierte Konservativität. Test U.

**S16 – Niedrige Temperatur.** P1 3 °C → serialbattery CCL_P1 = 30 A.
Erwartet: CCL = 30/0,35 ≈ 86 A. Bei P1 < 0 °C: CCL_P1 = 0 und FET an → **CCL = 0**; öffnet JK
LTP den Lade-FET → P1 raus, CCL = min(200/0,55; 200/0,56; Σ 400; 300) = 300 A (Rampe).
`/System/MinCellTemperature` + ID `P1.T1`. Test U, S.

**S17 – Hohe Temperatur beim Entladen.** P2 48 °C → DCL_P2 = 40 A.
Erwartet: DCL = min(124,6; 40/0,36 = 111) = **111 A**, LimitReason P2. `/Dc/0/Temperature` = 48 °C. Test U.

**S18 – Zell-Unterspannung (realer Wert).** Min. Zelle P1 2,987 V unter Last.
Erwartet: Taper DCL_P1' = 43,6·(2,987 − 2,90)/0,10 ≈ 37,9 A → **DCL ≈ 108 A**; bei 2,90 V → DCL_P1' = 0 →
DCL = 0, bis P1-Entlade-FET offen (dann P1 raus aus P_dchg, bleibt in P_chg). Test U, S, R.

## C. SoC

**S19 – Unkalibrierter SoC (realer Fall).** SoC 8 / 1 / 13 %, Ruhe, alle Zellmittel ≈ 3,03 V (ΔV < 15 mV).
Erwartet: SoC_w = (24,4 + 3,1 + 39,7) Ah / 924 Ah = **7,3 %**; SoC_min = 1 % (P2);
relative Plausibilität (ΔSoC 12 % bei ΔV < 15 mV) → `soc_suspect` für P1–P3, Status
„SoC-Drift, Kalibrier-Ladung empfohlen“; `/Soc = 7,3 %` (soc_mode weighted). Da ESS-Min-SoC = 0 %
(Owner): Schutz der Unterkante ausschließlich über S18-Taper + BMS-DCL → kein Pack läuft
unbemerkt leer. Test U, R.

**S20 – Kalibrier-Ladung.** Kalibrierungsalter > 14 d (hier: nie), PV-Überschuss.
Erwartet: CVL = 16·3,45 = 55,2 V, Absorption bis jedes Pack Tail < 0,02 C (≈ 6 A) oder max. 2 h;
erreicht ein Pack SoC 100 (serialbattery-Reset **[VERIFY: Reset-Bedingung]**) → `t_last_full`
persistiert, `soc_suspect` gelöscht. Danach CVL zurück auf Normalwert. Test S, L.

**S21 – Topologiewechsel glättet SoC.** P3 (13 %) wird GONE.
Erwartet: Roh-SoC springt von 7,3 % auf 4,4 %; `/Soc` sinkt mit max. 2 %/min, Rohwert unter
`/Custom/SocRaw`; `/InstalledCapacity` 924 → 619 Ah. Test U.

## D. Ausfall, Rückkehr, N-1 (R1, R5)

**S22 – N-1: P3 Kommunikationsausfall während Entladung 100 A.**
Erwartet: 0–10 s ACTIVE (Werte noch frisch) → 10–30 s STALE: letzte Werte eingefroren, Rampen
nach oben gesperrt → ab 30 s OFFLINE/HOLD: P1/P2 renormiert s ≈ 0,54/0,55 →
DCL = min(43,6/0,54 = 80,7; 92/0,55 = 167; 0,9·135,6 = 122) = **80,7 A**; CVL ≤ 56,8 V (letzter P3-Wert
gehalten); SoC rechnet mit letztem P3-Ah; Modus DEGRADED, `/Alarms/BmsCable = 1`,
`NrOfModulesOffline = 1`. Nach 10 min GONE (S21). Kein Prozessabsturz. Test U, S, L (USB ziehen bei moderater Last).

**S23 – N-1: P1 trippt hardwareseitig (Entlade-FET auf) bei 124,6 A Bank.**
Erwartet: sofort teilen sich 124,6 A auf P2/P3: P2 ≈ 68 A (< 92), P3 ≈ 67 A (> 59 weich, < ocp_hw 250 →
keine Kaskade). Innerhalb ≤ 3 s: P1 aus P_dchg (Strom-Evidenz/FET), DCL = min(92/0,55; 59/0,54) =
**109 A**; P1 bleibt in P_chg (Laden über Lade-FET möglich). Test S.

**S24 – N-1-Präventivgrenze greift (schwacher Restverbund).** Wie S02, aber ocp_hw P1 = 60 A.
Erwartet: N-1-hw-Grenze für Ausfall P3: 60·0,65/0,35 ≈ 111 A < 124,6 → **DCL = 111 A**,
LimitReason „N-1 P1 bei Ausfall P3“. Mit `n1_mode = soft`: 79,7 A. Test U.

**S25 – Eingefrorene Daten.** serialbattery publiziert P2 60 s lang bit-identische Werte (Treiber hängt).
Erwartet: STALE nach 60 s Gleichstand → weiter wie S22 (für P2). Test U, S.

**S26 – Rückkehr mit veralteten Daten.** P3 kommt zurück, liefert zunächst gecachte Werte (Spannung
2 V vom Bus-Median entfernt, SoC alt).
Erwartet: QUARANTINE, Plausibilitätsprüfung scheitert → keine Aufnahme; erst nach 30 s frischer,
plausibler Daten ACTIVE; Limits steigen nur über die Rampe; SoC-Übergang geglättet. Test U, S.

**S27 – Serieller Glitch, Flapping.** P1 wechselt alle 5–20 s online/offline.
Erwartet: P1 erreicht nie ACTIVE; Quarantäne 30 → 60 → … → 600 s; Bank bleibt stabil DEGRADED mit
P1-fehlt-Werten (DCL = min(92/0,55; 59/0,54; 0,9·151) = 109 A), CVL-Halt auf letztem P1-Wert;
Warnung „P1 instabil“; **keine** Limit-Oszillation im Takt des Flappings. Test S, P.

**S28 – Alle Packs offline (USB-Hub fällt aus).**
Erwartet: 10–30 s STALE-Halt; dann FAILSAFE: **CCL = 0 sofort**, DCL → `dcl_emergency` 90 A in 10 s,
CVL = 16·3,35 = 53,6 V, `/Alarms/BmsCable = 2`, `NrOfModulesOnline = 0`. Dienst bleibt registriert
(Venus fällt **nicht** auf ein Einzelpack zurück). Test U, S, L (nur mit Owner-Freigabe).

**S29 – Pack hinzugefügt (4. Pack).**
Erwartet: unbekannte Seriennummer → bei Whitelist: ignoriert + Hinweis; bei Auto-Modus: QUARANTINE 30 s,
Prior nach Kapazität (alle Anteile neu), `/InstalledCapacity` steigt, SoC-Slew, Limits nur per Rampe. Test U, S.

**S30 – Pack manuell abgetrennt (Trennschalter auf), Kommunikation ok.**
Erwartet: I ≈ 0 in beide Richtungen + Spannung > 0,5 V vom Bus-Median → „elektrisch getrennt“,
raus aus P_chg/P_dchg/CVL_follow, Warnung; Owner kann `EXCLUDED` setzen (dann SoC/Kapazität ohne das Pack). Test U.

## E. Plattform, Neustarts, Updates

**S31 – Venus-Neustart.**
Erwartet: INIT: CCL 0, DCL 90 A, CVL 53,6 V (Multi invertiert während Boot autark weiter); Packs
erscheinen nach 20–60 s, je 30 s Quarantäne, dann NORMAL mit Rampen; Schätzer aus `state.json`
(falls valide), sonst Prior. Test S, L. Reiner Dienst-Neustart mit frischem `state.json` (≤ 180 s):
Restart-Hold (Design §5.4a) hält die zuletzt publizierten CVL/CCL/DCL statt INIT-Werten, nur nach
unten geklemmt durch Live-Pack-Grenzen, max. 45 s. Test `test_restart_hold.py`.

**S32 – Aggregator-Absturz / kill -9.**
Erwartet: Dienst verschwindet, daemontools startet in ≤ 5 s neu → INIT. Da BmsInstance explizit auf
das Aggregat zeigt, wählt Venus kein Einzelpack **[VERIFY: DVCC-Verhalten in der Lücke]**. Test L.

**S33 – Exception im Core (Programmierfehler).**
Erwartet: FAULT: letzte Ausgaben, CCL sofort halbiert, nach 10 s FAILSAFE-Werte, Traceback im Log;
nach 60 s ohne Erholung Watchdog-Exit → Neustart. Mainloop läuft weiter. Test U (Fehlerinjektion).

**S34 – dbus-serialbattery-Update ändert Pfade.** `/Info/MaxDischargeCurrent` fehlt bei P2.
Erwartet: P2 UNTRUSTED (wie offline, elektrisch vorhanden) → Werte wie S22 für P2; Log nennt fehlenden
Pfad; Vertragstest hätte das vor dem Update gemeldet. Test U, Vertragstest.

**S35 – ttyUSB-Tausch nach Reboot.** P1 erscheint als ttyUSB3, P2 als ttyUSB2.
Erwartet: Zuordnung per Seriennummer → Labels, Kapazitäten, Schätzer und Kalibrierdaten unverändert. Test U.

**S36 – Müllwerte.** SoC 255, Zelle 0,000 V, Strom NaN, Kapazität −1.
Erwartet: keine Exception; Pack UNTRUSTED (sicherheitsrelevante Felder); Anzeige-Felder ignoriert. Test P.

**S37 – Firmwareupdate setzt Systemeinstellungen zurück.** BmsInstance wieder „automatisch“ (→ P3).
Erwartet: Startprüfung erkennt Abweichung → Log; `/Alarms/InternalFailure = 1` nur wenn keine der beiden
Einstellungen auf das Aggregat zeigt (halbe Auswahl = nur Warnung im Log, 0.2.1); nur bei
`enforce_system_settings = true` automatische Korrektur. Dienst überlebt das Update via `/data/rc.local`. Test L.

**S38 – Verwaiste Instanzen / MultipleBatteries.** battery/0 „Alle Batterien“ verwaist, Alarm MultipleBatteries = 1.
Erwartet: Aggregat nutzt Instanz 512 (kollisionsfrei); keine automatische Bereinigung (Owner-Schritt);
Status listet verwaiste Instanzen. Ob „MultipleBatteries“ bleibt **[VERIFY]**. Test L.

**S39 – Großer Lastsprung (Wallbox 11 kW ≈ 240 A DC) bei DCL 124,6 A.**
Erwartet ESS netzparallel: Multi begrenzt Batterie auf 124,6 A, Rest aus dem Netz. Insel/Netzausfall:
DCL nicht durchsetzbar → P1 ≈ 84 A > 43,6 weich, Schutz nur durch BMS-OCP/Sicherung; Warnung
„DCL überschritten“. Dokumentierte Systemgrenze (vgl. FMEA). Test S.

**S40 – Limit-Flattern eines Packs.** P3 CCL wechselt alle 2 s 20 ↔ 200 A (serialbattery-Hysterese).
Erwartet: Bank-CCL fällt sofort auf 57 A und bleibt dort (30 s Haltezeit wird von jeder Absenkung
neu gestartet) – keine Oszillation am Multi. Test P, S.

## F. Geschlossener Regelkreis je Pack-Limit (Owner-Anforderung, `tests/test_closed_loop.py`)

Simulator `tools/control_sim.py`: 3 × 280 Ah, 16s LFP, 15 mΩ, Nachfrage 200 A; Zahlen in 02 §4.2a.

**S41 – Regelgesetz mit den Owner-Zahlen.** 110 A erlaubt, P3 erreicht 15 A bei 10 A Limit (u = 1,5).
Erwartet: sofort 55 A (Spielraum halbiert); der nächste, noch verzögerte Messwert schneidet nicht erneut
(Hochrechnung auf das neue Limit: 0,75); fließt danach immer noch zu viel, zweiter Schnitt. Anheben nur
bei bindendem Limit (Anti-Windup), ohne Live-Messung kein Gewinn über das Modell. Test U.

**S42 – Owner-Szenario 50/50/10 A.** P3 fast voll (höhere OCV, nimmt weniger als 1/3).
Erwartet: P3 nie über 10 A, Bank eingeschwungen 40–60 A (Sim 48,5 A; alt 25,7 A), Restwelligkeit
< 2 A. Variante *owner_step*: alle Packs 50 A bei ≈ 133 A Bankstrom, dann meldet P3 10 A, trägt aber
≈ 38 A → spätestens nach 2 Takten unter dem Limit, danach Wiederanstieg auf ≈ 50 A, nie zurück zu Σ.
Test S.

**S43 – P3 nimmt mehr als seinen Anteil** (0,48 statt geschätzt 1/3). Erwartet: ≤ 3 Takte über dem
Limit, eingeschwungen ≤ 10/0,48 A. Test S.

**S44 – CCL-Taper in Stufen 50 → 40 → 30 → 20 → 10 A.** Erwartet: je Stufe höchstens 1 Takt über dem
Limit (Vorsteuerung `out = Anker · r`), eingeschwungen > 45 A (alt < 30 A). Test S.

**S45 – Entladen, P3 fast leer, DCL 50/50/10 A.** Erwartet wie S42 in Entladerichtung. Test S.

**S46 – Rauschen und Verzug.** 1 A Stromrauschen, 2 s Messlag, 3 s Multi-Lag, Seeds 1–3. Erwartet:
≤ 2 s mehr als 5 % über dem Limit, Bank > 40 A; hungrige Variante ≤ 7 Takte über 5 % (Totzeit ≈ 5 s).
Test S.

**S47 – Pack verschwindet mitten in der Regelung** (P2 abgesteckt, D-Bus-Dienst weg). Erwartet: kein
Absturz, Regler-Reset, P3 ≤ 3 Takte über dem Limit; solange P2 unsicher ist, gilt nur das Modell (keine
Verstärkung ohne Messung); nach GONE regelt der Kreis auf 2 Packs weiter (20–40 A). Test S.

**S48 – Pack erscheint (dynamische Erkennung).** (a) P3 wird bei ≈ 99 A Bankstrom eingesteckt:
1 Takt Einschaltstrom, dann Schutz von P3 und Wiederanstieg > 40 A. (b) Ohne Packliste und ohne
Packzahl: Packs werden per Seriennummer gefunden, P3 kommt zur Laufzeit dazu; ohne konfigurierte
JK-OCP rechnet N-1 auf BMS-Limits (D4, konservativ). Test S.

**S49 – Vorsichtige Anteile nicht doppelt zählen.** Gelernte Anteile 0,495/0,51/0,495 (Σ ≈ 1,5),
200 A je Pack, Busbar 1000 A, Wechselrichter 210 A, `measured_shares_verified = false`. Erwartet: Laden
erreicht 210 A, Entladen > 390 A (N-1 mit normierten Anteilen), kein Pack über 200 A, kein 59-A-Deckel
bei Live-Messung. Test S.


---

## Abdeckungsmatrix

| Anforderung | Szenarien |
|---|---|
| R1 dynamisch zusammenführen | S22, S26, S27, S29, S30, S35, S47, S48 |
| R2 Summe der Ströme | S01, S05, S06 |
| R3 Min/Max-Zellen mit ID | S13, S14, S16, S18 |
| R4 Pack-Limit nicht auf ganze Bank | S02, S03, S04, S05, S16, S17, S23, S41–S46 |
| Pack-Limit nie lange überschritten, Rest genutzt (Regelkreis) | S41–S48 |
| R5 kein Absturz | S22, S25, S28, S32, S33, S34, S36 |
| N-1 (FMEA) | S22, S23, S24, S27 |
| SoC unkalibriert | S19, S20, S21 |
