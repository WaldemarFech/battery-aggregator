# battery-aggregator – Design (Konklave-Mitglied 2: Design & Szenarien)

Stand: 2026-10-04 · Status: ENTWURF zur Konklave-Review · Reines Design, kein Gerätezugriff.
Alle Aussagen über Venus-OS- bzw. dbus-serialbattery-Interna, die mit **[VERIFY]** markiert sind,
müssen vor der Implementierung auf dem Cerbo (v3.70~61) belegt werden.

---

## 1. Problem und Ziel

**Ist-Zustand:** Cerbo GX mit DVCC, drei LiFePO4-48-V-Packs (A ≈ 314 Ah, B ≈ 305 Ah, C ≈ 305 Ah)
parallel an einem DC-Bus, je ein JK-BMS über dbus-serialbattery 2.0 RC → drei Dienste
`com.victronenergy.battery.ttyUSBx`. Venus nutzt genau **einen** Batteriedienst als
„Controlling BMS“ und Batteriemonitor. Folge: CVL/CCL/DCL und SoC folgen einem Pack, die anderen
wurden bis ~0–1 % entladen.

**Ziel:** Ein Dienst `com.victronenergy.battery.aggregate` bildet aus allen erreichbaren Packs eine
virtuelle Batterie, die Venus als Controlling BMS **und** Batteriemonitor nutzt.

**Owner-Anforderungen (Abnahmekriterien):**

| ID | Anforderung | Umsetzung (Kapitel) |
|----|-------------|---------------------|
| R1 | Packs dynamisch erkennen und automatisch zusammenführen, auch wenn einer ausfällt | §5 Discovery & Lebenszyklus |
| R2 | Summe der Maximalströme | §4.2 (Summe als Obergrenze, korrigiert um reale Stromaufteilung) |
| R3 | Min/Max-Zellspannung über alle Packs (mit Pack-ID) | §4.5 |
| R4 | Limit eines Packs (z. B. 20 A) darf **nicht** die ganze Bank auf 20 A drosseln, sondern nur das betroffene Pack schützen | §4.2 Aufteilungsmodell (Kernstück) |
| R5 | Darf nicht abstürzen, wenn ein Pack offline geht | §6 Robustheit |

**Reale Ausgangswerte (aus `00_FAKTEN_IST_ZUSTAND.md`, Readback 04.10.):** Pack 1 (305 Ah) DCL 43,6 A,
Pack 2 (314 Ah) DCL 92 A, Pack 3 (305 Ah) DCL 59 A; CCL je 200 A; CVL je 56,8 V (= 3,55 V/Zelle);
Venus wählt automatisch Pack 3 → Bank-DCL 59 A, Bank-CCL 171 A; SoC 8 / 1 / 11–14 %, nie kalibriert;
historische Zellspitzen 3,837 V (P3) und 3,689 V (P1); ESS-Min-SoC 0 % (Owner: so lassen);
Alarm „MultipleBatteries“; verwaister Dienst battery/0 „Alle Batterien“; kein SSH.
**Lesart:** Bei 59 A Bank trägt Pack 1 nur ≈ 20 A (< 43,6 A) – aktuell also *keine* Überlast, sondern
eine willkürliche Bankgrenze: Hätte Pack 3 200 A gemeldet, wäre Pack 1 mit ≈ 66 A überfahren worden.
Das Modell aus §4.2 ergibt 125 A (§4.2-Beispiel) – sicher für alle drei. Die Zellspitzen > 3,6 V
zeigen, dass ein Pack-CVL von 3,55 V/Zelle ohne Zellregler (§4.3) nicht reicht.

---

## 2. Physikalische Grundwahrheit (warum Mitteln/Summieren falsch ist)

1. **Gemeinsame Busspannung.** Alle Packs sehen dieselbe Klemmenspannung (± Kabelabfall). CVL ist
   daher zwangsläufig eine *Bank*-Größe – es gibt keinen „Pro-Pack-CVL“.
2. **Strom teilt sich nach Physik, nicht nach Wunsch.** Ohne DC/DC-Wandler pro Pack kann niemand den
   Strom eines einzelnen Packs steuern. Für Pack *i* gilt näherungsweise
   `I_i = (V_bus − OCV_i(SoC_i)) / R_i`. Der Anteil hängt also von Innenwiderstand, Kabel/Sicherung,
   SoC und Temperatur ab – nicht von der Kapazität allein.
3. **Konsequenz für R4:** Man kann ein einzelnes Pack nur schützen, indem man den **Bankstrom** so
   wählt, dass dessen Anteil unter seinem Limit bleibt. Die richtige Antwort ist weder
   `min(CCL_i)` (= 20 A, die beklagte Fehlfunktion der bestehenden Aggregatoren) noch `Σ CCL_i`
   (= 320 A, würde das gedrosselte Pack mit ~100 A überfahren), sondern
   **der größte Bankstrom, bei dem jedes Pack innerhalb seines Limits bleibt**.
4. **Selbstkorrektur am oberen Ende:** Ein Pack, das wegen hoher Zellspannung drosselt, ist meist
   voller → höhere OCV → sinkender Stromanteil → die Bankgrenze steigt von selbst wieder. Das Modell
   nutzt diesen Effekt, statt ihn zu bekämpfen.
5. **Ausgleichsströme:** Bei Bankstrom ≈ 0 fließen zwischen ungleich geladenen Packs Kreisströme
   (A +12 A, B −12 A). Ein Pack kann laden, während die Bank entlädt. Darum wird pro Pack ein
   Offset modelliert (§4.2).
6. **Kommunikationsverlust ≠ elektrisch weg.** Fällt die serielle Verbindung aus, hängt das Pack in
   der Regel weiter am Bus, und sein JK-BMS schützt die Zellen weiter **lokal** (Hardware-Schutz ist
   von dbus unabhängig). Das Design behandelt „offline“ daher als „Zustand unbekannt“, nicht als
   „Pack nicht vorhanden“.
7. **SoC-Divergenz ist meist Zähler-Drift.** Parallel verbundene LiFePO4-Packs gleichen ihre
   Spannung ständig aus. Echte SoC-Unterschiede von 1 % vs. 14 % sind im flachen Kennlinienbereich
   kaum möglich, ohne dass die Spannungen auseinanderlaufen. Ein 1-%-Pack mit 3,25 V Zellspannung im
   Ruhezustand hat sehr wahrscheinlich einen falsch kalibrierten Coulomb-Zähler (JK setzt SoC nur bei
   Vollladung auf 100 %). Siehe §4.4.

---

## 3. Architektur

