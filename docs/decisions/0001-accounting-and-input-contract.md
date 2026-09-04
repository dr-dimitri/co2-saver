# ADR-0001: Mess- und CO₂-Bilanzierungsvertrag für den MVP

- **Status:** Angenommen
- **Version:** 2.1
- **Datum:** 2026-09-04
- **Gültig für:** MVP `0.1.0`
- **Zugehörige Issues:** [#1](https://github.com/dr-dimitri/co-saver/issues/1), [#17](https://github.com/dr-dimitri/co-saver/issues/17), [#20](https://github.com/dr-dimitri/co-saver/issues/20)

## Entscheidung in Kürze

Der MVP verarbeitet ausschließlich kumulative Energiezähler. Er unterstützt eine wechselrichterbasierte und eine smartmeterbasierte Eingangstopologie, jeweils mit richtungsgetrennt gemessenem Netzbezug und Export sowie mit oder ohne Speicher. Haus und zusätzliche Verbraucher werden entweder über einen gemeinsamen Zähler mit festen Anteilen oder über getrennte, überschneidungsfreie Zähler erfasst.

Die Netz-CO₂-Intensität kommt verpflichtend aus einem auswählbaren Home-Assistant-Sensor. Als empfohlene Quelle dient die offizielle [Electricity-Maps-Integration](https://www.home-assistant.io/integrations/co2signal/). CO2 Saver hängt aber weder von dieser Integration noch von deren Entity-Namen ab und akzeptiert jeden kompatiblen Sensor über denselben normalisierten Quellvertrag.

Aggregierte Intervallwerte verraten bei mehreren gleichzeitig aktiven Quellen und Senken nicht jeden physischen Teilfluss. Deshalb wird nur die mathematisch garantierte Untergrenze eines PV-Flusses gutgeschrieben; der uneindeutige Rest bleibt dauerhaft nicht gutschriftfähig. Speicherladung erzeugt keine Gutschrift. Garantiert PV-stämmiger Speicherstrom wird erst bei garantiert lokaler Entladung mit der dann gültigen Netz-CO₂-Intensität bewertet. Netzeinspeisung wird nicht gutgeschrieben.

Version 2.0 korrigiert die beim Vorabreview entdeckte optimistische Prioritätszuordnung aus Version 1.0. Sie ergänzt Messwert-Frische, eine konservative Speicherquarantäne, eine Kapazitätsgrenze, aufgeschobene PV-Herstellungsbelastung und fail-closed Wiederherstellung. Diese Auswahl ist der kleinste verlässlich testbare Umfang. Die Verträge trennen Quellen, Normalisierung und Bilanzkern, sodass später Leistungssensoren, weitere CO₂-Quellen oder genauere Speichermodelle ergänzt werden können, ohne die fachlichen Kernobjekte auszutauschen.

Version 2.1 präzisiert die Beobachtungsgrenze: Ausschließlich der feste 60-Sekunden-Takt liest Energiezustände; einzelne State-Events lösen keine zusätzlichen Auswertungen aus. Jeder Wechsel der deklarierten Energieeinheit unterbricht die Zeitreihe konservativ und verlangt eine neue Baseline, auch wenn beide Einheiten grundsätzlich unterstützt und ihre normalisierten Werte monoton sind. Dadurch können weder kurzlebige Zwischenzustände die festgelegte Stichprobe verändern noch ein unbewiesener Maßstabswechsel eine Energiespitze erzeugen.

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
| Nutzbare Speicherenergie | `kWh` am AC-Ausgang | expliziter Wert von `0,1` bis `1000 kWh` |
| CO₂-Intensität | `gCO₂e/kWh` | Gramm- und Kilogrammvarianten mit eindeutigem CO₂e-Bezug |
| Kumulative Emission | `gCO₂e` intern, `kgCO₂e` als Entity | Keine externe Eingabe |
| Anteil oder Wirkungsgrad | Wert von `0` bis `1` | Anzeige im Config Flow in Prozent |

Alle Energieeingänge sind nichtnegative, richtungsgetrennte `sensor`-Entities mit `device_class: energy`, einem numerischen Zustand und `state_class: total` oder `total_increasing`. Ein bidirektionaler Nettozähler mit Vorzeichen ist im MVP nicht zulässig. Auch bei `state_class: total` muss ein für eine Richtung ausgewählter Eingang monoton wachsen; jede echte Abnahme wird als Reset behandelt.

Die deklarierte Einheit gehört zur Identität einer akzeptierten Zählerprobe und wird mit Baseline und Kandidaten persistiert. Ein Wechsel zwischen `Wh`, `kWh` und `MWh` ist zwar für einen neuen Baseline-Snapshot zulässig, darf aber ein laufendes Segment nicht kontinuierlich überbrücken. Sobald eine Rolle nach ihrer akzeptierten Baseline eine andere Einheit meldet, wird das aktuelle Intervall ohne Delta verworfen und die Zeitreihe gemäß Abschnitt 10 neu gebaselined. Diese konservative Regel gilt auch dann, wenn die beiden normalisierten Zählerstände rechnerisch monoton wären.

Unterstützte Mindestbasis ist Home Assistant `2026.9.0`. Ausgewählte Quellen müssen einen Eintrag in der Entity Registry besitzen. Persistiert wird dessen umbenennungsstabile Registry-ID, nicht die veränderliche Entity-ID.

Jede Energiequelle muss außerdem einen autoritativen, UTC-normalisierbaren Messperioden-Zeitstempel im State-Attribut `co2saver_period_end` liefern. Alle Rollen eines Snapshots müssen dort exakt dasselbe Ende derselben physischen Erfassungsperiode ausweisen. Für Quellen ohne dieses Attribut oder ohne gemeinsame Messperiode kann der kumulative MVP keine positive Flussgarantie ableiten. Der Adapter verwendet `State.last_reported` zusätzlich als Zeitpunkt der Home-Assistant-Veröffentlichung; ein Fallback auf `last_updated` ist nicht zulässig.

Leistungssensoren in `W` oder `kW` werden im MVP nicht unterstützt. Damit entfällt eine fehleranfällige zeitliche Integration. Eine spätere `PowerIntervalSource` kann denselben normalisierten Intervallvertrag bedienen, ohne den Bilanzkern zu ändern.

### 2.1 Konkreter synchroner Quellpfad

Der generische Entity-Adapter ist mit normalen Fremdsensoren nur dann nutzbar, wenn deren Integration die gemeinsame physische Messperiode tatsächlich kennt. Der MVP dokumentiert deshalb als konkreten Referenzpfad einen atomaren JSON-Snapshot über Home Assistants [MQTT-Sensoren](https://www.home-assistant.io/integrations/sensor.mqtt/). Ein Zähler-Gateway veröffentlicht nach Abschluss einer Erfassungsperiode genau eine Nachricht mit dem messseitig erzeugten UTC-Zeitpunkt und sämtlichen kumulativen Pflichtzählern, zum Beispiel:

```json
{
  "period_end": "2026-09-04T12:00:00Z",
  "pv_generation_kwh": "123.4",
  "grid_import_kwh": "45.6",
  "grid_export_kwh": "7.8",
  "local_load_kwh": "161.2"
}
```

Für jede konfigurierte Rolle extrahiert ein MQTT-Energiesensor den passenden Wert aus demselben Topic und übernimmt ausschließlich den Zeitstempel derselben Nachricht als Attribut. Das verbindliche Muster für jede Rolle lautet, hier beispielhaft für Netzbezug:

```yaml
mqtt:
  sensor:
    - name: "CO2 Saver Netzbezug"
      unique_id: "co2_saver_grid_import"
      state_topic: "site/energy/cumulative"
      value_template: "{{ value_json.grid_import_kwh }}"
      json_attributes_topic: "site/energy/cumulative"
      json_attributes_template: >-
        {{ {'co2saver_period_end': value_json.period_end} | tojson }}
      device_class: energy
      state_class: total_increasing
      unit_of_measurement: "kWh"
      force_update: true
```

Alle weiteren Rollen verwenden dasselbe `state_topic` und `json_attributes_topic`. `force_update` ist erforderlich, damit auch eine Rolle ohne Zählerzuwachs für jede neue Periode veröffentlicht wird. Der Nachrichtenerzeuger muss die Werte aus derselben abgeschlossenen Geräteerfassung atomar zusammenstellen; ein Home-Assistant-Empfangszeitpunkt, eine gerundete Uhrzeit oder das nachträgliche Zusammenkopieren unabhängig aktualisierter Entities erfüllt diesen Vertrag nicht. Ein Trigger-Template ist nur dann gleichwertig, wenn sein Trigger selbst einen messseitigen Abschlussdatensatz mit allen Werten und dessen echtem Periodenende liefert. Der Config Flow zeigt diese Einschränkung an und verlangt ihre Bestätigung. Native Integrationen mit derselben garantierten Attributsemantik bleiben zulässig.

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
| Netzbezug | Pflicht | Pflicht | Richtungsgetrennter Importzähler und Quelle der konservativen Flussgrenzen. |
| Netzeinspeisung | Pflicht | Pflicht | Richtungsgetrennter Exportzähler und Senke der konservativen Flussgrenzen. |
| Speicherladung | Entfällt | Pflicht | AC-Energie in den Speicher. |
| Speicherentladung | Entfällt | Pflicht | AC-Energie aus dem Speicher. |

Die PV-Erzeugung wird direkt gemessen. Auch Netzbezug und Netzeinspeisung sind Pflicht: Ohne beide Richtungen kann aus kumulativer PV-Erzeugung und lokalem Verbrauch keine positive Untergrenze für direkten PV-Verbrauch bewiesen werden. Innerhalb desselben Intervalls könnte sonst die gesamte PV-Energie exportiert und die gesamte Last aus dem Netz versorgt worden sein. Ein einzelner Netzsensor oder ein Nettofluss schließt unbeobachtete Gegenflüsse ebenfalls nicht aus.

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

Ohne Speicher sind Lade- und Entladewert null. Für den Rohwert gilt `tau_roh = max(0,01 kWh, 0,02 * max(E_last, E_batterieladung, E_export, E_netzbezug, E_batterieentladung))`. Ein Rohwert kleiner als `-tau_roh` macht das Intervall ungültig. Für einen konfigurierten PV-Plausibilitätszähler gilt gesondert `tau_pv = max(0,01 kWh, 0,02 * max(E_pv, E_pv_gemessen))`; eine größere Abweichung verwirft das Intervall, eine kleinere ändert den autoritativ abgeleiteten Wert nicht. Ein Rohwert von `-tau_roh` bis unter null wird auf null gesetzt; die tolerierte Differenz wird als unbekannter Fluss behandelt und kann keine Gutschrift begründen.

### 3.4 Warum es zunächst nur diese Varianten gibt

Kumulative, richtungsgetrennte Energiezähler sind einfacher wiederherzustellen und erzeugen keine Integrationsfehler aus unregelmäßigen Leistungssamples. Die zwei Topologien unterscheiden sich in ihrer autoritativen PV-Quelle: Der Wechselrichterpfad misst PV direkt, der Smartmeterpfad leitet sie aus der vollständigen Standortbilanz ab. Import und Export werden in beiden Pfaden benötigt, damit dieselben konservativen Teilflussgrenzen gelten. Zusätzliche Topologien werden später als Quelladapter ergänzt und ändern nicht die normalisierten Rollen.

## 4. Netz-CO₂-Intensität als austauschbare Sensorquelle

### 4.1 Empfohlene vorhandene Integration

Die offizielle Home-Assistant-Integration [Electricity Maps](https://www.home-assistant.io/integrations/co2signal/) ist die Referenzquelle. Sie hieß früher CO2Signal, verwendet intern weiterhin die Domain `co2signal` und stellt einen Carbon-Intensity-Sensor in `gCO2eq/kWh` bereit. Sie fragt die Electricity-Maps-API standardmäßig etwa alle 15 Minuten ab und benötigt einen eigenen API-Schlüssel.

Die Empfehlung ist keine technische Abhängigkeit. CO2 Saver installiert Electricity Maps nicht, greift nicht auf dessen interne Klassen zu und erwartet keinen festen Entity-Namen.

### 4.2 Generischer Quellvertrag

Der Config Flow speichert die Entity-Registry-ID eines ausgewählten `sensor`. Ein Home-Assistant-Adapter löst daraus die aktuelle Entity-ID auf und erzeugt ein normalisiertes Sample:

```text
GridIntensitySample(
    value_g_co2e_per_kwh,
    observed_at,
    source_registry_id,
)
```

Der Bilanzkern kennt ausschließlich dieses Sample. Damit funktionieren neben Electricity Maps auch Template-Sensoren, lokale Datenquellen und andere Integrationen, sofern sie folgende Bedingungen erfüllen:

- numerischer, endlicher und nichtnegativer Zustand;
- eindeutige Einheit wie `gCO2eq/kWh`, `gCO2e/kWh`, `gCO₂e/kWh` oder eine eindeutig umrechenbare Kilogrammvariante;
- Zeitpunkt der letzten Meldung ist als `State.last_reported` verfügbar, damit auch eine erneut gemeldete, unveränderte Intensität als frisch gilt;
- Zustand ist weder `unknown` noch `unavailable`.

Ein Sensor für CO₂-Raumluftkonzentration in `ppm` ist ausdrücklich inkompatibel. Da Home Assistant für Netz-CO₂-Intensität keine allgemeine passende Device Class garantiert, wird nach Semantik und Einheit validiert, nicht nach einer anbieterbezogenen Integration oder Device Class.

Der Benutzer kann die maximal erlaubte Quellenalterung konfigurieren. Der sichtbare Standard ist `60 Minuten`, der erlaubte Bereich `1` bis `1440 Minuten`. Eine ältere oder ungültige Probe erzeugt keine CO₂-Buchung.

### 4.3 Zeitliche Zuordnung

Ein Energieintervall verwendet die jüngste gültige CO₂-Probe, deren Zeitstempel nicht nach dem Intervallende liegt und deren Alter den konfigurierten Grenzwert nicht überschreitet. Für direkten PV-Verbrauch ist dies die Probe am Verbrauchszeitpunkt. Für Speicherenergie ist es ausdrücklich die Probe am Entladezeitpunkt; eine Netzintensität vom Ladezeitpunkt wird nicht gespeichert.

Eine statische Netzintensität ist im MVP nicht vorgesehen. Die Sensorabstraktion lässt einen späteren konstanten oder historischen Adapter zu, ohne Formeln oder Speicherledger zu ändern.

## 5. Intervallbildung, Plausibilität und Zuordnung

CO2 Saver liest die aktuellen kumulativen Zählerstände ausschließlich an den UTC-Minutenwechseln mit `second = 0` aus dem Home-Assistant-Zustandsspeicher. Der Takt ist damit über Setup, Reload und Neustart hinweg an dasselbe Zeitraster gebunden; verpasste Takte werden weder nachgeholt noch aus aktuellen Zuständen rekonstruiert. `state_changed`- und `state_reported`-Events lösen keine zusätzlichen Messwert-Reads oder Auswertungen aus. Ein kurzzeitig ungültiger Zustand zwischen zwei Taktzeitpunkten ist damit kein beobachteter Eingang; liegt er am Taktzeitpunkt vor, gilt unverändert das fail-closed Verhalten aus Abschnitt 10. Entity-Registry-Lifecycle-Callbacks dienen nur dazu, Umbenennung oder Entfernung einer konfigurierten Quelle gemäß Abschnitt 12.1 kontrolliert zu behandeln, nicht zur Intervallbildung.

Der Runner registriert genau einen UTC-Timer. Beim Unload wird dieser Timer zuerst entfernt; danach wartet der Runner auf seinen je Entry serialisierten Read-/Commit-Lock. Bereits laufende Verarbeitung darf ihren atomaren Commit beenden, aber wartende oder später eintreffende Callbacks lesen und speichern nach gesetztem Stoppzustand nichts mehr. Setup und Reload erzeugen dadurch weder überlappende Takte noch nachlaufende Store-Schreibvorgänge.

Ein Lesevorgang kopiert ohne dazwischenliegendes `await` einen unveränderlichen Vektor aus Wert, deklarierter Einheit, `co2saver_period_end` und `last_reported` jeder konfigurierten Energierolle. Dazu gehört auch ein optionaler PV-Plausibilitätszähler. Home-Assistant-`State`-Objekte werden nicht über diese synchrone Kopiergrenze hinaus gehalten, weil ihr `last_reported` bei einer erneuten identischen Meldung mutieren kann.

Dezimal dargestellte Eingaben werden im Bilanzkern als exakte rationale Zahlen verarbeitet. Untergrenzen werden nie auf- und Belastungsobergrenzen nie abgerundet. Insbesondere wird `rho_pv = F_pv / eta` als gekürztes Zähler-/Nennerpaar persistiert und verglichen; nur die Anzeige rundet.

### 5.1 Frische und synchrone Snapshots

Jedes gelesene Sample wird zuerst für sich kopiert und auf Registry-Bindung, Wert, Einheit, `co2saver_period_end` und `last_reported` geprüft. Messperiodenende und Veröffentlichung dürfen nicht in der Zukunft liegen, das Messperiodenende darf nicht nach `last_reported` liegen und die einzelne Veröffentlichung darf höchstens `60 Sekunden` nach ihrem Messperiodenende liegen. Die Altersprüfung folgt erst nach der Einordnung als Duplikat, alter Baseline-Anteil oder neuer Kandidat, damit sequenzielle Veröffentlichung nicht vorschnell als Lücke gilt.

Danach gilt diese feste Einzel- und Batch-Reihenfolge:

1. Stimmen die aktuellen Samples aller Rollen in Registry-ID, deklarierter Einheit, Wert und bereits akzeptiertem `co2saver_period_end` mit der Baseline überein, ist der Vektor ein idempotentes Duplikat. Neu gemeldete `last_reported`-Werte ändern daran nichts. Solange noch keine Rolle einer neuen Periode vorliegt, darf diese bereits akzeptierte Baseline wegen des zulässigen Fünf-Minuten-Messabstands plus der Publikationsfrist höchstens `360 Sekunden` alt sein; andernfalls beginnt eine Unterbrechung.
2. Ändert sich nach Annahme einer Periode die deklarierte Einheit oder ein Wert für genau denselben `co2saver_period_end`, ist das ein widersprüchlicher Korrektursnapshot und sofort eine Unterbrechung. Meldet eine Rolle für eine strikt neuere Periode eine andere unterstützte Einheit als in ihrer aktiven Baseline, ist dies ebenfalls sofort eine Unterbrechung; die Probe wird nicht gepuffert.
3. Ein Sample mit `co2saver_period_end` vor dem zuletzt akzeptierten Ende ist ein aktiver Zeitstempel-Rollback und ebenfalls sofort eine Unterbrechung. Es wird nie gepuffert; `recovery_after_period_end` bleibt auf dem letzten akzeptierten Ende.
4. Samples mit strikt neuerem `co2saver_period_end` müssen bei ihrer Aufnahme in den Puffer sowohl mit Messperiodenende als auch `last_reported` höchstens `300 Sekunden` alt sein. Sie werden einschließlich ihrer deklarierten Einheit unveränderlich nach Messperiodenende gepuffert. Meldet dieselbe Rolle für dieselbe gepufferte Periode später eine andere Einheit oder einen anderen Wert, ist das ein widersprüchlicher Korrektursnapshot und sofort eine Unterbrechung; weder Erst- noch Letztwert wird bevorzugt. Solange noch Rollen fehlen, bleibt die bisherige Baseline unverändert und es wird nichts gebucht. Ein noch auf der Baseline stehendes Sample darf während dieses offenen, höchstens 60 Sekunden langen Kandidatenfensters älter als 300 Sekunden sein; ein gemischter aktueller State-Vektor aus alter und neuer Periode ist deshalb noch kein Synchronitätsfehler.
5. Ein Kandidaten-Batch ist erst vollständig, wenn jede Rolle dasselbe `co2saver_period_end` besitzt. Dann müssen Messperiodenende und alle `last_reported` beim Lesen höchstens `300 Sekunden` alt, alle `last_reported` gegenüber ihrer Baseline strikt neuer und die Veröffentlichungszeiten untereinander höchstens `60 Sekunden` auseinander sein. Der physische Messperiodenversatz bleibt exakt `0 Sekunden`.
6. Zum vorherigen gemeinsamen Messperiodenende muss der Abstand größer als null und höchstens `900 Sekunden` sein; kein kumulativer Wert darf echt gesunken sein. Erst dann wird der vollständige Batch zum neuen Intervallendpunkt.

Bei jedem Poll werden neue Rollenproben zuerst unveränderlich in den Kandidaten aufgenommen. Ist der Kandidat danach vollständig, wird er anhand seiner gespeicherten Veröffentlichungszeiten und der übrigen Batch-Grenzen geprüft; das gilt auch dann, wenn der Poll mehr als 60 Sekunden nach der frühesten Veröffentlichung ausgeführt wird. Nur wenn der Kandidat danach weiterhin unvollständig ist, greift sein Timeout. So bleibt ein physisch innerhalb des erlaubten Skews vollständig publizierter Batch trotz des Pollrasters auswertbar.

Liegt kein neuer Kandidat vor, macht ein mehr als `360 Sekunden` altes Baseline-Messperiodenende die Zeitreihe stale. Diese Wartefrist ist keine Fristverlängerung für neue Samples: Deren einzelne Publikationsverzögerung bleibt auf 60 Sekunden und deren Alter bei Aufnahme und Batch-Abschluss auf 300 Sekunden begrenzt. Bleibt ein nach dem aktuellen Poll weiterhin unvollständiger Kandidaten-Batch länger als 60 Sekunden nach seinem frühesten `last_reported` unvollständig, wird sein Messperiodenende zu alt oder überspringt eine Rolle die Periode, bevor der Batch vollständig werden kann, liegt ebenfalls eine Unterbrechung gemäß Abschnitt 10 vor. Gepufferte Kandidaten, Baseline und Ledger werden zusammen persistiert, damit ein Neustart weder einen unvollständigen Batch bucht noch ihn mit einer späteren Periode vermischt.

Der gemeinsame Intervallendzeitpunkt ist der identische Wert von `co2saver_period_end`. Er steuert die CO₂-Probenwahl und Segmentgrenze. Energiequellen ohne gemeinsame Erfassungsperiode, ohne erneute Messung mindestens alle fünf Minuten oder mit mehr als 60 Sekunden HA-Veröffentlichungsversatz sind nicht kompatibel.

Home Assistant vergibt `last_reported` je Entity-Schreibvorgang; gemeinsam erfasste Werte dürfen deshalb nacheinander publiziert werden. Der gemeinsame Messzeitstempel darf dagegen nicht aus `last_reported` erraten oder zeitlich gerundet werden. Schon ein positiver physischer Messperiodenversatz würde Deltas über unterschiedliche Zeitfenster vergleichen. Ohne zusätzliche Leistungsobergrenzen könnte die gesamte Energie in den nicht überlappenden Randzeiten liegen; eine positive Teilfluss-Untergrenze wäre dann nicht beweisbar. Historisches Resampling und Leistungsgrenzen sind bewusst kein Bestandteil des kumulativen MVP-Vertrags.

Beim allerersten Baseline-Snapshot gibt es noch keinen akzeptierten Vorgänger. Er muss Form, Frische und Synchronität erfüllen, überspringt aber Delta-, Neuheits-, Dauer- und Monotonieprüfung und bucht nichts. Ein Recovery-Snapshot hat dagegen einen persistierten Vorgängerzeitpunkt: Er muss ein strikt neueres `co2saver_period_end` besitzen und überspringt nur die Intervall-Dauer sowie die Delta- und Monotonieprüfung der Zählerwerte.

### 5.2 Standortbilanz und unbekannte Restflüsse

Für ein akzeptiertes Intervall seien:

- `P` PV-Erzeugung, im Wechselrichterpfad gemessen und im Smartmeterpfad gemäß Abschnitt 3.3 abgeleitet;
- `I` Netzbezug und `D` Speicherentladung als Quellen;
- `L` lokaler Verbrauch, `C` Speicherladung und `X` Netzeinspeisung als Senken.

Alle Größen sind nichtnegative Intervallenergien in `kWh`. Die Bilanzabweichung darf höchstens

```text
tau = max(0,01 kWh, 0,02 * max(P, I, D, L, C, X))
```

betragen. Eine größere Abweichung verwirft das Intervall. Innerhalb der Toleranz wird die kleinere Seite nicht passend gerechnet, sondern um einen unbekannten Fluss ergänzt:

```text
Q = P + I + D
Z = L + C + X
U_quelle = max(Z - Q, 0)
U_senke = max(Q - Z, 0)
T = max(Q, Z)
```

`U_quelle` beziehungsweise `U_senke` nimmt an der Bilanz teil, ist aber nie gutschriftfähig. So kann Messtoleranz eine Garantie nur verkleinern, nie eine zusätzliche Gutschrift erzeugen.

### 5.3 Garantierte Teilflüsse

Die aggregierten Zähler bestimmen im Allgemeinen keine eindeutige Zuordnung zwischen Quellen und Senken. Die augmentierte Quellenmenge ist `{P, I, D, U_quelle}`, die augmentierte Senkenmenge `{L, C, X, U_senke}`; Nullränder dürfen entfallen. Für jede augmentierte Quelle `q` und Senke `z` ist folgende Untergrenze in jeder mit den Messungen vereinbaren Zuordnung garantiert:

```text
LB(q -> z) = max(0, q + z - T)
```

Der MVP verwendet ausschließlich diese Untergrenzen:

```text
E_direkt = LB(P -> L) = max(0, L - I - D - U_quelle)
E_pv_ladung = LB(P -> C)
E_pv_export = LB(P -> X)
E_netz_ladung = LB(I -> C)
E_batterie_lokal = LB(D -> L)
```

Die nicht aufgelösten Ränder bleiben als prüfbare Vertragswerte erhalten:

```text
R_q = q - Summe_{über alle augmentierten z} LB(q -> z)
R_z = z - Summe_{über alle augmentierten q} LB(q -> z)
R_gesamt = T - Summe_{über alle augmentierten q,z} LB(q -> z)
         = Summe_{über alle augmentierten q} R_q
         = Summe_{über alle augmentierten z} R_z
```

Alle Restwerte sind nicht negativ. Insbesondere ist `R_pv_fluss = P - LB(P -> L) - LB(P -> C) - LB(P -> X) - LB(P -> U_senke)`. Der Rest wird weder nach einer künstlichen Priorität verteilt noch später gutgeschrieben. `min(P, L)` reicht nicht als Nachweis für direkten PV-Verbrauch, wenn gleichzeitig Netzbezug und Export vorkommen können. Die garantierten PV-Flüsse zu Last, Speicher und Export sind jeweils getrennte Untergrenzen und überschreiten zusammen weder `P` noch die zugehörigen Senken.

Sind `C > 0` und `D > 0` im selben normalisierten Intervall, ist die Speicherherkunft nicht belastbar. Jede positive Gleichzeitigkeit verwirft deshalb das gesamte Intervall; eine Toleranz darf diesen Herkunftskonflikt nicht verbergen.

## 6. Verbraucherzuordnung

Zuerst wird der lokale Senkenrand in Haus und konfigurierte zusätzliche Verbraucher zerlegt. Im Anteilsmodus erhält jeder zusätzliche Verbraucher einen festen Anteil zwischen `0 %` und `100 %` am gemessenen Gesamtverbrauch; die Summe darf `100 %` nicht überschreiten und der Rest ist Hausverbrauch. Im separaten Modus ist `L_i` das gemessene Intervallinkrement des jeweiligen überschneidungsfreien Zählers. In beiden Modi gilt exakt `L = Summe_i L_i`.

Diese Zerlegung beweist noch nicht, welche Quelle einen einzelnen Verbraucher versorgt hat. Insbesondere darf die systemweit garantierte PV-Menge nicht proportional verteilt werden. Für jeden Verbraucher einschließlich Haus werden deshalb eigene Flussuntergrenzen aus derselben augmentierten Standortbilanz berechnet:

```text
E_direkt_i = LB(P -> L_i) = max(0, P + L_i - T)
E_batterie_lokal_i = LB(D -> L_i) = max(0, D + L_i - T)
E_pv_speicher_lokal_i = max(
    0,
    E_pv_entladung_gesamt + E_batterie_lokal_i - D,
)
                         = max(0, E_batterie_lokal_i - N_hi)
```

`E_pv_entladung_gesamt` und `N_hi` sind dabei die Werte unmittelbar vor dem Entladungsupdate aus Abschnitt 7.3.

Die Summe der einzeln garantierten Flüsse kann kleiner sein als die für die vereinigte lokale Last garantierte Menge. Der nicht individuell beweisbare, aber systemweit weiterhin garantierte Rest wird daher ausdrücklich geführt:

```text
E_direkt_nicht_zuordenbar = E_direkt - Summe_i E_direkt_i
E_pv_speicher_nicht_zuordenbar =
    E_pv_speicher_lokal - Summe_i E_pv_speicher_lokal_i
```

Beide Reste sind nicht negativ und bezeichnen keinen zusätzlichen physischen Verbraucher. Sie verhindern, dass eine sichere Systemgutschrift einem konkreten Verbraucher ohne Beleg zugeschrieben oder stillschweigend verworfen wird. Bei null Gesamtverbrauch sind alle Verbraucher- und Restwerte null.

Direkte Bruttovermeidung und PV-Belastung eines Verbraucher- oder Restbuckets folgen wegen des einheitlichen Intervallfaktors unmittelbar aus seiner garantierten Energie. Für eine Verbraucher-Speicherenergie `e_i = E_pv_speicher_lokal_i` wird die mögliche PV-Belastung dagegen unabhängig konservativ mit der vor der Entladung gültigen Hülle `H(e_i)` angesetzt; Systemledger und Systemergebnis verwenden weiterhin genau einmal `H(E_pv_speicher_lokal)`. Da dieselbe unbekannte hoch belastete Kohorte im jeweils ungünstigsten Fall verschiedene Verbraucher versorgt haben könnte, sind diese verbraucherspezifischen Speicher-Nettozahlen unabhängige konservative Sichten und ausdrücklich nicht additiv. Nur die Systemwerte sind die autoritative Anlagenbilanz; die Energiemengen der Verbraucher plus Zuordnungsrest summieren sich dagegen exakt zu ihr.

## 7. Speicherherkunft und Verluste

Wenn ein Speicher konfiguriert wird, muss der Benutzer zwei Werte sichtbar bestätigen:

- einen AC-Rundtrip-Wirkungsgrad `eta` von mehr als `0 %` bis einschließlich `100 %`; der Config Flow schlägt `90 %` vor;
- die nutzbare Speicherkapazität `K` von `0,1` bis `1000 kWh`, bezogen auf abgebbare Energie am AC-Ausgang; dafür gibt es keinen stillen Standardwert.

Ohne Messung des Ladezustands oder der inneren Durchmischung dürfen Herkunftsbuckets nicht als exakter physischer Inhalt behandelt werden. Der persistente Ledger führt deshalb Schranken in output-äquivalenter AC-Energie:

- `S_lo`: Untergrenze des gesamten Speicherbestands;
- `S_hi`: Obergrenze des gesamten Speicherbestands;
- `P_lo`: garantiert noch vorhandene, gutschriftfähige PV-Energie;
- `N_hi = S_hi - P_lo`: Obergrenze aller nicht gutschriftfähigen Energie, also Netz- und unbekannte Herkunft gemeinsam;
- `B_pv`: verbleibendes konservatives PV-Belastungsbudget in `gCO₂e`;
- `rho_pv`: höchste noch relevante PV-Belastungsdichte in `gCO₂e/kWh` am AC-Ausgang.

Es gelten jederzeit:

```text
0 <= S_lo <= S_hi <= K
0 <= P_lo <= S_lo
N_hi = S_hi - P_lo >= 0
0 <= B_pv <= rho_pv * P_lo
P_lo = 0  =>  B_pv = 0 und rho_pv = 0
```

Die Umkehrung der letzten Implikation gilt wegen des zulässigen Faktors `F_pv = 0` nicht. Eine numerische Toleranz von `10^-12 kWh` dient ausschließlich dazu, intern entstandene Rechenreste konservativ auf null zu klemmen. Beobachtete Energie darf damit weder legitimiert noch passend gemacht werden.

### 7.1 Initialisierung und Quarantäne

Ohne lückenlos bekannte Vorgeschichte darf ein vorhandener Speicher nicht als leer angenommen werden. Bei Erstinitialisierung, jedem unbeobachteten Segmentwechsel, einer unterbrochenen Energiezeitreihe oder anderweitig verlorener Herkunft wird daher gesetzt:

```text
S_lo = 0
S_hi = K
P_lo = 0
N_hi = K
B_pv = 0
rho_pv = 0
```

Der vollständig unbekannte Startbereich ist eine epistemische Obergrenze, keine Behauptung über den realen Ladezustand. Beobachtete Ladung oder Entladung engt diesen Bereich ein. Unbekannte Energie kann nie nachträglich zu PV-Energie werden.

### 7.2 Ladung

Für ein gültiges Ladeintervall werden nur die garantierten PV-Flüsse aus Abschnitt 5.3 verwendet:

```text
A = C * eta
A_pv = E_pv_ladung * eta
B_neu = E_pv_ladung * F_pv_zum_Ladezeitpunkt
rho_neu = F_pv_zum_Ladezeitpunkt / eta   falls A_pv > 0
```

Ist `S_lo + A > K`, widerspricht die Ladung bereits dem garantiert vorhandenen Bestand; das Intervall ist ungültig und der Ledger wird quarantänisiert. Andernfalls beweist die beobachtete Ladung, dass unmittelbar davor mindestens `A` freier Platz vorhanden war. Deshalb gilt:

```text
S_hi_vor = min(S_hi, K - A)
S_lo_neu = S_lo + A
S_hi_neu = S_hi_vor + A
P_lo_neu = P_lo + A_pv
N_hi_neu = S_hi_neu - P_lo_neu
B_pv_neu = B_pv + B_neu
rho_pv_neu = max(rho_pv, rho_neu)        falls A_pv > 0
```

So kann auch ein unbekannter Startbereich durch reale Beobachtungen enger werden, ohne freien Platz zu erfinden. Der nicht garantiert PV-stämmige Anteil von `A` geht ausschließlich in `N_hi_neu` ein. Eine Ladung allein verändert keinen Ergebniszähler.

### 7.3 Entladung und lokale Nutzung

Für `D > S_hi` ist das Intervall ungültig und der Ledger wird quarantänisiert. Eine proportionale Herkunftsentnahme ist ohne Messung der inneren Durchmischung nicht beweisbar und deshalb unzulässig. Die sicher in der Entladung enthaltene PV-Menge und deren sicher lokale Schnittmenge sind stattdessen:

```text
E_pv_entladung_gesamt = max(0, D - N_hi)
E_pv_speicher_lokal = max(
    0,
    E_pv_entladung_gesamt + E_batterie_lokal - D,
)
                       = max(0, E_batterie_lokal - N_hi)
```

Die erste Formel nimmt adversarial an, dass zunächst der gesamte mögliche Nicht-PV-Bestand entladen wurde. Die zweite bildet anschließend die garantierte Schnittmenge aus PV-Herkunft und lokaler Senke. Nur `E_pv_speicher_lokal` ist gutschriftfähig; es gilt stets `E_pv_speicher_lokal <= E_pv_entladung_gesamt <= min(P_lo, D)`.

Für den Folgezustand wird die PV-Garantie so behandelt, als hätte die Entladung sie maximal abgebaut:

```text
S_lo_neu = max(0, S_lo - D)
S_hi_neu = S_hi - D
P_lo_neu = max(0, P_lo - D)
N_hi_neu = S_hi_neu - P_lo_neu
```

Insbesondere ist `P_lo_neu = P_lo - E_pv_entladung_gesamt` unzulässig: Es könnte PV erhalten, die real bereits entladen wurde, und sie später doppelt gutschreiben. Die beschlossene Regel garantiert dagegen `E_pv_speicher_lokal + P_lo_neu <= P_lo`.

### 7.4 Konservative Belastungshülle

Ein gewichteter Mittelwert von `B_pv / P_lo` würde eine homogene Entladung unterstellen. Statt einer unbegrenzt wachsenden Liste von Faktor-Kohorten verwendet der MVP eine konstant große obere Hülle:

```text
H(e) = min(B_pv, rho_pv * e)
B_pv_lokal = H(E_pv_speicher_lokal)
B_pv_neu = min(
    B_pv - B_pv_lokal,
    rho_pv * P_lo_neu,
)
rho_pv_neu = 0 falls B_pv_neu = 0, sonst rho_pv
```

`H(e)` ist die höchste Belastung, die eine beliebige Teilmenge von `e` noch gutschriftfähigen PV-kWh tragen könnte. Dadurch kombiniert die Rechnung eine Untergrenze der Bruttovermeidung mit einer Obergrenze der Herstellungsbelastung. Der verworfene Betrag `B_pv - B_pv_lokal - B_pv_neu` gehört zu PV-Energie, deren Gutschriftfähigkeit durch Export oder Herkunftsunsicherheit endgültig verloren ging. Er kann später weder gebucht noch doppelt verwendet werden. Exakte kohortenspezifische Intervallwerte würden einen unbeschränkt wachsenden Zustand erfordern und sind bewusst nicht Teil des MVP.

Auch bei ungültiger CO₂-Probe werden `P_lo`, `B_pv` und `rho_pv` mit denselben Formeln fortgeschrieben. `B_pv_lokal` wird dann zusammen mit der unbewerteten Energie aus dem Ledger entfernt, aber nicht in einen Emissions-Ergebniszähler gebucht; eine spätere Nachbewertung ist ausgeschlossen.

## 8. Emissionsfaktoren und Formeln

Alle Faktoren sind nichtnegative Werte in `gCO₂e/kWh`. Der zulässige technische Bereich ist `0` bis `5000`. Es gibt keine stillen Projektstandardwerte für PV und Speicher; der Benutzer muss die für seine Anlage passenden Werte explizit bestätigen.

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
PV_Last_speicher = B_pv_lokal_aus_dem_Ledger
Speicher_Last = E_pv_speicher_lokal * F_bat
Netto_speicher = Brutto_speicher - PV_Last_speicher - Speicher_Last
```

Der PV-Herstellungsanteil der garantierten PV-Ladung wird mit seinem Faktor am Ladezeitpunkt in `B_pv` festgehalten und erst bei einer garantiert lokalen PV-Entladung bilanziert. Jede belastete PV-kWh und jede gutschriftfähige Speicher-kWh wird höchstens einmal berücksichtigt. Ein negatives Nettoergebnis wird nicht auf null begrenzt.

## 9. Zahlenbeispiele

### 9.1 Wechselrichter, Gesamtverbrauch und Wallbox-Anteil

In einem Intervall werden `5 kWh` PV-Erzeugung, `4 kWh` lokaler Gesamtverbrauch, `0 kWh` Netzbezug und `1 kWh` Export gemessen. Es gibt keinen Speicher. Damit ist `T = 5 kWh` und `LB(P -> L) = 4 kWh`. Der konfigurierte Verbrauchsanteil bildet `L_wallbox = 1 kWh` und `L_haus = 3 kWh`; daraus folgen in diesem vollständig belegten Sonderfall `LB(P -> L_wallbox) = 1 kWh`, `LB(P -> L_haus) = 3 kWh` und kein Zuordnungsrest. Mit `G = 400 gCO₂e/kWh` und `F_pv = 40 gCO₂e/kWh` gilt:

```text
E_direkt = max(0, 5 + 4 - 5) = 4 kWh
Brutto = 4 * 400 = 1600 gCO₂e
PV-Last = 4 * 40 = 160 gCO₂e
Netto = 1440 gCO₂e = 1,44 kgCO₂e
Wallbox = 1 * (400 - 40) = 0,36 kgCO₂e
Haus = 3 * (400 - 40) = 1,08 kgCO₂e
```

Die verbleibende `1 kWh` PV ist Export und wird nicht gutgeschrieben.

### 9.2 Smartmeter und separate Verbraucher

Haus und Wallbox verbrauchen `2 kWh` und `1 kWh`. Netzbezug ist `1 kWh`, Export `2 kWh`, ein Speicher ist nicht vorhanden:

```text
E_pv = 3 + 2 - 1 = 4 kWh
T = 4 + 1 = 5 kWh
E_direkt = max(0, 4 + 3 - 5) = 2 kWh
E_pv_export = max(0, 4 + 2 - 5) = 1 kWh
```

Eine weitere PV-kWh ist zwischen Last und Export mehrdeutig und bleibt unbewertet. Bei denselben Faktoren wie oben beträgt die System-Nettoersparnis `2 * (400 - 40) = 720 gCO₂e`. Für das Haus ist nur `LB(P -> L_haus) = max(0, 4 + 2 - 5) = 1 kWh` und für die Wallbox `LB(P -> L_wallbox) = max(0, 4 + 1 - 5) = 0 kWh` individuell garantiert. Damit weist die Verbrauchersicht dem Haus `360 gCO₂e` und der Wallbox `0 gCO₂e` aus; die verbleibenden `360 gCO₂e` der Systembilanz erhalten keine Einzelzuordnung und sind über den Energie-Zuordnungsrest von `1 kWh` nachvollziehbar. Eine proportionale Aufteilung von `480/240 gCO₂e` würde der Wallbox eine physisch nicht garantierte PV-Menge zuschreiben.

### 9.3 PV-Speicherzyklus

Der Ledger eines Speichers mit mindestens `3 kWh` Kapazität wurde zuvor durch vollständig beobachtete Entladung auf `S_lo = S_hi = 0` eingeengt. Beim Laden werden `6 kWh` PV, `0 kWh` Netzbezug, `2 kWh` Last, `3 kWh` Speicherladung und `1 kWh` Export gemessen. Alle drei PV-Teilflüsse sind damit garantiert. Bei `eta = 0,9` und `F_pv = 40 gCO₂e/kWh` gelten danach `S_lo = S_hi = P_lo = 2,7 kWh`, `N_hi = 0`, `B_pv = 120 gCO₂e` und `rho_pv = 44,44 gCO₂e/kWh`. Die Speicher-CO₂-Ersparnis bleibt zu diesem Zeitpunkt unverändert.

Später entlädt der Speicher `2 kWh` bei sonst null Quellen vollständig an eine lokale Last von `2 kWh`. Die Netzintensität beträgt dann `500 gCO₂e/kWh` und `F_bat = 20 gCO₂e/kWh`:

```text
Brutto_speicher = 2 * 500 = 1000 gCO₂e
PV_Last_speicher = (120 / 2,7) * 2 = 88,89 gCO₂e
Speicher_Last = 2 * 20 = 40 gCO₂e
Netto_speicher = 871,11 gCO₂e
```

Erst diese Entladung erhöht die Speicherersparnis. Danach gelten `S_lo = S_hi = P_lo = 0,7 kWh`, `N_hi = 0` und `B_pv = 31,11 gCO₂e`.

### 9.4 Gemischte Speicherladung

Ein mit `S_lo = S_hi = 0` leer beobachteter Speicher mit `K >= 3,6 kWh` wird in einem ausgeglichenen Intervall mit `P = 3 kWh`, `I = 1 kWh`, `C = 4 kWh` und sonst null Flüssen geladen. Garantiert sind `3 kWh` PV- und `1 kWh` Netzladung. Bei `eta = 0,9` und `F_pv = 40 gCO₂e/kWh` gelten danach:

```text
S_lo = S_hi = 3,6 kWh
P_lo = 2,7 kWh
N_hi = 0,9 kWh
B_pv = 120 gCO₂e
rho_pv = 44,44 gCO₂e/kWh
```

Bei einer späteren Entladung von `2 kWh`, die garantiert vollständig lokal genutzt wird, sind nur `max(0, 2 - 0,9) = 1,1 kWh` sicher PV-stämmig und lokal; nur sie ist gutschriftfähig. Der Folgezustand ist `S_lo = S_hi = 1,6 kWh`, `P_lo = 0,7 kWh` und `N_hi = 0,9 kWh`.

Würden von derselben `2-kWh`-Entladung nur `1 kWh` garantiert lokal genutzt und `1 kWh` exportiert, wäre die strikte Schnittmenge lediglich `max(0, 1,1 + 1 - 2) = 0,1 kWh`. Deren konservative PV-Belastung betrüge `4,44 gCO₂e`; `B_pv_neu` wäre auf `31,11 gCO₂e` begrenzt. Eine doppelte proportionale Zuordnung würde die Energie überhöhen.

### 9.5 Mehrdeutige Gleichzeitigkeit

Der Smartmeterpfad misst `I = 1 kWh`, `L = 1 kWh`, `C = 1 kWh` sowie `X = D = 0` und leitet daraus `P = L + C + X - I - D = 1 kWh` ab. Sowohl vollständige PV-Last mit Netzladung als auch vollständige Netzlast mit PV-Ladung sind mit diesen Zählern vereinbar. Deshalb gelten `LB(P -> L) = 0` und `LB(P -> C) = 0`. Es entsteht weder direkte Gutschrift noch eine Erhöhung von `P_lo`; die gesamte Ladeenergie wird im Schrankenupdate ausschließlich als nicht PV-fähig behandelt.

## 10. Fehler-, Reset- und Neustartverhalten

- **Unterbrechung der Energiezeitreihe:** Fehlende, alte, asynchrone, nicht numerische oder semantisch falsche Samples, ein Wechsel der deklarierten Energieeinheit, Reset oder Rollover, ein Intervall über 15 Minuten, Bilanzfehler, positive gleichzeitige Ladung und Entladung sowie Ledger-Über- oder -Unterlauf verwerfen das ganze Intervall. Der atomar persistierte Auswertungszustand wechselt von `active` auf `awaiting_rebaseline`; dabei bleibt das letzte akzeptierte Messperiodenende separat als `recovery_after_period_end` erhalten. Ein konfigurierter Speicher wird beim Eintritt genau einmal gemäß Abschnitt 7.1 quarantänisiert und der Diagnosezähler nur einmal erhöht. Ein Recovery-Vektor muss Form, Frische und Synchronität aus Abschnitt 5.1 sowie `co2saver_period_end > recovery_after_period_end` erfüllen. Ältere oder gleichperiodige Replays und Korrekturen halten den Zustand ohne Buchung fail-closed; nur die Dauer seit dem verworfenen Vorgänger und eine Abnahme der kumulativen Zähler werden für den ersten strikt neueren Vektor nicht geprüft. Dieser wird für den vollständigen Pflichtsatz nur als neue gemeinsame Baseline gespeichert und schaltet den persistierten Zustand auf `active`; er bucht nichts, und erst der darauffolgende vollständige Vektor kann wieder ein Intervall bilden. Die Lücke wird nie geschätzt. Ein Neustart in `awaiting_rebaseline` darf weder die alte Baseline reaktivieren noch `recovery_after_period_end` verlieren.
- **Ungültige CO₂-Probe:** Eine fehlende, zukünftige, veraltete oder semantisch ungültige CO₂-Probe unterbricht nicht den physischen Energie- und Herkunftsledger. Das Intervall erhält aber keinerlei Emissionsbuchung. Seine gutschriftfähige Energie wird in getrennten Diagnosezählern für unbewertete direkte und gespeicherte PV-Energie erfasst und nie später mit einem aktuellen Wert nachbewertet.
- **Duplikate und Teilvektoren:** Ein vollständig identischer Snapshot ist idempotent. Ein nur teilweise erneuerter Vektor wird zunächst weder angenommen noch verworfen; erst eine Überschreitung der Grenzen aus Abschnitt 5.1 macht ihn zur Unterbrechung.
- **Home-Assistant-Neustart:** Auswertungszustand, Baselines, `recovery_after_period_end` und gepufferte Kandidaten mit deklarierten Einheiten, Werten, `co2saver_period_end` und `last_reported`, Segmentfingerabdruck, kumulative Emissionskomponenten, Diagnosezähler, `S_lo`, `S_hi`, `P_lo`, `N_hi`, `B_pv`, `rho_pv`, Kapazität und Schemaversion werden als eine atomare Generation persistiert und vor jeder neuen Auswertung wiederhergestellt.
- **Beschädigter, fehlender oder inkompatibler Zustand:** Ein bereits initialisierter Config Entry startet fail-closed gemäß Abschnitt 12. Es werden weder Listener noch Ergebnis-Entities aktiviert und bis zu einer ausdrücklichen Reparatur keine Werte gebucht.

## 11. Änderungen an Konfiguration und Faktoren

Jede fachlich wirksame Konfiguration bildet einen kanonischen, versionierten Segmentfingerabdruck. Er enthält `fingerprint_version`, ADR- und Accounting-Version, Topologie, rollenweise Registry-IDs der Quellen, Verbrauchsmodus, nach Verbraucher-UUID sortierte semantische Zuordnungen, Speicheridentität und -kapazität, `eta`, `F_pv`, `F_bat`, CO₂-Quelle und deren maximale Alterung. Schlüssel und Dezimalzahlen werden kanonisch serialisiert; Anzeigenamen, Listenreihenfolge und aktuelle Entity-IDs sind ausgeschlossen.

Weicht der Fingerabdruck beim Setup ab, startet ein neues Segment. Noch vor jeder Auswertung wird eine einzige neue Store-Revision mit neuem Fingerabdruck, dem UTC-Zeitpunkt `segment_transition_at`, gelöschter alter Baseline und leerem Kandidatenpuffer, Auswertungszustand `awaiting_segment_baseline` und einem gemäß Abschnitt 7.1 quarantänisierten Speicher geschrieben und zurückgelesen. Erst danach darf der Adapter Quellen lesen.

In `awaiting_segment_baseline` werden rollenweise alle Samples mit `co2saver_period_end < segment_transition_at` vor der normalen Kandidatenlogik ignoriert und niemals gepuffert. Nur Samples am oder nach dem Übergangszeitpunkt dürfen den neuen Segmentkandidaten füllen; ein gemischter State-Vektor aus bereits neuer und noch vorsegmentiger Veröffentlichung wartet deshalb zunächst auf die fehlenden Rollen. Wird dieser Kandidat innerhalb von 60 Sekunden nicht als vollständiger synchroner Batch gültig, wird nur der Kandidat verworfen und die Phase bleibt ohne Buchung `awaiting_segment_baseline`; der bereits quarantänisierte Speicher und `segment_transition_at` ändern sich nicht. Der erste vollständige, ansonsten gültige Snapshot mit `co2saver_period_end >= segment_transition_at` wird atomar nur als neue Baseline gespeichert und schaltet auf `active`; das kreuzende Intervall bucht nichts. Ein Absturz nimmt `awaiting_segment_baseline` samt unverändertem Übergangszeitpunkt und Kandidatenpuffer wieder auf und kann nie die alte Baseline oder verspätet publizierte Vorsegmentdaten verwenden.

Dieselbe Übergangsregel gilt für den allerersten Start und nach einer bestätigten Reparatur. Die neu initialisierte Generation enthält bereits vor jedem Quellenlesen `awaiting_segment_baseline` und ihren gespeicherten `segment_transition_at`; dadurch beginnt auch ihre erste buchbare Differenz vollständig nach Installation beziehungsweise Reset.

Ein Ledger darf nur dann erhalten bleiben, wenn eine künftige Implementierung denselben kohärenten Grenzsnapshot noch vollständig und atomar mit der alten Konfiguration verarbeitet; dieser Optimierungspfad gehört nicht zum MVP. Weil im unbeobachteten Intervall reale Entladung und anschließende Netzladung stattgefunden haben könnten, quarantänisiert der MVP den Speicher bei jedem Segmentwechsel.

Faktoren und CO₂-Quellen wirken damit ausschließlich auf klar abgegrenzte zukünftige Intervalle. Nach der neuen Baseline gilt ein neues `eta` nur für neue Ladung, ein neues `F_pv` für direkte Nutzung und neue Ladung, und ein neues `F_bat` für neue Entladungsbuchungen. Wegen der Quarantäne kann kein Bestand mit alten Parametern unter neuen Parametern gutgeschrieben werden. Historische Summen werden nie neu berechnet.

Systemweite Summen bleiben auch bei einer Änderung der Verbraucherstruktur erhalten. Verbraucher besitzen zufällig erzeugte, stabile interne UUIDs: Umbenennen führt dieselbe Zeitreihe fort, Entfernen beendet sie, und ein neu angelegter Verbraucher erhält eine neue Zeitreihe. Historische Verbraucherwerte werden bei einem Topologiewechsel nicht rückwirkend umverteilt.

Diese Segmentierung wird versioniert persistiert. Ein späterer optionaler Rechenlauf über Recorder-Historie kann als separater Adapter ergänzt werden, ohne die gespeicherten Originalsegmente stillschweigend umzuschreiben.

## 12. Identität, Persistenz und Reparatur

### 12.1 Anlagen- und Entity-Identität

Alle ausgewählten Quellen werden als Entity-Registry-ID gespeichert und bei jedem Setup über die öffentlichen Registry-Helfer zur aktuellen Entity-ID aufgelöst. Für jede Quelle registriert der Entry `async_handle_source_entity_changes` mit `helper_config_entry_id=entry.entry_id`, der Registry-ID als `source_entity_id_or_uuid`, `source_device_id=None`, einem dokumentierten No-op-Callback für `set_source_entity_id_or_uuid`, einem Removal-Callback und einem über `entry.async_on_unload` gebundenen Unsubscribe. Eine reine Entity-ID-Umbenennung löst so einen Reload aus, ändert aber weder Segment noch Ledger. Wird ein Registry-Eintrag entfernt oder kann er nicht eindeutig aufgelöst werden, stoppt der Entry und verlangt eine Neukonfiguration.

Beide Topologien verwenden als Anlagenkennung dasselbe physische Netzgrenzenpaar:

```text
plant_key = "grid:<kleinere_registry_id>:<größere_registry_id>"
```

Import- und Exportrolle werden getrennt gespeichert und müssen verschiedene Registry-IDs besitzen; nur für den `plant_key` werden beide IDs lexikografisch sortiert. Dadurch erkennt die Duplikatprüfung auch versehentlich vertauschte Rollen. `plant_key` liegt in den Config-Entry-Daten und wird im User- und Reconfigure-Flow mit `_async_abort_entries_match` geprüft. `ConfigEntry.unique_id` bleibt ungesetzt, weil die Quellen ausdrücklich rekonfigurierbar sind. Ein bewusstes Ersetzen auch nur eines physischen Grenzzählers kann ohne externe Standortkennung nicht automatisch als dieselbe Anlage erkannt werden und gilt daher als neue Anlagenidentität.

Die stabile Eigentümeridentität der fachlichen Zustände und Ergebnis-Entities ist `ConfigEntry.entry_id`, nicht ein Entity-Name oder `plant_key`. Unique IDs verwenden `{entry_id}:{metric}` beziehungsweise `{entry_id}:consumer:{consumer_uuid}:{metric}`. Damit bleiben sie bei erlaubten Umbenennungen stabil. Eine vor der Config-Entry-Erzeugung zufällig erzeugte, unveränderliche `storage_id` dient ausschließlich als persistenter Store-Locator; sie ersetzt die Eigentümer-ID nicht.

### 12.2 Autoritatives Manifest und atomare Generation

Alle Manifestmutationen werden durch einen integrationsweiten asynchronen Lock serialisiert; fachliche Generationsmutationen verwenden zusätzlich einen Lock je `storage_id`. Der Config Flow zieht erst nach allen fachlichen Validierungen und Duplikatprüfungen, aber vor `async_create_entry()`, eine kryptographisch zufällige `storage_id`, `manifest_epoch` und Generation. Innerhalb des Manifest-Locks prüft er die Kandidaten-ID gegen die `storage_id` aller Domain-Entries sowie gegen vorhandene Manifest-, Generations- und `.corrupt.*`-Dateien. Bei jedem Treffer wird eine neue ID gezogen; kein vorhandenes Byte wird geladen, verschoben oder überschrieben. Der Lock bleibt über Prüfung, Bootstrap-Save und Read-back gehalten und bildet damit innerhalb der einzelnen Home-Assistant-Instanz eine Create-if-absent-Reservierung.

Auch die Anlagen-Duplikatprüfung muss parallele User- und Reconfigure-Flows abdecken. Jede Flow-Operation, die einen `plant_key` erstmals setzt oder ändert, reserviert deshalb vor der letzten Duplikatprüfung unter demselben integrationsweiten Lock sowohl ihren kanonischen Ziel-`plant_key` als auch bei Reconfigure die betroffene `entry_id` in flüchtigen Mengen laufender Abschlüsse. Unter dem Lock prüft sie unmittelbar vor dem Commit erneut alle vorhandenen Entries außer dem eigenen Reconfigure-Entry sowie beide Reservierungsmengen. Ein zweiter Flow mit demselben Zielschlüssel oder derselben betroffenen `entry_id` bricht ab, solange die Reservierung besteht. Damit sind User gegen User, User gegen Reconfigure, zwei Reconfigure-Flows verschiedener Entries mit demselben Ziel und zwei Reconfigure-Flows desselben Entries serialisiert.

Bei einem Fehler vor dem jeweiligen Config-Entry-Commit werden alle Reservierungen unter dem Lock entfernt. Nach Rückgabe eines `CREATE_ENTRY`-Ergebnisses bleibt die Zielreservierung bestehen, bis der neue Entry in Home Assistants Config-Entry-Sammlung sichtbar ist, und wird beim ersten Setup entfernt. Bei Reconfigure bleiben Ziel- und Entry-Reservierung bis zur synchron sichtbaren Aktualisierung des vorhandenen Entries bestehen und werden erst danach gelöst. Ein Prozessabsturz verliert nur die flüchtigen Reservierungen: Entweder ist der neue beziehungsweise aktualisierte Entry bereits dauerhaft sichtbar und die normale Duplikatprüfung greift, oder der Commit ist nicht sichtbar und der vorherige Entry-Zustand bleibt autoritativ; bei einem abgebrochenen Create kann höchstens ein ungebundenes, weiterhin kollisionssicheres Waisenmanifest verbleiben. `ConfigEntry.unique_id` bleibt dabei wie in Abschnitt 12.1 festgelegt ungesetzt.

Erst für eine nachweislich freie ID schreibt der Flow das Bootstrap-Manifest `co2saver.<storage_id>.manifest` mit Schema- und Minor-Version, `storage_id`, `manifest_epoch`, `owner_entry_id = null`, `active_generation`, leerer Liste `previous_generations`, `initialized = false` und einer innerhalb dieser Epoche monoton steigenden `commit_revision`. Erst wenn eine frische `Store`-Instanz genau diesen vollständig validierten Payload von der Platte zurückliest, darf der Config Entry angelegt werden. Dessen Daten enthalten `storage_id` als einzigen Store-Locator, aber weder Generation noch Initialisierungsmarker. Scheitert die anschließende Config-Entry-Erzeugung oder stürzt Home Assistant vorher ab, ist das ungebundene Manifest ein ungefährliches Waisenelement, auf das kein Entry verweist und das bei künftigen Reservierungen weiterhin als belegt gilt.

Vor der ersten Bindung prüft das Setup alle Config Entries der Domain. Verweist bereits ein anderer Entry auf dieselbe `storage_id` oder nennt das Manifest einen anderen noch vorhandenen Eigentümer, bleibt es ohne jede Store-Mutation fail-closed. Nur ein kollisionsfreies Manifest mit `owner_entry_id = null` wird durch eine weitere atomare Revision an die tatsächliche `ConfigEntry.entry_id` gebunden; jede spätere Eigentümerabweichung ist ebenfalls fail-closed. Das Manifest ist danach die einzige autoritative Quelle für `active_generation` und `initialized`. Ein verspätet gespeicherter oder nach einem Absturz zurückgerollter Config Entry kann daher keine alte Generation reaktivieren. Ein für einen vorhandenen Config Entry fehlendes, syntaktisch beschädigtes, fremdes oder inkompatibles Manifest ist immer Datenverlust und wird niemals als Erststart interpretiert.

Die aktive fachliche Generation liegt unter `co2saver.<storage_id>.<active_generation>`. Ihr Payload enthält mindestens Schema- und Minor-Version, `storage_id`, Eigentümer-ID, Generation, eine eigene monoton steigende `commit_revision`, Auswertungszustand, Segmentfingerabdruck, `segment_transition_at`, Baselines, `recovery_after_period_end`, Kandidatenpuffer, Ledger, kumulative Ergebnisse, Diagnosen und gegebenenfalls einen Repair-Resetzeitpunkt. Vor dem Laden wird festgehalten, ob der Generation-Key oder eine von Home Assistant erzeugte `.corrupt.*`-Datei existiert. Für ein gültiges Manifest gilt folgende Zustandsmaschine:

- `initialized = false` und nachweislich noch nie vorhandene aktive Generation: Die Generation wird neu initialisiert, gespeichert und zurückgelesen.
- `initialized = false` und vollständig gültige aktive Generation: Genau diese Generation wird nach einem unterbrochenen Erststart oder Repair wieder aufgenommen und niemals überschrieben.
- `initialized = false` und vorhandene, aber ungültige oder als `.corrupt.*` erkannte aktive Generation: fail-closed.
- `initialized = true` und fehlende, beschädigte, fremde oder inkompatible aktive Generation: fail-closed.

Nach erfolgreicher Initialisierung oder Wiederaufnahme setzt eine atomar gespeicherte und zurückgelesene Manifest-Revision `initialized = true`. Erst danach werden Listener und Plattformen aktiviert.

Manifest und Generation verwenden ausnahmslos `Store(..., atomic_writes=True)`; eine Instanz mit Home Assistants nicht atomarem Standard erfüllt diesen Vertrag nicht. `Store.async_save()` meldet Schreib- und Serialisierungsfehler in Home Assistant 2026.9 nicht zuverlässig an den Aufrufer zurück. Deshalb wird nach jedem Save von Manifest oder Generation mit einer frischen, ebenfalls atomar konfigurierten `Store`-Instanz erneut von der Platte geladen und der vollständig validierte Payload samt erwarteter `commit_revision` verglichen. Jede fachliche Zustandsänderung erhöht die Generationsrevision und wird vor ihrer Veröffentlichung ebenso gespeichert und zurückgelesen. Jede Abweichung stoppt weitere Buchungen fail-closed. Ein Absturz nach verifiziertem Generations-Save, aber vor `initialized = true`, ist unschädlich: Das unverändert autoritative Manifest verweist mit `false` auf genau diese gültige Generation, die beim Neustart aufgenommen wird.

Die Messpipeline aus Issue #4 bleibt von dieser Locator- und Eigentümerlogik unabhängig: Reducer, Codec, UTC-Minuten-Runner und Store-Adapter erhalten vollständig validierte Quellenbindungen und den physischen Generation-Key als injizierte Abhängigkeiten. Sie erzeugen weder einen provisorischen Schlüssel aus Entity-ID oder Config-Entry-ID noch interpretieren sie einen fehlenden Store automatisch als Erststart. Die Issues #5 bis #8 bauen einen zusammenhängenden mehrstufigen Config Flow auf; Zwischenstufen erzeugen noch keinen Config Entry und keinen fachlichen Store-Zustand. Erst der vollständige Abschluss in Issue #8 besitzt Messplan, Speicherparameter, Verbrauchszuordnung, CO₂-Quelle und Faktoren und reserviert vor `async_create_entry()` ausschließlich `storage_id`, Generation-ID und das ungebundene Bootstrap-Manifest nach den Regeln dieses Abschnitts. Danach kennt `async_setup_entry()` die endgültige `entry_id`, bindet den Eigentümer, initialisiert oder übernimmt die Generation samt vollständigem Segmentfingerabdruck und `segment_transition_at`, verifiziert sie und setzt das Manifest auf `initialized = true`. Der Runner wird erst von einem atomar mitzukommittenden Auswertungszustand aktiviert: für Einträge ohne Speicher in Issue #9 und für Speichereinträge in Issue #10. Reparatur, Migration und Diagnose bleiben Issue #12 vorbehalten.

Bekannte ältere Manifest- und Generationsschemas werden deterministisch und atomar migriert. Der Migrator lehnt eine höhere Minor-Version bereits vor jedem Speichern ausdrücklich ab; auf den automatischen Schutz nur der Major-Version wird nicht vertraut. Fehlende Daten bei gesetztem Initialisierungsmarker, syntaktisch beschädigte Daten, eine unbekannte Zukunftsversion, falsche Locator-, Eigentümer- oder Generationswerte und verletzte Invarianten werden niemals als leerer Erststart interpretiert. Die betroffenen Daten bleiben read-only am ursprünglichen Key oder bei Syntaxkorruption unter dem von Home Assistant erzeugten `.corrupt.*`-Namen erhalten. Der Entry erzeugt über die Repairs-API eine behebbare Meldung mit Schweregrad Fehler und bricht das Setup mit einem nicht transienten `ConfigEntryError` ohne Listener, Entities oder Buchungen ab.

### 12.3 Kontrollierte Reinitialisierung

Der Reparaturablauf beschreibt Datenverlust und Statistikfolgen, empfiehlt vorab eine Sicherung und verlangt eine ausdrückliche Bestätigung. Abbrechen verändert nichts. Ist das Manifest vollständig gültig, schreibt und verifiziert der Flow nach Bestätigung genau eine neue Manifest-Revision: Die bisher aktive Generation wird an `previous_generations` angehängt, `active_generation` auf eine neue zufällige ID gesetzt und `initialized = false` gespeichert. Die Altgeneration bleibt unverändert zur manuellen Wiederherstellung erhalten.

Fehlt das Manifest oder ist es syntaktisch, semantisch oder versionsbedingt nicht lesbar, wird keine vorhandene Generation erraten oder aktiviert. Vor jedem Ersatz prüft der Flow alle Config Entries der Domain: Verweist ein anderer Entry auf dieselbe `storage_id` oder bezeichnet ein noch vorhandenes Manifest einen anderen, weiterhin existierenden Eigentümer, wird nichts überschrieben; die Reparatur bleibt mit einer Kollisionsdiagnose offen. Andernfalls wird eine noch vorhandene Manifestdatei bytegetreu unter einem eindeutigen, zeitgestempelten Repair-Backup-Namen archiviert und dieses Backup zurückgelesen; eine von `Store` bereits erzeugte `.corrupt.*`-Datei bleibt unangetastet. Danach schreibt und verifiziert der Flow unter derselben `storage_id` ein neues, an den bekannten Entry gebundenes Manifest mit neuer `manifest_epoch`, `commit_revision = 1`, neuer Generation, leerem `previous_generations`, `initialized = false` und einem Diagnosemerkmal für den Manifestverlust. Sämtliche auffindbaren Altgenerationen bleiben unverändert, werden aber mangels beweisbarer Zuordnung nicht automatisch aufgenommen.

Weil Zeiger und Initialisierungszustand in derselben atomaren Manifest-Datei liegen, kann ein Absturz weder einen halben Wechsel noch eine alte Config-Entry-Kopie aktivieren. Nach dem Wechsel initialisiert oder übernimmt das Setup ausschließlich die neue Generation mit Ergebniswerten null, einem Speicher gemäß Abschnitt 7.1, einem persistierten Repair-Resetzeitpunkt sowie `awaiting_segment_baseline` und `segment_transition_at` gemäß Abschnitt 11.

Der Flow wartet mit `await` auf den Entry-Reload und prüft den Zustand `LOADED`. Nur ein Setup, das die neue Generation und anschließend `initialized = true` jeweils gespeichert und zurückgelesen hat, kann diesen Zustand erreichen. Erst danach beendet der Flow sich erfolgreich, wodurch Home Assistant die Reparaturmeldung entfernt; bei jedem Fehler bleibt sie bestehen und der nächste Setup-Versuch setzt die im Manifest ausgewiesene neue Generation fort. Bestehende Entity-Unique-IDs bleiben gleich. Bei `total_increasing` erkennt Recorder den Wertabfall als neuen Zählerzyklus und führt die Langzeitstatistiksumme fort. Die Netto-Entities mit `state_class: total` veröffentlichen den gespeicherten Repair-Zeitpunkt als neues `last_reset`, damit auch dort ein neuer Zyklus statt eines negativen Deltas beginnt. Eine getrennte Statistik-ID wird nicht erzeugt.

## 13. Ergebnis-Entities und Statistiksemantik

Der MVP stellt mindestens folgende kumulative Werte bereit:

| Ergebnis | Einheit | `state_class` | Verhalten |
| --- | --- | --- | --- |
| Brutto vermiedene Netz-Emissionen | `kgCO₂e` | `total_increasing` | Nicht negativ und innerhalb einer Store-Generation monoton. |
| PV-Herstellungsbelastung | `kgCO₂e` | `total_increasing` | Nicht negativ und innerhalb einer Store-Generation monoton. |
| Speicher-Herstellungsbelastung | `kgCO₂e` | `total_increasing` | Nicht negativ und innerhalb einer Store-Generation monoton. |
| Netto-CO₂-Ersparnis gesamt | `kgCO₂e` | `total` | Darf durch negative Intervalle sinken. |
| Netto-CO₂-Ersparnis direkte PV | `kgCO₂e` | `total` | Darf sinken. |
| Netto-CO₂-Ersparnis Speicher | `kgCO₂e` | `total` | Ändert sich nicht beim Laden und darf sinken. |
| Direkt genutzte PV-Energie | `kWh` | `total_increasing` | Innerhalb einer Store-Generation monotoner Transparenzwert. |
| Lokal genutzte PV-Speicherenergie | `kWh` | `total_increasing` | Innerhalb einer Store-Generation monotoner Transparenzwert. |
| Nicht zuordenbare direkte PV-Energie | `kWh` | `total_increasing` | Garantierter lokaler Systemfluss ohne beweisbaren Einzelverbraucher. |
| Nicht zuordenbare lokale PV-Speicherenergie | `kWh` | `total_increasing` | Garantierter lokaler Speicherfluss ohne beweisbaren Einzelverbraucher. |
| Unbewertete direkte PV-Energie | `kWh` | `total_increasing` | Innerhalb einer Store-Generation monotone Diagnose für fehlende CO₂-Proben. |
| Unbewertete PV-Speicherenergie | `kWh` | `total_increasing` | Innerhalb einer Store-Generation monotone Diagnose für fehlende CO₂-Proben. |

Für Haus und jeden zusätzlichen Verbraucher werden die individuell garantierte direkte PV-Energie, lokal genutzte PV-Speicherenergie und die daraus berechnete Netto-CO₂-Ersparnis separat bereitgestellt. Ungerundete direkte beziehungsweise gespeicherte PV-Energien aller Verbraucher plus der jeweilige Zuordnungsrest müssen exakt zum Systemwert summieren. Verbraucherspezifische Speicher-Nettozahlen werden wegen der unabhängigen Belastungsobergrenzen aus Abschnitt 6 nicht summiert; System-Emissionswerte werden ausschließlich aus dem einmal belasteten Systemfluss gebildet.

Alle kumulativen Werte und Ledgerzustände werden restartfest gespeichert. Der einzige erlaubte Wertreset ist die bestätigte neue Store-Generation aus Abschnitt 12.3; ihre Recorder-Semantik ist dort festgelegt. Emissions-Entities verwenden keine unpassende CO₂-Konzentrations-Device-Class. Darstellung rundet nur über Home Assistants Anzeigepräzision; der persistierte Rechenwert bleibt ungerundet.

## 14. Verbindlicher MVP-Umfang

Enthalten sind:

- beide Eingangstopologien aus Abschnitt 3;
- beide Verbrauchsmodi;
- null oder ein Speicher pro Config Entry;
- richtungsgetrennte Import- und Exportzähler sowie alle weiteren Energierollen mit Registry-Eintrag, gemeinsamem `co2saver_period_end`, höchstens fünf Minuten Messabstand sowie höchstens 60 Sekunden HA-Veröffentlichungsverzögerung und -versatz; der MQTT-JSON-Referenzpfad aus Abschnitt 2.1 ist die konkret dokumentierte Quelle;
- explizit bestätigte AC-Ausgangskapazität bei einem Speicher;
- eine frei auswählbare Netz-CO₂-Sensorquelle;
- konstante, explizit bestätigte PV- und Speicherfaktoren;
- prospektive, restartfeste Intervallbilanzierung;
- System- und Verbraucher-Entities aus Abschnitt 13.

Bewusst nicht enthalten sind Leistungssensoren, statische Netzfaktoren, historische Nachberechnung, Exportgutschriften, mehrere Speicher pro Entry, Hybridmischungen der Verbrauchsmodi, Tarife, Kosten, Prognosen, Lade- oder Laststeuerung, Dashboards und externe Telemetrie.

Die Erweiterungspunkte sind absichtlich stabil: Energiequellen liefern normalisierte Intervalle, die CO₂-Quelle liefert `GridIntensitySample`, der Speicher verwendet einen versionierten Herkunftsledger und der Bilanzkern kennt keine anbieterbezogenen Home-Assistant-Integrationen.

## 15. Abdeckung der Entscheidungen aus Issues #1 und #17

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
| Entity-Identität, Store und Reparatur | Abschnitt 12 |
| Ergebniswerte und Langzeitstatistik | Abschnitt 13 |
| MVP und Ausschlüsse | Abschnitt 14 |

## 16. Verbindliche Vertragstests

Die Implementierung muss mindestens folgende benannte Szenarien als Unit-, Integrations- oder Property-Test abdecken:

- Smartmeter mit gleichzeitigem Import und Export schreibt im Beispiel aus Abschnitt 9.2 nur `2 kWh` direkte PV gut;
- im selben Beispiel erhält das Haus nur seine individuell garantierte `1 kWh`, die Wallbox `0 kWh` und der Zuordnungsrest `1 kWh`; eine proportionale Verbraucheraufteilung ist in beiden Verbrauchsmodi verboten;
- der mehrdeutige Smartmeter-Eingangsvektor aus Abschnitt 9.5 leitet `P = 1 kWh` ab, schreibt aber weder direkte PV noch PV-Ladung gut;
- ein positiver Smartmeter-Ladefall mit `L = I = X = D = 0` und `C = 2 kWh` leitet `P = 2 kWh` ab, beweist `LB(P -> C) = 2 kWh` und erhöht bei zuvor bewiesen leerem Speicher `P_lo` genau um `2 * eta`;
- beide Topologien verweigern Betrieb ohne vollständiges Import-/Exportpaar;
- Alter eines neuen Samples bei exakt `300 Sekunden`, seine Publikationsverzögerung und der HA-Veröffentlichungs-Skew bei exakt `60 Sekunden`, physischer Messperioden-Skew bei exakt `0 Sekunden` und Intervalldauer bei exakt `900 Sekunden` sind zulässig; jeder positive Messperioden-Skew und jede andere Grenzüberschreitung unterbricht das Segment;
- ohne neuen Kandidaten bleibt eine akzeptierte Baseline bis einschließlich `360 Sekunden` gültig, damit ein Fünf-Minuten-Messzyklus seine zulässige 60-Sekunden-Publikationsfrist ausschöpfen kann; danach unterbricht sie, während neu eintreffende Samples weiterhin die strengeren Grenzen erfüllen müssen;
- ein atomarer messseitiger JSON-Snapshot wird über die MQTT-Sensor-Konfiguration aus Abschnitt 2.1 in reale sequenzielle HA-State-Writes derselben `co2saver_period_end` überführt und als ein Vektor akzeptiert; gleiche `last_reported` oder timerbasierte Templates ohne gemeinsamen autoritativen Messzeitstempel reichen nicht;
- bei einem Fünf-Minuten-Zyklus darf die erste neue Rolle ein bereits über 300 Sekunden altes Baseline-Sample der noch fehlenden Rollen für höchstens 60 Sekunden überbrücken, ohne vorzeitig zu unterbrechen;
- frische Teilvektoren und frische exakte Duplikate verändern weder Baseline, Ledger noch Summen; ein inzwischen altes Duplikat unterbricht dagegen das Segment;
- eine zweite Meldung derselben Rolle mit gleichem gepuffertem `co2saver_period_end`, aber anderem Wert, verwirft den Kandidaten fail-closed;
- ein aktiver Sample-Zeitstempel vor dem letzten akzeptierten `co2saver_period_end` wird nie gepuffert, löst eine Unterbrechung aus und lässt die Recovery-Schranke auf dem letzten akzeptierten Ende;
- ein Recovery-Snapshot nach Reset darf trotz gesunkenem Stand neue Baseline werden, bucht selbst nichts und macht erst den folgenden vollständigen Vektor auswertbar;
- ein frischer Recovery-Snapshot nach einer Lücke über `900 Sekunden` wird trotz seiner Distanz zur verworfenen Baseline angenommen, bucht selbst aber nichts;
- ein älteres oder gleichperiodiges Replay beziehungsweise eine Korrektur kann in `awaiting_rebaseline` nie Baseline werden; erst ein vollständiger Snapshot strikt nach dem persistierten `recovery_after_period_end` darf trotz gesunkener Zählerstände rebaselinen;
- ein Neustart im persistierten Zustand `awaiting_rebaseline` verlangt weiterhin einen reinen Recovery-Snapshot und kann nicht gegen die alte Baseline buchen;
- ein Neustart mit teilgefülltem Kandidatenpuffer bucht ihn nicht, vermischt ihn nicht mit einer späteren Periode und unterbricht nach unverändertem Timeout oder Periodensprung;
- wird eine Pflicht-Energiequelle `unavailable` oder `unknown` oder liefert sie einen nicht endlichen, negativen, nicht numerischen beziehungsweise semantisch oder von der Einheit her ungültigen Zustand, wird das ganze Intervall ohne Buchung verworfen, Diagnose und Speicherquarantäne erfolgen beim Eintritt genau einmal und erst eine gültige Recovery-Baseline macht Folgeintervalle wieder auswertbar;
- ein kurzzeitiges `unavailable` zwischen zwei 60-Sekunden-Taktzeitpunkten löst keinen Read aus; derselbe Zustand am Taktzeitpunkt unterbricht die Zeitreihe, und weder `state_changed` noch `state_reported` erzeugt einen zusätzlichen Messwert-Read;
- Setup, Reload und Neustart binden den Poll erneut an UTC-Minutenwechsel mit `second = 0`; Unload entfernt den Timer, wartet einen laufenden serialisierten Commit ab und erlaubt danach keinen Read oder Store-Write mehr;
- mehrere während eines Stopps verpasste UTC-Minutenwechsel erzeugen nach dem Neustart keinen Catch-up-Read; genau der nächste reguläre UTC-Minutenwechsel liest wieder einen aktuellen Vektor;
- ein beim ersten Poll unvollständiger Kandidat wird beim nächsten Poll zuerst mit allen inzwischen publizierten Rollen vervollständigt und bleibt bei gespeicherten `last_reported`-Skew von exakt `60 Sekunden` zulässig; nur ein danach noch unvollständiger Kandidat läuft ab;
- ein Wechsel einer Rolle von `Wh` zu `kWh` oder `MWh` unterbricht auch bei rechnerisch konsistentem, monotonem kWh-Stand die Zeitreihe; dasselbe gilt für einen inkonsistent skalierten Wechsel, ohne dass dessen künstliches Delta ausgegeben wird. Der erste strikt neuere vollständige Vektor darf die neue Einheit innerhalb seines unveränderlichen Recovery-Kandidaten übernehmen und bucht nichts;
- ein vollständig konfigurierter Eintrag ohne Speicher aktiviert den UTC-Minuten-Runner erstmals mit der atomaren Direktberechnung aus Issue #9; ein Speichereintrag bleibt dort ohne Timer fail-closed und aktiviert ihn erst mit der gemeinsam persistierten Speicherbilanz aus Issue #10;
- eine CO₂-Probe ist am konfigurierten Maximalalter noch gültig und unmittelbar darüber, bei `observed_at` nach dem Energieintervall, Zustand `unavailable`, nicht numerischem Wert oder inkompatibler Einheit ungültig; das Energieintervall schreibt dann nur die getrennten Unbewertet-Zähler fort, aktualisiert einen betroffenen Speicherledger normal und wird durch eine spätere gültige Probe nie nachbewertet;
- Reset, lange Lücke, Bilanzfehler sowie positive gleichzeitige Ladung und Entladung verwerfen das ganze Intervall und quarantänisieren einen Speicher;
- ein neuer oder quarantänisierter Speicher startet mit `S_lo = P_lo = 0` und `S_hi = N_hi = K`; unbekannte oder netzstämmige Ladung kann nie später PV-Gutschrift erzeugen;
- Kapazitätsgrenzen, Ledger-Überlauf und Entladung über den Bestand scheitern geschlossen;
- der gemischte Ledger aus Abschnitt 9.4 schreibt bei vollständig lokaler `2-kWh`-Entladung nur `1,1 kWh` und bei je `1 kWh` lokaler Nutzung und Export nur `0,1 kWh` PV-Speicherenergie gut; der Folgezustand kann diese PV nicht erneut gutschreiben;
- bei mehreren lokalen Lasten verwendet jede Verbraucher-Speichergutschrift erst `LB(D -> L_i)` und dann die strikte Schnittmenge mit `E_pv_entladung_gesamt`; der Zuordnungsrest schließt exakt zur garantierten Systemenergie auf, während unabhängige `H(e_i)`-Belastungsobergrenzen nicht als additive Systembelastung verwendet werden;
- `F_pv = 1`, `eta = 0,03` und `E_pv_ladung = 0,01` verletzen trotz periodischer Dezimaldarstellung weder Belastungshülle noch Invarianten;
- Zeilen- und Spaltensummen aller garantierten Flüsse bleiben innerhalb ihrer augmentierten Marginalen; `R_q`, `R_z` und `R_gesamt` sind nicht negativ und erhalten einschließlich `U_quelle` oder `U_senke` die Gesamtenergie;
- ein tolerierter Bilanzrest bei `P = 1`, `L = 1`, `X = 0,005` und sonst null erzeugt `U_quelle = 0,005` und nur `0,995 kWh` direkte PV; ein Rest oberhalb `tau` verwirft das Intervall;
- Änderungen von `eta`, `F_pv` und `F_bat` gelten erst nach einer neuen Baseline und quarantänisieren einen vorhandenen Speicherbestand; `B_pv` aus dem alten Segment kann danach keine Buchung mehr erzeugen;
- in `awaiting_segment_baseline` werden Vorsegment-Samples rollenweise vor dem Puffern ignoriert; ein gemischter Vor-/Nach-Übergangsvektor wartet auf den vollständigen neuen Batch, und ein Neustart oder Timeout kann weder alte Rollen übernehmen noch `segment_transition_at` verlieren;
- ein verspätet publizierter vollständiger Batch mit `co2saver_period_end < segment_transition_at` kann weder vor noch nach einem Absturz Segment-Baseline werden; erst ein vollständiger Batch am oder nach dem persistierten Übergangszeitpunkt schaltet auf `active`;
- eine Umbenennung derselben Registry-Quelle erhält Segment und Entity-Identität; doppelte `plant_key` und entfernte Quellen werden abgewiesen;
- liefert der Zufall beim Bootstrap zunächst eine bereits von einem Domain-Entry oder einer Manifest-, Generations- beziehungsweise `.corrupt.*`-Datei belegte `storage_id`, wird unter dem integrationsweiten Manifest-Lock neu gezogen und das bestehende Bytebild samt Revision bleibt unverändert;
- parallele `plant_key`-Commits werden durch Ziel- und Entry-Reservierungen serialisiert: User gegen User erzeugt höchstens einen neuen Entry, User gegen Reconfigure und Reconfigure gegen Reconfigure können weder zwei Entries noch zwei vorhandene Entries demselben Zielschlüssel zuordnen; Fehler lösen ihre Reservierungen, ohne eine dauerhafte `ConfigEntry.unique_id` einzuführen;
- der Config Flow erzeugt einen Entry erst nach verifiziertem Bootstrap-Manifest; die erste Eigentümerbindung lehnt einen zweiten `storage_id`-Referenten ohne Store-Mutation ab; ein gebundenes fehlendes oder beschädigtes Manifest sowie beschädigte, fremde oder zukünftige Major- oder Minor-Versionen aktivieren keine Listener oder Entities;
- Manifest und Generation werden nachweislich mit `atomic_writes=True` geschrieben; simulierte Save-Fehler, Teilstände oder abweichende Read-backs dürfen keine Buchung oder Entity-Veröffentlichung erreichen;
- `initialized = false` nimmt eine bereits gültige aktive Generation wieder auf; `initialized = true` mit fehlender Generation sowie ein Absturz zwischen Generations-Read-back und Manifest-Read-back bleiben fail-closed beziehungsweise eindeutig fortsetzbar;
- nur der bestätigte Reparaturablauf schaltet den atomaren Manifestzeiger auf eine neue Generation; Abbruch oder fehlgeschlagener Reload bewahren Issue und Altgeneration, ein Crash kann die alte Generation nicht reaktivieren, und Erfolg setzt die dokumentierten Recorder-Zyklen;
- bei fehlendem oder ungültigem Manifest archiviert die bestätigte Reparatur vorhandene Rohdaten, aktiviert keine nur vermutete Altgeneration und startet über ein verifiziertes Ersatzmanifest eindeutig neu; eine Kollision mit einem anderen vorhandenen Eigentümer oder derselben `storage_id` wird ohne Überschreiben abgewiesen;
- Property-Tests erhalten `0 <= S_lo <= S_hi <= K`, `0 <= P_lo <= S_lo`, `N_hi = S_hi - P_lo`, `0 <= B_pv <= rho_pv * P_lo`, die Nullimplikation aus Abschnitt 7, nicht negative Zuordnungsreste, Energie-Summengleichheit aus Verbraucheruntergrenzen plus Rest und das Verbot jeder Gutschrift ohne garantierten PV-zu-Last-Fluss.

## 17. Versionshistorie

| Version | Status | Änderung |
| --- | --- | --- |
| 1.0 | Ersetzt | Erste Entscheidung aus #1 mit optimistischer Last-vor-Speicher-vor-Export-Priorität und unvollständigem Wiederherstellungsvertrag. |
| 2.0 | Ersetzt | Korrektur aus #17: garantierte System- und Verbraucherflüsse, synchroner MQTT-Referenzpfad, Frische- und Segmentgrenzen, unbekannter Speicherbestand, kapazitätsgebundener Belastungsledger sowie manifestgestützte Identität und fail-closed Reparatur. |
| 2.1 | Gültig | Präzisierung aus #20: ausschließlich 60-sekündliches Polling für Energiezustände, konservative Rebaseline bei jedem Einheitenwechsel und klare Injektionsgrenze zwischen Messpipeline und Config-/Store-Bootstrap. |
