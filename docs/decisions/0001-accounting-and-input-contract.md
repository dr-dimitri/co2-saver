# ADR-0001: Mess- und CO₂-Bilanzierungsvertrag für den MVP

- **Status:** Angenommen
- **Version:** 1.0
- **Datum:** 2026-09-04
- **Gültig für:** MVP `0.1.0`
- **Zugehöriges Issue:** [#1](https://github.com/dr-dimitri/co-saver/issues/1)

## Entscheidung in Kürze

Der erste Entwurf verarbeitet ausschließlich kumulative Energiezähler. Er unterstützt eine wechselrichterbasierte und eine smartmeterbasierte Eingangstopologie, jeweils mit oder ohne Speicher. Haus und zusätzliche Verbraucher werden entweder über einen gemeinsamen Zähler mit festen Anteilen oder über getrennte, überschneidungsfreie Zähler erfasst.

Die Netz-CO₂-Intensität kommt verpflichtend aus einem auswählbaren Home-Assistant-Sensor. Als empfohlene Quelle dient die offizielle [Electricity-Maps-Integration](https://www.home-assistant.io/integrations/co2signal/). CO2 Saver hängt aber weder von dieser Integration noch von deren Entity-Namen ab und akzeptiert jeden kompatiblen Sensor über denselben normalisierten Quellvertrag.

PV-Strom erzeugt nur dann eine Gutschrift, wenn er einen konfigurierten lokalen Verbraucher versorgt. Speicherladung erzeugt keine Gutschrift. PV-stämmiger Speicherstrom wird erst bei lokaler Entladung mit der dann gültigen Netz-CO₂-Intensität bewertet. Netzeinspeisung wird nicht gutgeschrieben.

Diese Auswahl ist der kleinste verlässlich testbare Umfang. Die Verträge trennen Quellen, Normalisierung und Bilanzkern, sodass später Leistungssensoren, weitere CO₂-Quellen oder genauere Speichermodelle ergänzt werden können, ohne die fachlichen Kernobjekte auszutauschen.

## 1. Systemgrenze und Begriffe

Die Systemgrenze ist der elektrische AC-Bus des Standorts hinter dem Netzanschlusspunkt. Zähler müssen ihre Energie auf derselben Seite dieser Grenze messen.

| Begriff | Verbindliche Bedeutung |
| --- | --- |
| PV-Erzeugung | Vom Wechselrichter am Standort bereitgestellte AC-Energie. |
| Netzbezug | Aus dem öffentlichen Netz in den Standort geflossene Energie. |
| Netzeinspeisung | Aus dem Standort in das öffentliche Netz geflossene Energie. Sie erhält im MVP keine CO₂-Gutschrift. |
| Lokaler Verbrauch | Energie für Haus und konfigurierte zusätzliche Verbraucher; Speicherladung und Netzeinspeisung gehören nicht dazu. |
| Direkter PV-Eigenverbrauch | PV-Energie, die im selben Bilanzintervall einen lokalen Verbraucher versorgt. |
| Speicherladung | Energie, die am AC-Eingang des Speichers aufgenommen wird. Sie erzeugt noch keine CO₂-Gutschrift. |
| Speicherentladung | Energie, die der Speicher am AC-Ausgang abgibt. Nur der PV-stämmige und lokal verbrauchte Anteil ist gutschriftfähig. |
| Bruttovermeidung | CO₂e des Netzstroms, der durch gutschriftfähige PV-Energie ersetzt wird. |
| Herstellungsbelastung | Konfigurierbare PV- und Speicher-Lebenszyklusemissionen, die der gutschriftfähigen Energie zugerechnet werden. |
| Netto-CO₂-Ersparnis | Bruttovermeidung abzüglich PV- und Speicher-Herstellungsbelastung. Ein negativer Wert bleibt negativ. |

CO2 Saver erstellt keine vollständige Lebenszyklusanalyse der Anlage. Es bilanziert ausschließlich die Emissionen, die der als Eigenverbrauch gutgeschriebenen Energie zugeordnet werden. Exportierte Energie und netzstämmige Speicherenergie bleiben außerhalb der Gutschrift.

## 2. Einheiten und Eingangsvertrag

Intern gelten folgende Basiseinheiten:

| Größe | Interne Einheit | Eingaben im MVP |
| --- | --- | --- |
| Energie | `kWh` | `Wh`, `kWh`, `MWh` |
| CO₂-Intensität | `gCO₂e/kWh` | Gramm- und Kilogrammvarianten mit eindeutigem CO₂e-Bezug |
| Kumulative Emission | `gCO₂e` intern, `kgCO₂e` als Entity | Keine externe Eingabe |
| Anteil oder Wirkungsgrad | Wert von `0` bis `1` | Anzeige im Config Flow in Prozent |

Alle Energieeingänge sind positive, richtungsgetrennte `sensor`-Entities mit `device_class: energy`, einem numerischen Zustand und `state_class: total` oder `total_increasing`. Ein bidirektionaler Nettozähler mit Vorzeichen ist im MVP nicht zulässig. Auch bei `state_class: total` muss ein für eine Richtung ausgewählter Eingang monoton wachsen; ein Rückgang wird als Reset behandelt.

Leistungssensoren in `W` oder `kW` werden im MVP nicht unterstützt. Damit entfällt eine fehleranfällige zeitliche Integration. Eine spätere `PowerIntervalSource` kann denselben normalisierten Intervallvertrag bedienen, ohne den Bilanzkern zu ändern.

## 3. Zulässige Eingangstopologien

### 3.1 Gemeinsame Verbrauchsmodi

Jede Erzeugungstopologie wird mit genau einem der folgenden Verbrauchsmodi kombiniert:

| Modus | Pflichtsensoren | Optionale Angaben | Ableitung |
| --- | --- | --- | --- |
| Gesamtverbrauch mit Anteilen | Ein kumulativer Zähler für den gesamten lokalen Verbrauch | Beliebig viele benannte Verbraucher mit festem Prozentanteil | Zusätzliche Verbraucher erhalten ihren Anteil je Intervall; der Rest ist Hausverbrauch. |
| Separate Verbraucher | Ein kumulativer Zähler für Hausverbrauch; je zusätzlichem Verbraucher ein eigener kumulativer Zähler | Weitere benannte, überschneidungsfreie Verbraucher | Lokaler Gesamtverbrauch ist die Summe aller Zähler. |

Die Modi dürfen in einem Config Entry nicht gemischt werden. Verbrauchszähler erfassen ausschließlich lokale Lasten und schließen Speicherladung sowie Netzeinspeisung aus.

### 3.2 Wechselrichterbasierte Topologie

| Rolle | Ohne Speicher | Mit Speicher | Verwendung |
| --- | --- | --- | --- |
| PV-Erzeugung | Pflicht | Pflicht | Autoritative PV-Energie am AC-Ausgang des Wechselrichters. |
| Verbrauch gemäß Abschnitt 3.1 | Pflicht | Pflicht | Autoritative lokale Last. |
| Netzbezug | Optional | Optional | Plausibilitätsprüfung und Diagnose. |
| Netzeinspeisung | Optional | Optional | Plausibilitätsprüfung und Diagnose. |
| Speicherladung | Entfällt | Pflicht | AC-Energie in den Speicher. |
| Speicherentladung | Entfällt | Pflicht | AC-Energie aus dem Speicher. |

Die PV-Erzeugung wird direkt gemessen. Netzflüsse sind für die Berechnung nicht erforderlich, solange PV, lokaler Verbrauch und gegebenenfalls Speicherflüsse vollständig vorliegen. Werden Netzsensoren angegeben, müssen sie die Energiebilanz innerhalb der Toleranz bestätigen.

### 3.3 Smartmeterbasierte Topologie

| Rolle | Ohne Speicher | Mit Speicher | Verwendung |
| --- | --- | --- | --- |
| Netzbezug | Pflicht | Pflicht | Richtungsgetrennter Importzähler. |
| Netzeinspeisung | Pflicht | Pflicht | Richtungsgetrennter Exportzähler. |
| Verbrauch gemäß Abschnitt 3.1 | Pflicht | Pflicht | Autoritative lokale Last. |
| PV-Erzeugung | Optional | Optional | Nur Plausibilitätsprüfung; nicht zweite Wahrheitsquelle. |
| Speicherladung | Entfällt | Pflicht | AC-Energie in den Speicher. |
| Speicherentladung | Entfällt | Pflicht | AC-Energie aus dem Speicher. |

Für ein Intervall wird die PV-Erzeugung aus der Energieerhaltung abgeleitet:

```text
E_pv = E_last + E_batterieladung + E_export
       - E_netzbezug - E_batterieentladung
```

Ohne Speicher sind Lade- und Entladewert null. Ein negativer Wert außerhalb der Toleranz oder eine Abweichung zu einem optionalen PV-Zähler macht das Intervall ungültig.

### 3.4 Warum es zunächst nur diese Varianten gibt

Kumulative, richtungsgetrennte Energiezähler sind einfacher wiederherzustellen und erzeugen keine Integrationsfehler aus unregelmäßigen Leistungssamples. Die zwei Topologien decken sowohl Anlagen mit zugänglichem Wechselrichterzähler als auch Anlagen mit vollständiger Messung am Netzanschlusspunkt ab. Zusätzliche Topologien werden später als Quelladapter ergänzt und ändern nicht die normalisierten Rollen.

## 4. Netz-CO₂-Intensität als austauschbare Sensorquelle

### 4.1 Empfohlene vorhandene Integration

Die offizielle Home-Assistant-Integration [Electricity Maps](https://www.home-assistant.io/integrations/co2signal/) ist die Referenzquelle. Sie hieß früher CO2Signal, verwendet intern weiterhin die Domain `co2signal` und stellt einen Carbon-Intensity-Sensor in `gCO2eq/kWh` bereit. Sie fragt die Electricity-Maps-API standardmäßig etwa alle 15 Minuten ab und benötigt einen eigenen API-Schlüssel.

Die Empfehlung ist keine technische Abhängigkeit. CO2 Saver installiert Electricity Maps nicht, greift nicht auf dessen interne Klassen zu und erwartet keinen festen Entity-Namen.

### 4.2 Generischer Quellvertrag

Der Config Flow speichert nur die Entity-ID eines ausgewählten `sensor`. Ein Home-Assistant-Adapter erzeugt daraus ein normalisiertes Sample:

```text
GridIntensitySample(
    value_g_co2e_per_kwh,
    observed_at,
    source_entity_id,
)
```

Der Bilanzkern kennt ausschließlich dieses Sample. Damit funktionieren neben Electricity Maps auch Template-Sensoren, lokale Datenquellen und andere Integrationen, sofern sie folgende Bedingungen erfüllen:

- numerischer, endlicher und nicht negativer Zustand;
- eindeutige Einheit wie `gCO2eq/kWh`, `gCO2e/kWh`, `gCO₂e/kWh` oder eine eindeutig umrechenbare Kilogrammvariante;
- Zeitpunkt der letzten Meldung ist verfügbar; bevorzugt wird `State.last_reported`, damit auch eine erneut gemeldete, unveränderte Intensität als frisch gilt. Nur wenn die in Issue #2 festgelegte Mindestversion dies erfordert, darf der Adapter auf `last_updated` zurückfallen;
- Zustand ist weder `unknown` noch `unavailable`.

Ein Sensor für CO₂-Raumluftkonzentration in `ppm` ist ausdrücklich inkompatibel. Da Home Assistant für Netz-CO₂-Intensität keine allgemeine passende Device Class garantiert, wird nach Semantik und Einheit validiert, nicht nach einer anbieterbezogenen Integration oder Device Class.

Der Benutzer kann die maximal erlaubte Quellenalterung konfigurieren. Der sichtbare Standard ist `60 Minuten`, der erlaubte Bereich `1` bis `1440 Minuten`. Eine ältere oder ungültige Probe erzeugt keine CO₂-Buchung.

### 4.3 Zeitliche Zuordnung

Ein Energieintervall verwendet die jüngste gültige CO₂-Probe, deren Zeitstempel nicht nach dem Intervallende liegt und deren Alter den konfigurierten Grenzwert nicht überschreitet. Für direkten PV-Verbrauch ist dies die Probe am Verbrauchszeitpunkt. Für Speicherenergie ist es ausdrücklich die Probe am Entladezeitpunkt; eine Netzintensität vom Ladezeitpunkt wird nicht gespeichert.

Eine statische Netzintensität ist im MVP nicht vorgesehen. Die Sensorabstraktion lässt einen späteren konstanten oder historischen Adapter zu, ohne Formeln oder Speicherledger zu ändern.

## 5. Intervallbildung, Plausibilität und Zuordnung

CO2 Saver liest die aktuellen kumulativen Zählerstände in einem festen 60-Sekunden-Takt aus dem Home-Assistant-Zustandsspeicher. Es bildet Deltas zum letzten vollständig akzeptierten Snapshot. Intern wird nicht gerundet.

Ein Intervall ist höchstens 15 Minuten lang. Bei einer längeren Lücke, einem unvollständigen Pflichtsatz oder einem unzulässigen Wert werden alle betroffenen Zähler neu baseliniert und für die Lücke keine Energie oder CO₂-Ersparnis geschätzt.

Die Bilanzabweichung darf höchstens den größeren Wert aus `0,01 kWh` und `2 %` des größten Intervallflusses betragen. Größere Widersprüche verwerfen das Intervall und erhöhen einen Diagnosezähler. Diese Toleranz ist eine versionierte MVP-Konstante und kann später konfigurierbar werden.

Für ein gültiges Intervall gelten diese Zuordnungsprioritäten:

```text
E_direkt = min(E_pv, E_last)
E_pv_überschuss = max(E_pv - E_direkt, 0)
E_pv_ladung = min(E_pv_überschuss, E_batterieladung)
E_netz_ladung = max(E_batterieladung - E_pv_ladung, 0)
E_batterie_lokal = min(E_batterieentladung, max(E_last - E_direkt, 0))
E_batterie_export = max(E_batterieentladung - E_batterie_lokal, 0)
```

PV versorgt damit rechnerisch zuerst lokale Last, dann den Speicher und erst danach den Export. Batterieentladung versorgt nur den nach direkter PV-Nutzung verbleibenden lokalen Bedarf; ein darüber hinausgehender Anteil gilt als Export. Optional gemessene Netzflüsse müssen zu den verbleibenden Flüssen passen.

Werden im selben Intervall Speicherladung und Speicherentladung oberhalb der Toleranz gemessen, ist die Herkunft nicht eindeutig. Der MVP verwirft dann das gesamte Intervall, verändert weder CO₂-Summen noch Speicherledger und baseliniert für den nächsten vollständigen Snapshot neu.

## 6. Verbraucherzuordnung

Im Anteilsmodus erhält jeder zusätzliche Verbraucher einen festen Anteil zwischen `0 %` und `100 %`. Die Summe aller zusätzlichen Anteile darf `100 %` nicht überschreiten. Der nicht zugeordnete Rest ist Hausverbrauch. Die Anteile werden in jedem Intervall gleichermaßen auf direkten PV-Verbrauch, PV-stämmige Speicherentladung und die zugehörigen Emissionskomponenten angewendet.

Im separaten Modus wird die Quellenergie proportional zum gemessenen Intervallverbrauch auf Haus und zusätzliche Verbraucher verteilt. Bei Verbräuchen `E_i` und Gesamtverbrauch `E_last` gilt für eine gutschriftfähige Energiemenge `E_gutschrift`:

```text
E_gutschrift_i = E_gutschrift * E_i / E_last
```

Bei null Gesamtverbrauch ist jede Verbraucherzuordnung null. Rundungsreste entstehen nur in der Darstellung; intern müssen die Verbraucherwerte exakt zum Systemwert summieren.

## 7. Speicherherkunft und Verluste

Wenn ein Speicher konfiguriert wird, muss der Benutzer einen sichtbaren AC-Rundtrip-Wirkungsgrad `eta` von mehr als `0 %` bis einschließlich `100 %` bestätigen. Der Config Flow schlägt `90 %` vor, übernimmt ihn aber nicht verborgen. Ein späteres, messwertbasiertes Verlustmodell kann denselben Ledger ersetzen.

Der persistente Speicherledger enthält zwei output-äquivalente Energiemengen:

- `S_pv`: voraussichtlich am AC-Ausgang nutzbare, PV-stämmige Energie;
- `S_grid`: voraussichtlich am AC-Ausgang nutzbare, netzstämmige Energie.

Beim Laden gilt:

```text
S_pv += E_pv_ladung * eta
S_grid += E_netz_ladung * eta
```

Die Herkunft gemischter Ladungen wird als gewichteter Durchschnitt behandelt. Vor einer Entladung ist der PV-Anteil:

```text
q_pv = S_pv / (S_pv + S_grid)
```

Die gesamte Entladung reduziert beide Ledgeranteile proportional. Auch Entladung ins Netz und nicht lokal nutzbare Entladung verbrauchen Herkunftsbestand, erzeugen aber keine Gutschrift. Für `E_batterieentladung > 0` gilt:

```text
E_pv_entladung_gesamt = min(E_batterieentladung * q_pv, S_pv)
E_pv_speicher_lokal = E_pv_entladung_gesamt
                         * E_batterie_lokal / E_batterieentladung
```

Nur `E_pv_speicher_lokal` ist gutschriftfähig. Der Ledger darf nie negativ werden. Übersteigt eine Entladung den modellierten Bestand, wird nur der nachweisbare Bestand zugeordnet; die Abweichung wird diagnostiziert. Eine Ladung allein verändert keinen CO₂-Ersparniszähler.

## 8. Emissionsfaktoren und Formeln

Alle Faktoren sind nicht negative Werte in `gCO₂e/kWh`. Der zulässige technische Bereich ist `0` bis `5000`. Es gibt keine stillen Projektstandardwerte für PV und Speicher; der Benutzer muss die für seine Anlage passenden Werte explizit bestätigen.

| Faktor | Quelle und Bezugsbasis |
| --- | --- |
| `G` Netzintensität | Aus dem gewählten Home-Assistant-Sensor; pro am Standort vermiedener Netz-kWh zum Verbrauchs- beziehungsweise Entladezeitpunkt. |
| `F_pv` PV-Herstellungsfaktor | Benutzereingabe; pro am AC-Ausgang erzeugter PV-kWh. |
| `F_bat` Speicher-Herstellungsfaktor | Benutzereingabe; pro am AC-Ausgang entladener, gutschriftfähiger Speicher-kWh. |

Für direkten PV-Verbrauch `E_direkt` gilt:

```text
Brutto_direkt = E_direkt * G
PV_Last_direkt = E_direkt * F_pv
Netto_direkt = Brutto_direkt - PV_Last_direkt
```

Für lokal genutzte, PV-stämmige Speicherentladung `E_pv_speicher_lokal` gilt:

```text
Brutto_speicher = E_pv_speicher_lokal * G_entladung
PV_Last_speicher = (E_pv_speicher_lokal / eta) * F_pv
Speicher_Last = E_pv_speicher_lokal * F_bat
Netto_speicher = Brutto_speicher - PV_Last_speicher - Speicher_Last
```

Der PV-Herstellungsanteil der Speicherladung wird damit einschließlich des vereinbarten Verlusts erst bei Entladung bilanziert. Jede belastete PV-kWh und jede gutschriftfähige Speicher-kWh wird genau einmal berücksichtigt. Ein negatives Nettoergebnis wird nicht auf null begrenzt.

## 9. Zahlenbeispiele

### 9.1 Wechselrichter, Gesamtverbrauch und Wallbox-Anteil

In einem Intervall werden `5 kWh` PV-Erzeugung und `4 kWh` lokaler Gesamtverbrauch gemessen. Es gibt keinen Speicher. Die Wallbox erhält `25 %`, der Hausrest `75 %`. Mit `G = 400 gCO₂e/kWh` und `F_pv = 40 gCO₂e/kWh` gilt:

```text
E_direkt = min(5, 4) = 4 kWh
Brutto = 4 * 400 = 1600 gCO₂e
PV-Last = 4 * 40 = 160 gCO₂e
Netto = 1440 gCO₂e = 1,44 kgCO₂e
Wallbox = 25 % * 1,44 = 0,36 kgCO₂e
Haus = 75 % * 1,44 = 1,08 kgCO₂e
```

Die verbleibende `1 kWh` PV ist Export und wird nicht gutgeschrieben.

### 9.2 Smartmeter und separate Verbraucher

Haus und Wallbox verbrauchen `2 kWh` und `1 kWh`. Netzbezug ist `1 kWh`, Export `2 kWh`, ein Speicher ist nicht vorhanden:

```text
E_pv = 3 + 2 - 1 = 4 kWh
E_direkt = min(4, 3) = 3 kWh
```

Bei denselben Faktoren wie oben beträgt die Nettoersparnis `3 * (400 - 40) = 1080 gCO₂e`. Davon entfallen proportional `720 gCO₂e` auf das Haus und `360 gCO₂e` auf die Wallbox.

### 9.3 PV-Speicherzyklus

Beim Laden werden `6 kWh` PV, `2 kWh` Last und `3 kWh` Speicherladung gemessen. `1 kWh` verbleibt als Export. Bei `eta = 0,9` steigt `S_pv` um `2,7 kWh`. Die Speicher-CO₂-Ersparnis bleibt zu diesem Zeitpunkt unverändert.

Später entlädt der Speicher `2 kWh` vollständig an eine lokale Last. Die Netzintensität beträgt dann `500 gCO₂e/kWh`, `F_pv = 40 gCO₂e/kWh` und `F_bat = 20 gCO₂e/kWh`:

```text
Brutto_speicher = 2 * 500 = 1000 gCO₂e
PV_Last_speicher = (2 / 0,9) * 40 = 88,89 gCO₂e
Speicher_Last = 2 * 20 = 40 gCO₂e
Netto_speicher = 871,11 gCO₂e
```

Erst diese Entladung erhöht die Speicherersparnis. Im Ledger verbleiben `0,7 kWh` PV-stämmige output-äquivalente Energie.

### 9.4 Gemischte Speicherladung

Nach einer Ladung enthält der Ledger `2,7 kWh` PV- und `0,9 kWh` Netzenergie. Der PV-Anteil beträgt `75 %`. Bei einer lokalen Entladung von `2 kWh` sind daher nur `1,5 kWh` gutschriftfähig. Die übrigen `0,5 kWh` gelten als netzstämmig und erzeugen keine PV-CO₂-Ersparnis.

## 10. Fehler-, Reset- und Neustartverhalten

- **Zählerreset oder Austausch:** Ein Rückgang rebaseliniert genau diese Messrolle. Das betroffene Intervall bucht null; bereits kumulierte Ergebnisse bleiben erhalten.
- **`unknown`, `unavailable`, nicht numerisch oder falsche Einheit:** Solange ein Pflichtsensor ungültig ist, wird kein vollständiges Energieintervall gebucht. Nach Rückkehr wird der gesamte Pflichtsatz neu baseliniert; die Lücke wird nicht geschätzt.
- **Ungültige CO₂-Probe:** Energie- und Speicherherkunft werden physikalisch fortgeschrieben, aber die gutschriftfähige Energie des Intervalls erhält keine Emissionsbuchung. Sie wird in getrennten Diagnosezählern für unbewertete direkte und gespeicherte PV-Energie erfasst und nicht später mit einem aktuellen Wert nachbewertet.
- **Duplikate und verspätete Ereignisse:** Ein Snapshot mit gleichem Zählerstand und Zeitstempel ist idempotent. Zeitstempel vor oder gleich dem letzten akzeptierten Snapshot werden ignoriert.
- **Home-Assistant-Neustart:** Letzte Zählerstände, Zeitstempel, kumulative Emissionskomponenten, Diagnosezähler, `S_pv`, `S_grid`, Faktorenbasis und Schemaversion werden atomar persistiert und vor der ersten neuen Auswertung wiederhergestellt.
- **Beschädigter oder inkompatibler Zustand:** Keine Schätzung. Der Config Entry meldet eine reparierbare Störung und bucht bis zur kontrollierten Reinitialisierung keine neuen Werte.

## 11. Änderungen an Konfiguration und Faktoren

Faktoren und CO₂-Quellen wirken ausschließlich auf zukünftige Intervalle. Historische Summen werden nicht neu berechnet.

Ein Sensorwechsel startet für die betroffene Rolle ein neues Messsegment und rebaseliniert deren Zählerstand. Ein Wechsel der Verbrauchstopologie oder der Speicheridentität erfordert eine ausdrückliche Bestätigung. Historische Emissionssummen bleiben bestehen; bei einem Speicherwechsel wird der Herkunftsledger auf null gesetzt, damit Energie nicht auf ein anderes Gerät übertragen wird.

Systemweite Summen bleiben auch bei einer Änderung der Verbraucherstruktur erhalten. Verbraucher besitzen stabile interne Kennungen: Umbenennen führt dieselbe Zeitreihe fort, Entfernen beendet sie, und ein neu angelegter Verbraucher erhält eine neue Zeitreihe. Historische Verbraucherwerte werden bei einem Topologiewechsel nicht rückwirkend umverteilt.

Diese Segmentierung wird versioniert persistiert. Ein späterer optionaler Rechenlauf über Recorder-Historie kann als separater Adapter ergänzt werden, ohne die bereits gespeicherten Originalsegmente stillschweigend umzuschreiben.

## 12. Ergebnis-Entities und Statistiksemantik

Der MVP stellt mindestens folgende kumulative Werte bereit:

| Ergebnis | Einheit | `state_class` | Verhalten |
| --- | --- | --- | --- |
| Brutto vermiedene Netz-Emissionen | `kgCO₂e` | `total_increasing` | Nicht negativ und monoton. |
| PV-Herstellungsbelastung | `kgCO₂e` | `total_increasing` | Nicht negativ und monoton. |
| Speicher-Herstellungsbelastung | `kgCO₂e` | `total_increasing` | Nicht negativ und monoton. |
| Netto-CO₂-Ersparnis gesamt | `kgCO₂e` | `total` | Darf durch negative Intervalle sinken. |
| Netto-CO₂-Ersparnis direkte PV | `kgCO₂e` | `total` | Darf sinken. |
| Netto-CO₂-Ersparnis Speicher | `kgCO₂e` | `total` | Ändert sich nicht beim Laden und darf sinken. |
| Direkt genutzte PV-Energie | `kWh` | `total_increasing` | Monotoner Transparenzwert. |
| Lokal genutzte PV-Speicherenergie | `kWh` | `total_increasing` | Monotoner Transparenzwert. |
| Unbewertete direkte PV-Energie | `kWh` | `total_increasing` | Diagnose für fehlende CO₂-Proben. |
| Unbewertete PV-Speicherenergie | `kWh` | `total_increasing` | Diagnose für fehlende CO₂-Proben. |

Für Haus und jeden zusätzlichen Verbraucher werden direkt genutzte PV-Energie, lokal genutzte PV-Speicherenergie und Netto-CO₂-Ersparnis separat bereitgestellt. Die ungerundeten Verbraucherwerte müssen jeweils zum Systemwert summieren.

Alle kumulativen Werte und Ledgerzustände werden restartfest gespeichert. Emissions-Entities verwenden keine unpassende CO₂-Konzentrations-Device-Class. Darstellung rundet nur über Home Assistants Anzeigepräzision; der persistierte Rechenwert bleibt ungerundet.

## 13. Verbindlicher MVP-Umfang

Enthalten sind:

- beide Eingangstopologien aus Abschnitt 3;
- beide Verbrauchsmodi;
- null oder ein Speicher pro Config Entry;
- eine frei auswählbare Netz-CO₂-Sensorquelle;
- konstante, explizit bestätigte PV- und Speicherfaktoren;
- prospektive, restartfeste Intervallbilanzierung;
- System- und Verbraucher-Entities aus Abschnitt 12.

Bewusst nicht enthalten sind Leistungssensoren, statische Netzfaktoren, historische Nachberechnung, Exportgutschriften, mehrere Speicher pro Entry, Hybridmischungen der Verbrauchsmodi, Tarife, Kosten, Prognosen, Lade- oder Laststeuerung, Dashboards und externe Telemetrie.

Die Erweiterungspunkte sind absichtlich stabil: Energiequellen liefern normalisierte Intervalle, die CO₂-Quelle liefert `GridIntensitySample`, der Speicher verwendet einen versionierten Herkunftsledger und der Bilanzkern kennt keine anbieterbezogenen Home-Assistant-Integrationen.

## 14. Abdeckung der Entscheidungen aus Issue #1

| Fragengruppe | Entscheidung |
| --- | --- |
| Systemgrenze und Begriffe | Abschnitte 1 und 2 |
| Wechselrichter- und Smartmeter-Topologien | Abschnitt 3 |
| Energie- oder Leistungssensoren | Nur Energiezähler im MVP, Abschnitte 2 und 3 |
| Einheiten, Device Class und State Class | Abschnitt 2 |
| Verbrauchsmodelle und Anteile | Abschnitte 3.1 und 6 |
| Netz-CO₂-Quelle | Generischer Sensor, Electricity Maps empfohlen, Abschnitt 4 |
| Wertebereiche und Faktorbasen | Abschnitte 4.2, 7 und 8 |
| Zeitpunkt der Herstellungsbelastung | Abschnitt 8 |
| Speicherherkunft, Mischladung, Verluste und Export | Abschnitte 5 und 7 |
| Resets, Ausfälle, verspätete Daten und Neustart | Abschnitt 10 |
| Faktor- und Topologieänderungen | Abschnitt 11 |
| Ergebniswerte und Langzeitstatistik | Abschnitt 12 |
| MVP und Ausschlüsse | Abschnitt 13 |