```
            ┌──────────────────────── Cerbo GX (Venus OS) ─────────────────────────┐
 JK-BMS A ─▶│ dbus-serialbattery ─▶ com.victronenergy.battery.ttyUSB0 ──┐          │
 JK-BMS B ─▶│ dbus-serialbattery ─▶ com.victronenergy.battery.ttyUSB1 ──┤          │
 JK-BMS C ─▶│ dbus-serialbattery ─▶ com.victronenergy.battery.ttyUSB2 ──┤          │
            │                                                           ▼          │
            │   ┌───────────── battery-aggregator (1 Prozess) ─────────────────┐   │
            │   │ D-Bus-Adapter (dünn)   ── Snapshots ──▶  Core (reines Python) │   │
            │   │  · NameOwnerChanged    ◀── Outputs ───   · Pack-Zustandsautomat│   │
            │   │  · ItemsChanged-Abo                      · Aufteilungsmodell   │   │
            │   │  · VeDbusService publish                 · Limits/CVL/SoC/Alarm│   │
            │   │ Supervisor + Watchdog  · Persistenz /data (Schätzer, Kalibr.)  │   │
            │   └──────────────────────────────┬────────────────────────────────┘   │
            │                                  ▼                                    │
            │      com.victronenergy.battery.aggregate  ──▶ systemcalc / DVCC ──▶ Multi, MPPTs │
            └───────────────────────────────────────────────────────────────────────┘
```

**Schichtregel:** Der Core ist eine reine Funktion
`step(state, inputs: dict[pack_id, PackSnapshot], now) -> (state', BankOutputs, Events)` ohne
D-Bus, ohne Uhrzugriff, ohne I/O. Der Adapter liefert Snapshots und publiziert Outputs. Damit ist
der Core vollständig auf dem PC testbar (§8) und SSOT für alle Rechenregeln.

**Takt:** Core-Schritt alle 1 s (GLib-Timer). serialbattery pollt ~1 s; ESS regelt den Batteriestrom
auf Basis des Monitor-Stroms → `/Dc/0/Current` muss aktuell sein (max. 1 Takt Latenz).

**Laufzeit:** Ein Prozess, ein GLib-Mainloop, kein Threading (Venus-velib-Idiom). Python 3 der
Firmware, **keine pip-Abhängigkeiten**; velib_python wird mitgeliefert (vendored).

---

## 4. Rechenmodell (Core)

### 4.1 Eingaben pro Pack (PackSnapshot)

Aus dem jeweiligen serialbattery-Dienst (Pfadnamen Venus-Standard, Mapping im Adapter):
`/Dc/0/Voltage`, `/Dc/0/Current`, `/Dc/0/Temperature`, `/Soc`, `/Capacity`, `/InstalledCapacity`,
`/ConsumedAmphours`, `/Info/MaxChargeVoltage`, `/Info/MaxChargeCurrent`,
`/Info/MaxDischargeCurrent`, `/Io/AllowToCharge`, `/Io/AllowToDischarge`,
`/System/MinCellVoltage`, `/System/MaxCellVoltage`, `/System/Min/MaxVoltageCellId`,
`/System/Min/MaxCellTemperature`, `/Alarms/*`, `/Connected`, `/Serial` bzw. BMS-Seriennummer,
`/System/NrOfCellsPerBattery`, sowie – falls exportiert – FET-Zustände Laden/Entladen **[VERIFY:
genaue Pfade in serialbattery 2.0 RC]**. Jeder Wert trägt einen Empfangszeitstempel (Adapter).

**Feldvalidierung** (an der Systemgrenze): Typ, endlich (kein NaN/Inf/None), Plausibilitätsbereich
(z. B. Zellspannung 2,0–4,0 V, SoC 0–100, Strom |I| < 1000 A, Temperatur −40…+90 °C). Ungültige
*sicherheitsrelevante* Felder (Limits, Zellspannungen, Strom) → Pack `UNTRUSTED` (§5); ungültige
Anzeige-Felder → Feld wird ignoriert, Warnung.

### 4.2 Stromlimits: Aufteilungsmodell (Kern von R4)

**Modell pro Pack und Richtung** (Laden/Entladen getrennt geschätzt, da R und OCV-Steigung je
Richtung verschieden sind):

```
I_i ≈ s_i · I_bank + c_i          (s_i = Stromanteil, c_i = Kreisstrom-Offset)
I_bank = Σ I_j über alle elektrisch aktiven Packs
```

**Schätzung:** Rekursive Kleinste-Quadrate-Regression (RLS) von `I_i` auf `I_bank` mit
Vergessensfaktor (Zeitkonstante ~10 min). Nur Stichproben verwenden, wenn
- `|I_bank| ≥ I_est_min` (Default 15 A) – darunter ist der Anteil Rauschen/Kreisstrom,
- der Bankstrom quasi-stationär ist (`|dI_bank/dt| < 5 A/s`, Pack-Stichproben ≤ 1,5 s auseinander),
- alle Packs aktiv sind (kein Topologiewechsel im Fenster).
Danach Normierung `Σ s_i = 1`, `Σ c_i = 0` (Kirchhoff). Liefert zusätzlich eine Varianz `σ_i`.

**Konservativer Anteil** (Unterschätzen von s_i wäre gefährlich, Überschätzen nur ineffizient):

```
s_prior_i = C_i / Σ C_j                    (Kapazitätsanteil, Fallback)
s_eff_i   = clamp( ŝ_i + k·σ_i ,  s_floor_i , 1 )      k = 2
s_floor_i = 0,5 · s_prior_i                (solange Ladepfad des Packs geschlossen ist)
ohne gültige Schätzung: s_eff_i = min(1, 1,5 · s_prior_i)
```

**Bankgrenze Laden** über die Menge `P_chg` der Packs, deren Ladepfad geschlossen ist (FET an bzw.
Strom-Evidenz, §4.6):

```
CCL_model = min_{i∈P_chg}  max(0, CCL_i − c_i⁺) / s_eff_i        (c_i⁺ = max(c_i,0) bei Laden)
CCL_raw   = min( CCL_model ,  Σ_{i∈P_chg} CCL_i ,  CCL_hw )        → Rampe (§4.7) → Anker
CCL_pub   = Regelkreis(Anker, Decke, Boden, gemessene Auslastung)    (§4.2a)
```

`CCL_hw` = konfigurierte Hardwaregrenze (Kabel, Sicherungen, Busbar). Das offene Modell ist nur der
**Anker/Startwert**; was publiziert wird, entscheidet der geschlossene Regelkreis aus §4.2a.
Entladen analog mit `DCL_i`, `P_dchg`, Offset `c_i⁻`.

**N-1-Kaskadenschutz (Abgleich mit `04_SICHERHEIT_FMEA.md`):** Trippt Pack k plötzlich, steigen die
übrigen Anteile auf `s_i/(1−s_k)`, bis der nächste Takt (1–3 s inkl. Multi-Reaktion) nachregelt.
Präventiv wird darum gegen die **Hardware-Abschaltgrenze** (JK-OCP, `ocp_hw_i`, Konfig) gerechnet,
nicht gegen die weiche serialbattery-Grenze: `I_bank ≤ min_k min_{i≠k} ocp_hw_i·(1−s_k)/s_eff_i`.
Option `n1_mode = soft` rechnet dieselbe Formel mit CCL_i/DCL_i (strenger: reale DCL → 79,7 A statt
124,6 A). Default `hw` – verhindert die Kaskade, ohne die Bank dauerhaft auf weiche Limits zu drosseln;
die weiche Überschreitung nach einem Trip dauert ≤ 3 s und wird von der Rückkopplung abgefangen.

