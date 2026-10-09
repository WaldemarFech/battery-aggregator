# 05 – Schattenbetrieb (erste Phase auf dem Cerbo) / Shadow mode

Stand 2026-10-04 · Bezug: `02_DESIGN.md` §8.6, `04_SICHERHEIT_FMEA.md` SR-14/SR-21, Code `src/battery_aggregator/adapter/`.

## Ziel

Der Aggregator läuft auf dem Cerbo **neben** dem heutigen System, liest alle serialbattery-Dienste und rechnet
jede Sekunde, was er veröffentlichen *würde* – ohne irgendetwas an DVCC, Systemeinstellungen oder BMS zu ändern.
1–2 Wochen Mitschnitt belegen Modell (Anteile, Limits, Zustandsautomat) gegen den realen Produktionspfad,
bevor ein einziger Wert wirksam wird.

## Was im Schattenbetrieb passiert

| Aspekt | Verhalten |
|---|---|
| Battery-Dienst | **keiner**. `com.victronenergy.battery.aggregate` wird nicht registriert (`LivePublisher` verweigert sich, solange `shadow = true`). Venus kann ihn also weder als BMS noch als Batteriemonitor wählen. |
| Optionaler Diagnosedienst | `com.victronenergy.battaggregator_shadow` (keine Battery-Klasse) mit `/Custom/Shadow/*` (CCL, DCL, CVL, SoC, Modus) – für dbus-spy/MQTT. |
| Log | JSON-Zeile je Änderung der Kernwerte und mindestens alle 60 s: `would_publish` (alle Pfade wie live), `model_ccl/model_dcl` (vor SR-21-Deckel), Ereignisse, Pro-Pack-Status (Zustand, Anteil, Pack-Limits, Pfad offen/zu). multilog nach `/data/log/battery-aggregator`. |
| Schreibzugriffe | keine auf Pack-Dienste (SR-14, im Test per FakeBus belegt), keine auf Settings (`check_system_settings` korrigiert nur bei `shadow = false` **und** `enforce_system_settings = true`). |
| Persistenz | `state.json` (Anteilsschätzer, `t_last_full`) wird bereits geschrieben – der Schätzer lernt im Schatten. |
| Watchdog / Fault | aktiv wie live (Hänger → Prozessende → daemontools-Neustart), damit auch Stabilität mitgemessen wird. |

## Auswertung (Abnahmekriterien für das Verlassen des Schattens)

1. **Pfad-Vertrag**: alle `REQUIRED`-Pfade (`adapter/paths.py`) bei allen drei Packs vorhanden; `[VERIFY]`-Pfade
   (Seriennummer, FET-Status, SocResetLastReached, Settings-Pfade) per `dbus -y` belegt und `paths.py` angepasst.
2. **Anteile**: gelernte `share_dchg/share_chg` je Pack vs. Zangenmessung (FMEA Kap. 8, Messung 3); erst danach
   `measured_shares_verified = true` (SR-21). Bis dahin ist `would_publish` ohnehin auf 59 A DCL / 210 A CCL (Wechselrichtergrenze, D5) gedeckelt.
3. **Zustandsautomat**: jeder serialbattery-Neustart (≈ 50 s Lücke) erzeugt STALE → OFFLINE → QUARANTINE → ACTIVE
   ohne Prozessabsturz; keine Doppelzählung nach ttyUSB-Wechsel.
4. **Vergleich mit Realität**: Zeitpunkte, zu denen ein Pack sein eigenes Limit überschritt (Pack-Strom > Pack-DCL),
   müssen im Log als „wäre begrenzt worden“ erkennbar sein; keine Phase, in der `would_publish` über einem Pack-Limit
   × 1/Anteil lag.
5. **Keine Oszillation** der `would_publish`-Limits (≤ 1 Anstieg je 30 s).

## Übergang zu live (Owner-Schritt, nicht automatisch)

`activate.sh` (noch nicht gebaut): Settings sichern → `shadow = false` → Dienst neu starten → BmsInstance/BatteryService
auf Instanz 512 → Live-Abnahme S01/S02/S22/S31 (siehe `03_SZENARIEN.md`). `rollback.sh` stellt die gesicherten Settings
wieder her.

---

**EN (short):** In shadow mode the process reads all serialbattery services, runs the full core every second and only
logs (JSON) what it would publish; it never registers a battery service, never writes settings or pack services, and
can expose `/Custom/Shadow/*` under a non-battery service name. Exit criteria: path contract verified, shares measured
(SR-21 flag), lifecycle robust across driver restarts, no oscillation. Going live is an explicit owner step.
