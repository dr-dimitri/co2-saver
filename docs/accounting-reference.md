# Referenzrechnung: zwei Speicherzyklen mit Neustart

Diese Zeitreihe prüft die Regeln der [Bilanzierungs-ADR](decisions/0001-accounting-and-input-contract.md),
insbesondere Abschnitte 5–8 und 10. Die zweite Ladung trifft auf einen noch
gefüllten Speicher. Danach folgen eine Entladung mit Export und eine vollständige
Restentladung. Dazwischen wird Home Assistant neu gestartet.

Der zugehörige Test ist
[`test_documented_overlapping_storage_cycles_survive_restart_exactly`](../tests/test_accounting_reference.py).
Er läuft mit Wechselrichter- und Smartmeter-Topologie, jeweils mit
Gesamtverbrauch und Anteilen sowie mit getrennt gemessenen Verbrauchern. Er
verwendet echte Home-Assistant-Zustände, den Minutenpoll, den verifizierten
Generationsspeicher und eine zweite Home-Assistant-Instanz. Hardware und externe
Dienste werden nicht benötigt. Die erwarteten Zahlen sind als unabhängige
Konstanten hinterlegt; sie werden nicht mit dem zu prüfenden Bilanzkern erzeugt.

## Voraussetzungen und Einheiten

- Nutzbare Speicherkapazität: `K = 10 kWh`.
- Wirkungsgrad: `eta = 0,9`. Verluste entstehen im Modell beim Laden.
- PV-Herstellungsfaktor: `F_pv = 40 gCO₂e/kWh` erzeugter PV-Energie.
- Speicher-Herstellungsfaktor: `F_bat = 20 gCO₂e/kWh` anrechenbarer lokaler PV-Entladung.
- Die Wallbox nutzt in jedem Referenzintervall genau `25 %` der lokalen Last;
  das Haus nutzt `75 %`. Im Anteilsmodus ist dies die konfigurierte Aufteilung.
  Im separaten Modus liefern zwei Zähler genau diese Lasten. Der Gesamtverbrauch
  enthält die Wallbox im Anteilsmodus bereits.
- Jeder Energiezähler beginnt bei `100 kWh`. Zunächst wird eine gemeinsame
  Baseline beobachtet. Eine danach vollständig beobachtete Entladung von
  `10 kWh` zu lokalen Lasten engt den anfänglich unbekannten Speicher von
  `[0; 10]` auf `[0; 0]` ein. Diese unbekannte Energie erhält keine PV-Gutschrift.
  Erst danach beginnt die folgende Referenzrechnung.

Alle Energieangaben in den Tabellen sind `kWh`, Emissionsangaben sind `gCO₂e`.
Brüche wie `400/9` sind **exakt**, nicht gerundete Dezimalwerte. Die Rechentabellen
verwenden Intervallenergien, die im Test durch Differenzen kumulativer Zähler
entstehen. Die Zeitpunkte sind UTC; alle Pflichtquellen eines Intervalls besitzen
dasselbe physische Periodenende. Die jeweils angegebene aktuelle Netz-CO₂-Probe
`G` stammt genau von diesem Periodenende und erfüllt den Quellenvertrag.

## Gemessene Zeitreihe

`P` bezeichnet PV-Erzeugung, `I` Netzbezug, `X` Export, `C` Batterieladung,
`D` Batterieentladung und `L` die gesamte lokale Last. Die Wallbox ist eine
Teilmenge von `L`, keine zusätzliche Standortlast.

| Ende | Vorgang | P | I | X | C | D | L | Wallbox | G in gCO₂e/kWh |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 12:03 | A: PV-Ladung und direkte Nutzung | 6 | 0 | 1 | 3 | 0 | 2 | 0,5 | 400 |
| 12:04 | A: Teilentladung | 0 | 0 | 0 | 0 | 1 | 1 | 0,25 | 500 |
| 12:05 | B: Mischladung in den Restbestand | 3 | 1 | 0 | 4 | 0 | 0 | 0 | 100 |
| — | Home-Assistant-Neustart | — | — | — | — | — | — | — | — |
| 12:06 | B: Entladung mit Export | 0 | 0 | 1 | 0 | 2 | 1 | 0,25 | 600 |
| 12:07 | B: vollständige Restentladung | 0 | 0 | 0 | 0 | 3,3 | 3,3 | 0,825 | 300 |