**Rückkopplung alt (`limit_control = "legacy"`, nur noch zum Vergleich/Rollback):** Jede Sekunde
`r_i = I_i / CCL_i` für Packs im Laden. Ist `r_i > 1,05` für ≥ 3 s:
`k_fb ← k_fb · 0,95 / r_i` (sofort). Erholung: `k_fb ← min(1, k_fb + 0,01/s)` erst, wenn alle
`r_i < 0,9` seit ≥ 30 s. Anti-Windup: `k_fb ∈ [0,05; 1]`. Dieselbe Prüfung gilt **auch während die
Bank entlädt** (Kreisstrom kann ein Pack laden) – dann wird nur der Rückkopplungspfad aktiv,
und bei Bank-Entladung < 20 A wird zusätzlich ein Ereignis „Kreisstrom überschreitet CCL“ geloggt.

### 4.2a Geschlossener Regelkreis je Pack-Limit (`limit_control = "pi"`, Default, `control.py`)

**Owner-Anforderung** (die Idee der linearen Pack-Limit-Reduktion stammt aus seinem
dbus-serialbattery-Beitrag): P1 und P2 melden je 50 A, P3 „fast voll, max. 10 A“. Σ = 110 A wäre
unsicher (P3 bekäme ≈ 30 A), min × N = 30 A verschenkt die anderen Packs. Fließen bei erlaubten 110 A
z. B. 15 A in P3, muss **drastisch** reduziert werden (z. B. auf 55 A), dann prüfen: fließt noch
> 10 A → weiter runter, sonst langsam wieder hoch. Ein Pack-Limit darf nie längere Zeit überschritten
werden, und die Bank soll die Kapazität der übrigen Packs so weit wie möglich nutzen.

**Warum das offene Modell allein nicht reicht** (Simulator `tools/control_sim.py`, 3 × 280 Ah,
15 mΩ, Nachfrage 200 A, Start mit veralteter Schätzung 1/3 je Pack): Ein Pack am oberen Knick hat
höhere OCV und nimmt **weniger** als seinen Anteil (`I_3 ≈ I/3 − 7 A`), ein niederohmiges Pack nimmt
**mehr**. Die Anteilsschätzung lernt das langsam (`τ_down` 600 s, Kurzfenster-Maximum, Boden 0,8/N),
und `k_fb ≤ 1` kann nie über das Modell hinaus.

| Szenario (600 s) | alt: P3 > Limit | alt: Bank | neu: P3 > Limit | neu: Bank |
|---|---|---|---|---|
| owner – P3 voller, 50/50/10 A | 0 s (u_max 0,29) | **25,7 A** | 0 s (u_max 0,95) | **48,5 A** |
| owner_step – alle 50 A, dann P3 → 10 A | 1 s (Stufe) | 26,5 A | 1 s (Stufe) | 50,2 A |
| hungry – P3 nimmt 0,48 statt 1/3 | 3 s, 11,1 As | 19,1 A | 2 s, 7,4 As | 20,6 A |
| taper – P3 CCL 50→40→30→20→10 A | 3 s | 28,3 A | 3 × 1 s (je Stufe) | 53,5 A |
| noisy – owner + 1 A Rauschen, 2 s Messlag, 3 s Multi-Lag | 0 s | 18,8 A | 6 s, max. 3 % über | 46,6 A |
| noisy_h – hungry + Rauschen/Lags | 7 s, 25,8 As | 16,0 A | 6 s, 22,1 As | 19,2 A |
| discharge – P3 fast leer, DCL 50/50/10 A | 0 s | 26,0 A | 0 s | 50,5 A |

Alt ist im Owner-Fall **zu konservativ** (knapp die Hälfte der möglichen Bankleistung verschenkt, P3
bei 29 % Auslastung) und bei falschem Modell **zu langsam** (3 s Trip-Zeit + multiplikatives k). Die
1-s-Überschreitung bei einer Limit-Stufe ist physikalisch (P3 trägt im Moment der Stufe noch den alten
Strom, der Multi reagiert frühestens einen Takt später); sie wird im selben Takt per Vorsteuerung
beantwortet. Reproduzieren: `python -m battery_aggregator.tools.control_sim` (in `src/`).

**Regelgesetz je Richtung** (Zustand `r = out / Anker`, also die Korrektur des offenen Modells):

```
u_i  = I_i / L_i                         gemessene Auslastung je Pack (Richtung d)
u    = max_i u_i                         schlechtestes Pack
u_p  = u · out / I_bank                  auf das kommandierte Limit hochgerechnet (gemessene Anteile;
                                         korrigiert Mess- und Multi-Verzug nach einem Schnitt)
Anker = Rampe(CCL_raw)                   offenes Modell inkl. SR-09-Rampe; Vorsteuerung out = Anker · r:
                                         eine Limit-Stufe wirkt im selben Takt
Decke = min(Σ L_i, N-1 mit Kirchhoff-normierten Anteilen, CCL_hw/Wechselrichter, Blind-Sperre)
        – nur wenn alle Mitglieder live und frisch (≤ 3 s) gemessen sind, sonst Decke = Anker
Boden = min(min_i (L_i − c_i), Anker)    Σ-sicher: selbst wenn ein Pack alles nimmt, bleibt es im Limit

Schnitt  Median aus 3 u_p > 1,02 (oder ein Wert ≥ 1,3 bei echtem u > 1,02):
         out ← Boden + (out − Boden) · max(0,25; 1 − g·(u_p − 1)),  höchstens out · 0,95 / u_p
         → 15 A bei 10 A (u = 1,5) halbiert den Spielraum sofort (110 → 55 A)
Trimmen  0,95 < u_p ≤ 1,02: PI in Geschwindigkeitsform abwärts, nicht ratenbegrenzt
Anheben  u_p < 0,95: PI in Geschwindigkeitsform, NUR wenn das Limit bindet
         (0,9·out ≤ I_bank ≤ 1,05·out + 1 A), 5 s nach Schnitt bzw. 30 s nach Reset gesperrt,
         begrenzt auf 2 A/s und 2 %/s von max(out, 50 A)
         Δout = G·(k_p·Δe + k_i·e·Δt),  e = 0,95 − ū_p (Tiefpass τ 3 s),  G = out / ū_p
```

- **Anti-Windup:** Geschwindigkeitsform (kein separater Integrator) und Anheben nur bei bindendem,
  eingehaltenem Limit (darunter sagt die Messung nichts über höhere Ströme, bei Überlauf S39 auch
  nicht). `out` ist immer auf [Boden, Decke] begrenzt.
- **Ohne Gegenbeweis folgt `out` dem Anker nach oben** (kein Schnitt → `r ≥ 1`). Schnitt-Evidenz gilt
  300 s; im Leerlauf (I < 2 A) driftet `r < 1` nach 30 s mit Anhebe-Rate zurück auf 1, und ein Gewinn
  über das Modell (`r > 1`) verfällt nach 30 s ohne Strom.
- **Reset bei Mitgliedswechsel** (Pack kommt/geht, Rolle aktiv ↔ unsicher): Messhistorie verworfen,
  `r ← min(r, 1)`, Anheben 30 s gesperrt – nie ein Sprung nach oben. Fehlt einem Mitglied der aktuelle
  Messwert (> 1,5 s), wird nicht hochgerechnet (der Bankstrom enthielte gehaltene Werte).
- **Kreisstrom** (Bank fließt nicht in Richtung d, Pack trotzdem über Limit): Bank-Limit senken hilft
  nicht → nur Warnung (S09).
- **Fehler-Wrapper/Ausgabevalidierung** klemmen Rampe und Regler (`clamp_published`).
- **SR-21 neu (Live-Befund Cerbo, D6):** der statische Inbetriebnahme-Deckel (DCL 59 A) gilt im
  Regelkreis-Modus nur noch, solange eine Pack-Strommessung fehlt oder veraltet ist (> 3 s, unsicheres
  Mitglied, STALE). Mit Live-Messung schützt die gemessene Auslastung jedes Pack; Vorsteuerung = BMS-Limits.
  `limit_control = "legacy"` behält den Deckel bis `measured_shares_verified`.
- **Keine doppelte Vorsicht:** die gelernten Anteile sind absichtlich hoch (Live: 0,495/0,51/0,495,
  Σ ≈ 1,5). Sie bestimmen nur den Anker (sicherer Startwert). Die Decke rechnet N-1 mit
  `s_i / Σ s` – sonst hieße N-1 „ein Pack trägt alles“ (200 A) und deckelte die Bank unnötig (S49).
- **Neustart:** CVL startet beim ersten Schritt mit zugelassenen Packs auf dem effektiven CVL-Ziel
  (Pack-CVL, Zellregler, 3,55-V-Grenze), nicht bei 53,6 V; CCL/DCL bei Live-Messung auf dem Ziel.
- Unverändert: B1/M1–M6, Wechselrichter-Deckel 210 A, Temperatur-Taper, CVL-Regler, N-1 im Anker.

**Tuning** (`config.py`, Grenzen in `validate()`):

| Parameter | Default | Wirkung / wann ändern |
|---|---|---|
| `ctl_setpoint` | 0,95 | Ziel-Auslastung des schwächsten Packs; bei starkem Messrauschen 0,90 |
| `ctl_cut_ratio` / `ctl_cut_now_ratio` | 1,02 / 1,30 | Schnittschwelle (Median-3) / Sofortschnitt bei einem Messwert |
| `ctl_cut_gain` / `ctl_cut_min_factor` | 1,0 / 0,25 | Schnitttiefe `1 − g·(u−1)`; g = 1 halbiert bei u = 1,5 |
| `ctl_kp` / `ctl_ki` | 0,05 / 0,05 s⁻¹ | PI; Faustregel `k_i · Totzeit < 0,5` (Totzeit = Messlag + Multi-Lag ≈ 2–5 s) |
| `ctl_filter_s` | 3 s | Tiefpass von u für das PI (Schnitte nutzen den rohen Median) |
| `ctl_raise_a_s` / `ctl_raise_pct_s` | 2 A/s / 2 %/s | maximale Anhebe-Rate |
| `ctl_hold_s` | 5 s | Anhebe-Sperre nach Schnitt (> Mess- + Multi-Verzug) |
| `ctl_recover_s` / `ctl_forget_s` | 30 / 300 s | Leerlauf-Rückkehr zum Modell / Gültigkeit der Schnitt-Evidenz |
| `ctl_binding_frac` / `ctl_predict_frac` | 0,9 / 0,5 | „Limit bindet“ / Hochrechnung erst ab 50 % des Limits |
| `ctl_min_flow_a` | 2 A | darunter keine Messung in dieser Richtung |
| `ctl_max_age_s` / `ctl_flow_age_s` | 3 / 1,5 s | Frische für Gewinn über das Modell / für die Hochrechnung |
| `limit_control` | `pi` | `legacy` = alte multiplikative Rückkopplung (Vergleich/Rollback) |

Vorgehen: im Schattenbetrieb `reason_charge` mitschreiben (enthält `closed loop … (Zustand, u …)`),
die Totzeit aus dem Recorder ablesen (Limit-Schritt → Stromänderung), `ctl_ki` danach wählen, dann
`ctl_setpoint` so, dass die Pack-Ströme trotz Rauschen nicht über 1,0 laufen.


**Sonderfälle:**

| Fall | Behandlung |
|------|-----------|
| `s_i ≈ 0`, Ladepfad laut BMS **geschlossen** | widersprüchlich → `s_eff_i = s_floor_i` (Pack kann jederzeit Strom aufnehmen) |
| `s_i ≈ 0`, Ladepfad **offen** (BMS hat getrennt) | Pack aus `P_chg` entfernt; beim Wiedereinschalten Rampenstart mit `s_prior`-Anteil |
| Vorzeichenwechsel Laden↔Entladen | getrennte Schätzer je Richtung; Wechsel nutzt den jeweils anderen Satz, kein Reset |
| Messwerte verrauscht | σ_i wächst → s_eff steigt → Grenze sinkt (gewollt konservativ) |
| `CCL_i = 0` und Ladepfad geschlossen | Bank-CCL = 0 (physikalisch unvermeidbar: jeder Ladestrom fließt anteilig auch in dieses Pack) |
| Pack offline (Komm.) | §5.3 – Annahme „elektrisch getrennt“ für die anderen (konservative Re-Normierung) + Haltephase |

**Rechenbeispiel (reale DCL):** DCL = 43,6 / 92 / 59 A, s_eff ≈ 0,35 / 0,36 / 0,35 →
DCL_bank = min(124,6; 255; 168,6) = **124,6 A** (Σ = 194,6 A) – statt 59 A heute (`Pack 3 allein`) und
statt 194,6 A (`Σ`, würde Pack 1 mit ≈ 66 A überfahren). Ohne gültige Schätzung (s_eff = 0,495):
43,6/0,495 ≈ **88 A**. Lade-Beispiel R4: CCL_B = 20 A, sonst 200 A, s_eff_B ≈ 0,35 → **57 A**
(statt 20 A bzw. 420 A); ist B voller (ŝ_B = 0,15) → ≈ **117 A**.

### 4.3 Ladespannung (CVL)

```
CVL_follow = min_{i ∈ P_cvl} CVL_i                     (P_cvl = Packs mit geschlossenem Ladepfad
                                                         + offline-Packs in Haltephase mit letztem Wert)
CVL_cell   = V_bus − g · max(0, Vcell_max_bank − V_cell_target)   (Zellregler, g ≈ 16, Pack-Zellzahl)
CVL_raw    = min( CVL_follow , CVL_cell , CVL_cfg_max )
```