Jede Zeile erfüllt `P + I + D = L + C + X`. Der Smartmeterpfad leitet
`P = L + C + X - I - D` ab und erhält deshalb dieselben PV-Energien wie der
Wechselrichterpfad. Beim Neustart bleiben sämtliche Zählerbaselines und der
vollständige Ledger erhalten; Setup erzeugt weder einen Messwert-Read noch eine
Buchung. Die nächste aktuelle CO₂-Probe kommt erst mit dem nächsten Minutenpoll.

## Herkunft und aufgeschobene PV-Belastung

`S` steht hier für beide Bestandsgrenzen: Nach der beweisenden Leerentladung gilt
in dieser ausgeglichenen Zeitreihe stets `S_lo = S_hi = S`. `P_lo` ist dagegen
nur die garantierte PV-Untergrenze. `N_hi = S - P_lo` ist die maximal mögliche
Nicht-PV-Menge, keine behauptete exakte Mischung.

| Nach Intervall | S | P_lo | N_hi | B_pv | rho_pv in gCO₂e/kWh |
| --- | ---: | ---: | ---: | ---: | ---: |
| A: Ladung | 2,7 | 2,7 | 0 | 120 | 400/9 |
| A: Teilentladung | 1,7 | 1,7 | 0 | 680/9 | 400/9 |
| B: Mischladung | 5,3 | 4,4 | 0,9 | 1760/9 | 400/9 |
| Neustart | 5,3 | 4,4 | 0,9 | 1760/9 | 400/9 |
| B: Entladung mit Export | 3,3 | 2,4 | 0,9 | 320/3 | 400/9 |
| B: Restentladung | 0 | 0 | 0 | 0 | 0 |

Die erste Ladung fügt `3 × 0,9 = 2,7 kWh` PV-Bestand und
`3 × 40 = 120 gCO₂e` aufgeschobene Belastung hinzu. Ihre obere Belastungsdichte
ist `40 / 0,9 = 400/9`. Die erste lokale Entladung schreibt `1 kWh` gut und
entnimmt `400/9 gCO₂e`; zurück bleiben `120 - 400/9 = 680/9 gCO₂e`.

Die Mischladung fügt `4 × 0,9 = 3,6 kWh` Bestand hinzu. Davon sind
`3 × 0,9 = 2,7 kWh` garantiert PV und höchstens `0,9 kWh` Nicht-PV. Zusammen
mit dem Restbestand entstehen `S = 1,7 + 3,6 = 5,3`,
`P_lo = 1,7 + 2,7 = 4,4` und `B_pv = 680/9 + 120 = 1760/9`.
Die Netzintensität `100` beim Laden erzeugt keine Speicherersparnis.

Bei der folgenden `2-kWh`-Entladung ist nur `1 kWh` lokale Batterienutzung
garantiert; die andere `1 kWh` geht ins Netz. Nach Abzug des möglichen
Nicht-PV-Bestands bleiben nur `max(0; 1 - 0,9) = 0,1 kWh` garantiert lokal
genutzte PV. Der neue Herkunftsnachweis muss trotzdem um die **gesamte**
Entladung sinken: `P_lo_neu = 4,4 - 2 = 2,4`. Andernfalls könnte bereits
entladene PV in einem späteren Intervall noch einmal gutgeschrieben werden.

Die lokale Belastung beträgt `H(0,1) = 40/9`. Die verbleibende Belastung wird
auf die neue PV-Untergrenze begrenzt:

```text
B_pv_neu = min(1760/9 - 40/9; (400/9) × 2,4)
          = 960/9 = 320/3 gCO₂e
```

Damit gehen `1760/9 - 40/9 - 960/9 = 760/9 gCO₂e` endgültig aus dem
Gutschriftnachweis verloren. Sie werden weder als Ersparnis gebucht noch für
eine spätere Entladung aufbewahrt. Der verlorene PV-Herkunftsnachweis beträgt
`4,4 - 0,1 - 2,4 = 1,9 kWh`; dies ist eine konservative Beweisgrenze und
keine Messung einer exakten inneren Mischung oder einer exakten PV-Exportmenge.

Die abschließende vollständig lokale Entladung von `3,3 kWh` enthält mindestens
`3,3 - 0,9 = 2,4 kWh` PV. Sie verbraucht den restlichen PV-Nachweis und dessen
Belastung `320/3` vollständig.

## Exakte Emissionsrechnung

Nur das erste Intervall liefert direkte PV: `2 kWh` ergeben bei `G = 400`
eine Bruttovermeidung von `800 gCO₂e`, eine PV-Belastung von `80 gCO₂e` und
`720 gCO₂e` direkte Nettoersparnis. Diese Werte bleiben danach unverändert.