- **Abnahme sofort**, **Anstieg** rampenbegrenzt (Default 0,05 V je 10 s) mit Hysterese 0,05 V und
  Mindesthaltezeit 60 s nach jeder Absenkung → keine Oszillation mit der serialbattery-eigenen
  CVL-Hysterese.
- Pack mit nachweislich **offenem Ladepfad** (≥ 10 s FET-aus bzw. I_i ≈ 0 bei ladender Bank) fließt
  nicht in `CVL_follow` ein – sonst würde ein vom BMS bereits isoliertes Pack die ganze Bank
  festhalten.
- Spannungsbezug: `V_bus = Median` der Pack-Spannungen (robust gegen ein falsch messendes BMS);
  Kalibrieroffsets je Pack konfigurierbar.

**Zell-Überspannung abgestuft statt Hart-0 für die Bank** (Default-Schwellen für LiFePO4, konfigurierbar):

| Max. Zellspannung im Pack i | Reaktion |
|---|---|
| < 3,45 V | normal |
| 3,45–3,55 V | Zellregler senkt CVL (Bus-Spannung runter → hohe Zelle entlastet, Balancer arbeitet), CCL_i' = CCL_i linear auf 30 % |
| 3,55–3,60 V | CCL_i' linear auf 0; über Aufteilungsmodell wirkt das auf die Bank nur mit Faktor 1/s_i |
| ≥ 3,60 V oder Alarm HighCellVoltage = 2 | CCL_i' = 0 → Bank-CCL = 0, **bis** BMS den Ladepfad nachweislich geöffnet hat; danach Pack aus `P_chg`, Bank lädt mit den anderen weiter |

`CCL_i'` = min(serialbattery-CCL, eigene Kurve) – wir verschärfen nur, lockern nie.

### 4.4 SoC, Kapazität, Ah

```
Ah_rem_i  = SoC_i/100 · C_i
SoC_w     = Σ Ah_rem_i / Σ C_i          (über Packs ACTIVE + HOLD mit letztem Wert)
SoC_min   = min SoC_i  (+ Pack-ID)
/Soc      = je nach soc_mode:  weighted (Default) | min | guarded = min(SoC_w, SoC_min + D)  (D = 10 %)
/Capacity = Σ Ah_rem_i ;  /InstalledCapacity = Σ C_i ;  /ConsumedAmphours = −(Σ C_i − Σ Ah_rem_i)
```

**Kalibrierungsbewusstsein:**
- Pro Pack wird `t_last_full` persistiert (SoC = 100 und Tail-Strom < 0,02 C bei CVL).
  Kalibrierungsalter > 14 d → Warnung + optional **Kalibrier-Ladung**: CVL auf `N·3,45 V` mit
  Absorption bis alle Packs Tail-Strom unterschreiten (max. 2 h), danach Rückkehr.
- **Plausibilitätsprüfung** im Ruhezustand (|I_i| < 2 A ≥ 30 min): (a) absolut: SoC_i < 10 % bei
  Ruhe-Zellmittel > 3,25 V oder SoC_i > 90 % bei < 3,30 V; (b) relativ: Pack-zu-Pack-ΔSoC > 10 % bei
  ΔZellmittel < 15 mV (gleiche Busspannung ⇒ ähnlicher echter SoC; realer Fall 1 % vs. 13 % bei je
  ≈ 3,03 V) → Flag `soc_suspect` für alle beteiligten Packs + Empfehlung Kalibrier-Ladung, Gewicht von SoC_i in `SoC_w`
  wird nicht verändert (keine Erfindung von Werten), aber das Flag erscheint im Status und
  `soc_mode=guarded` ignoriert verdächtige Packs für `SoC_min`.
- **Sprünge bei Topologiewechsel** (Pack fällt weg/kommt hinzu): `/Soc` slew-begrenzt (max. 2 %/min),
  damit ESS-Logik nicht springt; Rohwert unter `/Custom/SocRaw`.
- Schutz der Unterkante kommt primär aus den **DCL-Limits** (§4.2 mit serialbattery-Taper bei
  niedrigem SoC/Zellspannung) und der eigenen Zell-Unterspannungskurve:
  min. Zelle < 3,00 V → DCL_i' linear auf 0 bis 2,90 V.
  → Ein leeres Pack bremst die Bank anteilig, statt (wie bisher) unbemerkt auf 0 % zu laufen.

### 4.5 Zellen, Temperaturen, Alarme

- `/System/MinCellVoltage`, `/MaxCellVoltage` = min/max über alle aktiven Packs; IDs als
  `"<PackLabel>.C<n>"` (z. B. `B.C07`) in `/System/MinVoltageCellId` usw.
- Temperaturen analog (`/System/MinCellTemperature`, …, IDs `A.T2`). `/Dc/0/Temperature` = max.
- `/System/NrOfModulesOnline/Offline/BlockingCharge/BlockingDischarge` aus dem Pack-Zustand.
- **Alarme**: je Pfad `max(level_i)` über ACTIVE-Packs (0 ok, 1 Warnung, 2 Alarm). Zusätzlich eigene:
  `/Alarms/BmsCable` = 1 bei ≥ 1 Pack offline, = 2 bei allen offline; `/Alarms/InternalFailure` = 2
  bei Core-FAULT; `/Alarms/CellImbalance` zusätzlich 1 bei Pack-zu-Pack-ΔSoC > 20 % (Drift-Hinweis).
- **Pro-Pack-Status** unter `/Custom/Packs/<label>/{State,Soc,Current,Share,CCL,DCL,CVL,SocSuspect}`
  für GUI/VRM-Diagnose (nicht von Venus ausgewertet).

### 4.6 Ladepfad-Zustand eines Packs (FET-Evidenz)

Reihenfolge der Quellen: (1) exportierter FET-Status, (2) `/Io/AllowToCharge|Discharge`
**[VERIFY: ob AllowToCharge in serialbattery den FET oder nur die Software-Absicht spiegelt]**,
(3) Strom-Evidenz: Bank lädt mit ≥ 20 A und `|I_i| < 1 A` für ≥ 10 s → „offen“.
Ist keine Quelle eindeutig → „unbekannt“ = **geschlossen** behandeln (konservativ).

### 4.7 Ratenbegrenzung und Publikation

| Größe | Absenken | Anheben | Totband |
|---|---|---|---|
| CCL/DCL | sofort | +5 A/s, zusätzlich max. +10 % je 10 s; 30 s Mindesthaltezeit nach Absenkung | 1 A |
| CVL | sofort | 0,05 V / 10 s, 60 s Haltezeit | 0,02 V |
| /Soc | – | slew 2 %/min nur bei Topologiewechsel | 0,1 % |

Messwerte (`/Dc/0/*`, Zellen) werden ungefiltert jede Sekunde publiziert. `AllowToCharge = CCL > 0`,
`AllowToDischarge = DCL > 0`.

---

## 5. Discovery und Pack-Lebenszyklus (R1)

### 5.1 Erkennung

- Abo auf `org.freedesktop.DBus.NameOwnerChanged`; beim Start einmal `ListNames`.
- Kandidaten: `com.victronenergy.battery.*` **außer** dem eigenen Dienst; Filter über
  `/ProductName`/`/Mgmt/ProcessName` (serialbattery) **und/oder** Whitelist von Seriennummern in der
  Konfiguration. Andere Batteriedienste (z. B. ein späterer SmartShunt) werden nie als Pack gezählt,
  können aber optional als Referenz-Strommesser dienen (`reference_current_service`).
- **Identität = BMS-Seriennummer** (bzw. konfigurierte Zuordnung), **nicht** der Dienstname: nach
  Reboot können ttyUSB-Nummern tauschen. Kapazität, Label (A/B/C), Kalibrierdaten und Schätzer hängen
  an der Identität.
- Werte via `ItemsChanged`-Signal abonnieren (kein Polling-Sturm), plus Fallback-`GetValue` alle 5 s
  pro Pack, falls Signale ausbleiben.

### 5.2 Zustandsautomat pro Pack

```
            erscheint                 30 s plausibel & frisch
 UNKNOWN ─────────────▶ QUARANTINE ───────────────────────────▶ ACTIVE
                            ▲  │ Flap-Backoff                    │  │ keine Aktualisierung > 10 s
                            │  └──────────▶ (bleibt)              │  ▼
                            │                                     │ STALE ── frisch ──▶ ACTIVE
                            │                                     │  │ > 30 s
                            └──── Rückkehr ◀──── OFFLINE (HOLD → GONE) ◀┘
 jederzeit: UNTRUSTED (Validierungsfehler/Schemafehler)   EXCLUDED (manuell, Wartung)
```

- **Frische:** Zeitstempel des letzten *geänderten* Werts. Komplett bit-identische Werte (alle
  Zellspannungen, Strom, Spannung) über 60 s → `STALE` (eingefrorener Treiber), da Zellspannungen
  im Betrieb im mV-Bereich schwanken.
- **Zulassung aus QUARANTINE:** ≥ 30 s frische Daten, Spannung innerhalb ±0,5 V vom Bus-Median,
  Zellzahl passt zur Konfiguration, Limits gültig, Stromsumme plausibel. Danach wird das Pack in
  `P_chg/P_dchg` mit `s_prior` aufgenommen; Bankgrenzen steigen nur über die Rampe (§4.7).
- **Flapping:** jede Rückkehr innerhalb 10 min nach Ausfall verdoppelt die Quarantänezeit
  (30 s → 60 → … max. 10 min); Warnung „Pack X instabil“.
- **UNTRUSTED** wirkt wie „Zustand unbekannt, elektrisch vorhanden“ → für die Bank wie OFFLINE/HOLD
  (§5.3), aber ohne Rückfall auf letzte Werte, wenn diese selbst ungültig waren.
- **EXCLUDED** (Setting `/Settings/BatteryAggregator/Exclude/<serial>`): für Wartung, wenn der Owner
  ein Pack physisch abgetrennt hat. Nur dann darf ein Pack ohne Rückfallregeln ignoriert werden.

### 5.3 Pack geht offline (Kommunikation weg) – konservativ, aber Bank läuft weiter

1. **STALE (10–30 s):** letzte Werte eingefroren weiterverwenden; Rampen nach oben gesperrt.
2. **OFFLINE/HOLD (bis 10 min):** Pack aus der Live-Rechnung, aber
   - für die verbleibenden Packs Annahme „Pack elektrisch getrennt“ → `s_j ← s_j / (1 − s_off)`
     (sie tragen ggf. den ganzen Strom = konservativ für sie),
   - CVL bleibt ≤ letzter CVL des Offline-Packs (es hängt wahrscheinlich noch am Bus),
   - zusätzliche Degradation: CCL/DCL ≤ 0,9 · Σ Limits der aktiven Packs,
   - SoC rechnet mit letztem Ah_rem des Offline-Packs weiter, Flag gesetzt.
3. **GONE (> 10 min):** Pack fällt aus SoC/Kapazität heraus (mit /Soc-Slew), CVL-Halt endet.
   Dessen Hardware-BMS schützt es weiterhin lokal.
Damit gilt R1 (Bank läuft ohne Eingriff weiter) und R5 (kein Absturz), ohne zu behaupten, ein nur
stumm gewordenes Pack sei weg.

### 5.4 Bank-Modi

| Modus | Bedingung | Publikation |
|---|---|---|
| INIT | Prozessstart, noch kein Pack ACTIVE (max. 120 s) | CCL = 0, DCL = `dcl_emergency`, CVL = `N·3,35 V` – außer Restart-Hold (§5.4a) |
| NORMAL | alle bekannten Packs ACTIVE | §4 |
| DEGRADED | ≥ 1 Pack STALE/OFFLINE/UNTRUSTED, ≥ 1 ACTIVE | §4 mit §5.3 |
| FAILSAFE | 0 Packs ACTIVE (nach STALE-Haltezeit) | CCL → 0 (sofort), DCL → `dcl_emergency` (Rampe 10 s), CVL = `N·3,35 V`, `/Alarms/BmsCable = 2` |
| FAULT | Core-Exception | letzte Ausgaben, CCL sofort halbiert, dann Verhalten wie FAILSAFE nach 10 s; Neustart durch Watchdog, falls > 60 s |

**Entscheidung FAILSAFE (alle Packs offline):** Dienst **bleibt registriert** und publiziert sichere
Werte, statt zu verschwinden. Begründung:
- Verschwindet der Dienst, fällt Venus je nach Einstellung auf einen anderen Batteriedienst oder auf
  „kein BMS“ zurück **[VERIFY: genaues DVCC-Verhalten bei „BMS lost“ in v3.70]**. Ein Rückfall auf ein
  einzelnes Pack wäre genau der Ursprungsfehler; „kein BMS“ kann bedeuten, dass Lader mit eigenen
  Ladeparametern ohne Zellsicht weiterlaufen.
- **Laden** ohne jede Zellsicht ist das größere Risiko → CCL = 0.
- **Entladen** auf 0 zu setzen erzeugt im Inselbetrieb einen Blackout, obwohl die JK-BMS lokal
  vor Unterspannung schützen. Daher `dcl_emergency` (Default 0,1 C ≈ 90 A; im reinen Netzparallel-ESS
  kann der Owner 0 wählen). CVL konservativ, damit auch Restladung durch MPPT-Überschuss die Zellen
  nicht hochtreibt.
- Die Konfiguration erzwingt beim Install eine explizite Wahl von `dcl_emergency` (kein stiller Default).

### 5.4a Restart-Hold (0.2.1)