Die folgenden Speicherwerte sind **kumulativ**, jeweils nach dem Intervall:

| Intervall | PV-Speicherenergie | Brutto vermieden | PV-Belastung | Speicherbelastung | Speicher-Netto | Gesamt-Netto einschließlich direkter PV |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A: Ladung | 0 | 0 | 0 | 0 | 0 | 720 |
| A: Teilentladung | 1 | 500 | 400/9 | 20 | 3920/9 | 10400/9 |
| B: Mischladung | 1 | 500 | 400/9 | 20 | 3920/9 | 10400/9 |
| B: Entladung mit Export | 1,1 | 560 | 440/9 | 22 | 4402/9 | 10882/9 |
| B: Restentladung | 3,5 | 1280 | 1400/9 | 70 | 9490/9 | 15970/9 |

Die drei Entladungen verwenden ausschließlich ihre jeweilige aktuelle Probe:

```text
Speicher-Brutto = 1 × 500 + 0,1 × 600 + 2,4 × 300 = 1280 gCO₂e
Speicher-PV-Belastung = 400/9 + 40/9 + 320/3 = 1400/9 gCO₂e
Speicher-Herstellungsbelastung = (1 + 0,1 + 2,4) × 20 = 70 gCO₂e
Speicher-Netto = 1280 - 1400/9 - 70 = 9490/9 gCO₂e
Gesamt-Netto = 720 + 9490/9 = 15970/9 gCO₂e = 1597/900 kgCO₂e
```

Die gesamte beim Laden aufgeschobene PV-Belastung schließt exakt:
`240 = 1400/9 + 760/9`. Der erste Teil wurde genau einmal bei lokaler
PV-Entladung belastet; der zweite Teil verlor seinen Gutschriftnachweis.

## Verbraucher und Energieerhaltung

Die direkte Energie entfällt auf `1,5 kWh` Haus und `0,5 kWh` Wallbox, ohne
Zuordnungsrest. Die lokale Speicherenergie wird pro Verbraucher unabhängig
begrenzt. Bei der Entladung mit Export liegt sowohl die Hauslast `0,75` als
auch die Wallboxlast `0,25` unter `N_hi = 0,9`; deshalb kann keine der beiden
Lasten die systemweit garantierten `0,1 kWh` einzeln beanspruchen.

Bei der letzten Entladung erhält das Haus
`max(0; 2,475 - 0,9) = 1,575 kWh`, die Wallbox
`max(0; 0,825 - 0,9) = 0`. Der Rest beträgt `2,4 - 1,575 = 0,825 kWh`.

| Nach Intervall | Haus: PV-Speicherenergie | Wallbox: PV-Speicherenergie | Zuordnungsrest | System |
| --- | ---: | ---: | ---: | ---: |
| A: Ladung | 0 | 0 | 0 | 0 |
| A: Teilentladung | 0,75 | 0,25 | 0 | 1 |
| B: Mischladung | 0,75 | 0,25 | 0 | 1 |
| B: Entladung mit Export | 0,75 | 0,25 | 0,1 | 1,1 |
| B: Restentladung | 93/40 | 1/4 | 37/40 | 7/2 |

Die Energie-Sicht schließt exakt: `93/40 + 1/4 + 37/40 = 7/2 kWh`.
Verbraucher-Nettoemissionen sind wegen ihrer unabhängig konservativen
Belastungsobergrenzen keine Summanden eines neu berechneten Systemergebnisses.

Über die fünf Referenzintervalle sind `P = 9`, `I = 1`, `L = 7,3`,
`X = 2`, `C = 7` und `D = 6,3 kWh`. Die `0,7 kWh` Ladeverluste erklären
`9 + 1 = 7,3 + 2 + 0,7`. Direkt und aus dem Speicher werden zusammen nur
`2 + 3,5 = 5,5 kWh` PV gutgeschrieben. Die beiden PV-Pfade verwenden getrennte
Flüsse; weder Export, Netzladung, Ladeverlust noch verlorener Herkunftsnachweis
werden später als zusätzliche PV-Nutzung gebucht.

## Reproduktion

```bash
pytest tests/test_accounting_reference.py
```

Alle vier Varianten vergleichen nach jeder Zeile exakt die Bestandsgrenzen,
den PV-Nachweis, die aufgeschobene Belastung, die kumulativen Emissionskomponenten
sowie Verbraucherenergie und Zuordnungsrest. Jede Buchung muss genau eine
verifizierte Generationsrevision bilden. Der Neustart darf diese Revision,
Baselines, Summen und den gefüllten Ledger nicht verändern.