Nach jedem Dienst-Neustart stehen alle Packs `quarantine_s` (30 s) in QUARANTINE → INIT. Ohne
Hold publizierte das Aggregat dabei CCL 0 / CVL 53,6 V und sprang nach der Zulassung auf
z. B. 200 A / 56,8 V: Ist das Aggregat Controlling BMS, kostet jeder Neustart einen Ladestopp und
einen CVL-Sprung. Daher:
- `state.json` enthält zusätzlich `outputs` = zuletzt in NORMAL/DEGRADED publizierte CVL/CCL/DCL +
  Wanduhrzeit; geschrieben alle 60 s und bei SIGTERM (daemontools `svc -t/-d`).
- Beim Start werden sie gehalten, wenn sie frisch sind (`0 ≤ Alter ≤ restart_hold_max_age_s`, 180 s),
  höchstens `restart_hold_s` (45 s = Quarantäne 30 s + Reserve), nur im Modus INIT.
- Jeder Schritt klemmt sie mit dem Live-Modell der (noch quarantänierten, live lesbaren) Packs:
  Pack-CCL/DCL inkl. Zell-, Temperatur- und Alarm-Derating, SR-21-Deckel (nur CCL; DCL ab 0.2.2
  ohne den 59-A-Startdeckel, D8), Live-CVL inkl. Zellregler. Ohne frischen Zustand gilt SR-21 wie bisher. Werte gehen nur nach unten (Ratsche), nie wieder hoch.
- Sofortiges Ende (dann INIT-Werte): ein gesehenes Pack wird blind (weg, UNTRUSTED, unplausibel,
  `/Connected = 0`), ein Pack meldet einen Alarm ≥ 2, Blind-Lade-Sperre, Fenster abgelaufen.
- Übergabe an NORMAL: die Rampen starten auf dem Hold-Wert, der Seed (D6) hebt ggf. auf das
  Live-Ziel – kein Einbruch. Hold-Werte werden nicht zurückgeschrieben (kein Verlängern durch
  Neustart-Ketten).
- Verworfene Alternative „Dienst erst nach Zulassung registrieren“: systemcalc (`dvcc.py`,
  `bms_seen and bms_service is None`) behandelt ein fehlendes Controlling-BMS als „BMS lost“ und
  hört auf, die Lader zu steuern – schlechter als ein sicher geklemmter Hold.

---

## 6. Robustheit (R5)

- **Isolation pro Pack:** jeder Adapter-Callback und jede Pack-Auswertung in `try/except`; Fehler
  setzt nur dieses Pack auf UNTRUSTED, nie den Prozess.
- **Core-Totalfehler:** Exception im `step()` → FAULT-Modus (§5.4), vollständiger Traceback ins Log,
  Zähler. Kein Absturz des Mainloops.
- **Watchdog:** GLib-Timer prüft, dass `step()` in den letzten 5 s lief; sonst `os._exit(1)` →
  daemontools startet neu (≈ 1–5 s). Zusätzlich Heartbeat-Pfad `/Custom/Heartbeat`.
- **Neustart-sicher:** Start immer in INIT (konservativ). Persistiert (atomar: tmp + `os.replace`,
  max. alle 5 min, in `/data/battery-aggregator/state.json`): RLS-Schätzer, `t_last_full`,
  Flap-Historie. Persistenz ist **nicht** sicherheitsrelevant – korrupte Datei → verwerfen, Prior nutzen.
- **Speicher/CPU:** begrenzte Puffer (Ringpuffer), kein Wachstum mit Laufzeit; Ziel < 2 % CPU, < 30 MB RSS.
- **Logging:** multilog nach `/data/log/battery-aggregator` (rotierend), Zustandswechsel, Limit-
  Ursache („CCL begrenzt durch B: 20 A / s 0,35“), Ausnahmen. Debug-Details hinter **einem**
  Bit (`DEBUG_MASK`), Instrumentierung bleibt im Code.
- **Erklärbarkeit:** `/Custom/LimitReason/Charge|Discharge|Voltage` als Text – Owner sieht in der
  GUI/VRM, *welches* Pack die Bank begrenzt.

---

## 7. Venus-Integration

- Dienstname `com.victronenergy.battery.aggregate`, feste DeviceInstance (Default 512, über
  `ClassAndVrmInstance` in localsettings; **nicht** 0/4–28/99 – dort liegen verwaiste Einträge früherer
  Aggregatoren, u. a. battery/0 „Alle Batterien“; Bereinigung ist Owner-Schritt), `/ProductName = "Battery Aggregator"`, `/Connected = 1`,
  `/Mgmt/*`, `/FirmwareVersion` = eigene Version.
- **Systemeinstellungen** (einmalig durch Installer, vorherige Werte gesichert für Rollback):
  Batteriemonitor `/Settings/SystemSetup/BatteryService` → Aggregat;
  Controlling BMS `/Settings/SystemSetup/BmsInstance` → Aggregat-Instanz **[VERIFY: Pfadname/Werte in v3.70]**.
- Startprüfung: Weicht eine der Einstellungen ab (z. B. Reset durch Firmwareupdate), Log-Warnung;
  `/Alarms/InternalFailure = 1` nur, wenn **keine** der beiden auf das Aggregat zeigt. Eine halbe
  Auswahl (nur Controlling BMS oder nur Batteriemonitor) ist eine bewusste Owner-Wahl und nur eine
  Log-Warnung – ein InternalFailure auf dem aktiven BMS wäre ein Fehlalarm (0.2.1).
  Automatisches Zurücksetzen nur, wenn `enforce_system_settings = true`.
- Einzelne Pack-Dienste bleiben sichtbar (Diagnose). Ob Venus bei mehreren BMS-Diensten einen
  Hinweis „mehrere BMS“ zeigt und ob serialbattery die Pack-Dienste als „nicht steuernd“ markieren
  kann, ist **[VERIFY]**.
- ESS: Venus regelt den Batteriestrom mit `/Dc/0/Current` → Summe der Pack-Ströme der ACTIVE-Packs
  (bei offline-Packs ggf. Referenz-Strommesser verwenden, sonst Flag „Strom unvollständig“).

---

## 8. Teststrategie

1. **Core-Unit-Tests** (pytest, PC): jede Formel aus §4 mit Tabellenfällen inkl. aller Szenarien aus
   `03_SZENARIEN.md` als parametrisierte Tests (Szenario-ID = Test-ID).
2. **Property-Tests** (hypothesis) – Invarianten für beliebige Eingaben (inkl. None/NaN/negativ/riesig):
   - kein Exception, alle Ausgaben endlich, CCL/DCL ≥ 0, CVL ≤ `CVL_cfg_max`;
   - **Sicherheit:** für jedes Pack in `P_chg` gilt `s_eff_i · CCL_bank + c_i ≤ CCL_i'` (Modellebene);
   - `CCL_bank ≤ Σ_{P_chg} CCL_i` und `≤ CCL_hw`;
   - **Monotonie:** Absenken irgendeines `CCL_i`/`CVL_i` erhöht nie eine Ausgabe im selben Schritt;
   - Rampen: Anstieg je Schritt ≤ Rampengrenze; Absenkung ohne Verzögerung;
   - CVL ≤ min CVL_i über `P_cvl`;
   - Topologiewechsel (Pack weg/hinzu) erhöht Limits nie sprunghaft;
   - Determinismus: gleiche Eingabefolge → gleiche Ausgabefolge.
3. **Simulator (closed loop, PC):** N Packs mit LiFePO4-OCV-Kurve, R_i (inkl. Temperatur), Zellstreuung,
   Balancer, JK-Schutzlogik (FETs, OVP/UVP/OCP/LTP), serialbattery-CCL/CVL-Logik (vereinfacht),
   Kommunikationsfehler (Ausfall, Einfrieren, Flapping, Müllwerte), DVCC-Lader-/Wechselrichtermodell mit
   1–3 s Verzögerung. Szenarien als YAML; Ausgabe: Zeitreihen + Pass/Fail gegen erwartete Werte.
   Kernmetrik: **kein Pack überschreitet sein Limit > 5 % länger als 3 s**; Bank nie unnötig auf
   `min(CCL_i)`.
4. **Replay echter Daten:** Recorder (kleines Tool auf dem Cerbo, read-only) schreibt alle Pack-Pfade
   1 Hz als CSV; Replay auf dem PC durch den Core, Vergleich mit dem, was Venus tatsächlich tat.
5. **Adapter-Vertragstest:** Liste der benötigten serialbattery-Pfade; gegen einen Mitschnitt
   (`dbus -y … GetValue`) jeder neuen serialbattery-Version prüfen, bevor auf dem Cerbo aktualisiert wird.
6. **Schattenbetrieb auf dem Cerbo (vor Umschaltung, 1–2 Wochen):** Dienst läuft mit
   `shadow = true` → registriert **keinen** Batteriedienst (nur Log + `/Custom` unter eigenem
   Nicht-Battery-Namen), Steuerung bleibt unverändert; Vergleich der berechneten Limits mit der Realität.
7. **Abnahme live:** nach Umschaltung definierte Live-Prüfungen (Szenarien S01, S02, S22, S31 real
   bzw. durch gezieltes Ziehen eines BMS-USB-Kabels bei moderater Last) – Belege aus dem
   Produktionspfad, nicht nur aus dem Simulator.

---

## 9. Deployment auf dem Cerbo

- Voraussetzung: Root/SSH (Owner aktiviert „Superuser“ + SSH; Owner-Schritt).
- Layout: `/data/battery-aggregator/releases/<version>/`, Symlink `current`, Konfig
  `/data/battery-aggregator/config.ini` (bleibt bei Updates), Logs `/data/log/battery-aggregator`.
  `/data` überlebt Firmwareupdates, Rootfs nicht.
- Dienst: daemontools-Verzeichnis (`run` + `log/run`); Symlink nach `/service` wird bei **jedem Boot**
  aus `/data/rc.local` (idempotent, nur anhängen, bestehende Einträge z. B. von serialbattery nicht
  anfassen) neu angelegt – so überlebt der Dienst Firmwareupdates **[VERIFY: /service-Mechanik v3.70]**.
- `install.sh`: Python-Syntaxcheck, Selbsttest des Cores, Sichern der alten Systemeinstellungen,
  Start im Schattenmodus. `activate.sh`: Umschalten der Systemeinstellungen. `rollback.sh`: Dienst
  stoppen (`svc -d`), Symlink entfernen, gesicherte Einstellungen zurück, `current` auf Vorversion.
  Alle Skripte mit `--dry-run`.
- Nach Firmwareupdate: Boot-Hook + Startprüfung (§7) melden, ob Einstellungen/Python-API noch passen.
- serialbattery-Update: erst Vertragstest (§8.5) gegen neuen Mitschnitt, dann Update.

---

## 10. Konfigurationsparameter (Defaults)

| Parameter | Default | Bedeutung |
|---|---|---|
| `packs` | Seriennr. → Label, C_i, Zellzahl | Identität/Kapazität |
| `ccl_hw` / `dcl_hw` | Owner (Sicherungen/Kabel) | absolute Bankgrenzen |
| `ocp_hw_i` / `n1_mode` | JK-OCP je Pack (Owner) / `hw` | N-1-Kaskadenschutz |
| `dcl_emergency` | **Pflichtfeld** (Vorschlag 90 A) | FAILSAFE/INIT-Entladegrenze |
| `stale_s` / `offline_s` / `hold_s` | 10 / 30 / 600 | Lebenszyklus |
| `quarantine_s` | 30 (Backoff bis 600) | Wiederaufnahme |
| `i_est_min` | 15 A | Mindeststrom für Anteilsschätzung |
| `share_k` / `share_floor` | 2 / 0,5·prior | konservativer Anteil |
| `ramp_up_a_s` / `hold_after_drop_s` | 5 / 30 | Ratenbegrenzung |
| `limit_control` / `ctl_*` | `pi` / siehe §4.2a | geschlossener Regelkreis je Pack-Limit |
| `v_cell_target` / `v_cell_soft` / `v_cell_hard` | 3,45 / 3,55 / 3,60 V | Zellregler |
| `v_cell_low_soft` / `v_cell_low_hard` | 3,00 / 2,90 V | Entlade-Taper |
| `soc_mode` | weighted | weighted / min / guarded |
| `calibration_interval_d` | 14 | Kalibrier-Ladung |
| `enforce_system_settings` | false | Venus-Einstellungen automatisch korrigieren |
| `shadow` | true (bis zur Abnahme) | Schattenbetrieb |

---

## 11. Offene Punkte für die Konklave

1. [VERIFY] FET-Status- und AllowToCharge-Semantik in serialbattery 2.0 RC (bestimmt Qualität von §4.6).
2. [VERIFY] DVCC-Verhalten bei „Controlling BMS verschwunden“ in v3.70 → bestätigt/ändert §5.4.
3. ESS (State 10) mit Min-SoC 0 % → Unterkante wird **nur** über DCL-Taper/Zellspannung geschützt;
   netzparallel oder auch Inselbetrieb (Netzausfall)? → Wahl `dcl_emergency`.
4. CVL 56,8 V (3,55 V/Zelle) bei historischen Spitzen 3,84 V: Owner-Entscheid, ob `v_cell_target`
   3,45 V (Default, Zellregler senkt CVL) oder serialbattery-CVL senken.
5. Kein SSH/Root auf dem Cerbo → Installation & Recorder blockiert bis Owner-Freigabe.
6. Gibt es einen unabhängigen Shunt (SmartShunt)? → verbessert I_bank bei Pack-Ausfall erheblich.
7. Wie weit lässt sich serialbattery so konfigurieren, dass die Pack-Dienste nie als BMS gewählt werden?
8. Alternativ/ergänzend zu bestehenden Aggregatoren (z. B. dbus-aggregate-batteries): Fork vs.
   Neuentwicklung – dieses Design setzt auf Neuentwicklung mit reinem Core, weil die genannten
   Fehler (min-Limit für die Bank, Absturz bei Pack-Ausfall) Architekturfehler sind, keine Bugs.
