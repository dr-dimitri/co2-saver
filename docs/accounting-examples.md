# So berechnet CO2 Saver die Ersparnis

CO2 Saver zählt nur PV-Energie, deren Nutzung durch lokale Verbraucher aus den
Messwerten sicher hervorgeht. Von den dadurch vermiedenen Netz-Emissionen zieht
die Integration die Herstellungsbelastung von PV und gegebenenfalls Speicher
ab. Die folgenden Beispiele verwenden dieselben Zahlen wie die bestehenden
Regressionstests und gelten für beide unterstützten Messtopologien.

Die Tabellen zeigen **Energie innerhalb eines Messintervalls**, also die
Differenz zweier gültiger Zählerstände, in `kWh`. Faktoren stehen in
`gCO₂e/kWh`, die Rechnung in `gCO₂e`. Die Ergebnis-Sensoren zeigen Emissionen
in `kgCO₂e`; `1000 gCO₂e = 1 kgCO₂e`. Intern bleiben auch Brüche exakt;
Dezimalnäherungen hier dienen nur dem Lesen. Die Faktoren sind Beispielwerte,
keine Empfehlung für eine konkrete Anlage.

## Direkte PV-Nutzung: 0,72 kgCO₂e

Es gibt keinen Speicher. Die Zähler liefern:

| Energiefluss | kWh |
| --- | ---: |
| PV-Erzeugung | 4 |
| Netzbezug | 1 |
| Gesamte lokale Last | 3 |
| Export ins Netz | 2 |

Die Bilanz stimmt: `4 + 1 = 3 + 2`. In der Smartmeter-Topologie wird die
PV-Erzeugung aus `3 + 2 - 1 = 4 kWh` hergeleitet. Von den `3 kWh` Last kann
höchstens `1 kWh` aus dem Netz stammen. Damit sind **mindestens 2 kWh direkte
PV-Nutzung** bewiesen. Die Zähler zeigen nicht, ob auch die dritte Last-kWh
aus PV stammt; diese mögliche Mehrmenge wird nicht gutgeschrieben.

Bei einer gültigen aktuellen Netzintensität von `400 gCO₂e/kWh` und einem
PV-Herstellungsfaktor von `40 gCO₂e/kWh` ergibt sich:

```text
Brutto vermiedene Netz-Emissionen = 2 × 400 = 800 gCO₂e
PV-Herstellungsbelastung          = 2 × 40  =  80 gCO₂e
Nettoersparnis                   = 800 - 80 = 720 gCO₂e = 0,72 kgCO₂e
```

Werden Haus und Wallbox separat mit `2 kWh` und `1 kWh` gemessen, sind für
das Haus nur `2 - 1 = 1 kWh` PV und für die Wallbox `0 kWh` PV einzeln
garantiert: Der Netzbezug könnte jeweils diesen Verbraucher versorgt haben.
Die weitere systemweit sichere PV-kWh bleibt als **Zuordnungsrest** erhalten.
Haus `1` + Wallbox `0` + Rest `1` ergeben weiterhin `2 kWh`; das
Systemergebnis wird nicht vermindert oder proportional neu verteilt.

Nachweis: `test_full_runtime_matrix_books_exact_system_consumers_and_remainder`
in [test_runtime.py](../tests/test_runtime.py), insbesondere die Variante
`separate`, sowie Abschnitt 9.2 der
[Bilanzierungsregeln](decisions/0001-accounting-and-input-contract.md).

## Ein PV-Speicherzyklus: erst bei Entladung zählen

Der Speicher hat `10 kWh` nutzbare Kapazität. Vor der Rechnung ist sein leerer
Zustand durch vollständig beobachtete Entladung bewiesen; ein Erststart allein
reicht dafür nicht. Der Wirkungsgrad beträgt `90 %`, der PV-Faktor
`40 gCO₂e/kWh` erzeugter PV-Energie und der Speicherfaktor
`20 gCO₂e/kWh` anrechenbarer lokaler PV-Entladung.

Beim Laden entstehen `6 kWh` PV. Davon gehen `2 kWh` direkt an lokale Lasten,
`3 kWh` in den Speicher und `1 kWh` ins Netz. Netzbezug und Entladung sind
null, daher sind diese PV-Pfade eindeutig. Die direkte Nutzung ergibt bei
`G = 400` wieder `720 gCO₂e`. **Die Ladung erzeugt keine Speicherersparnis.**

Der Speicher hält anschließend `3 × 0,9 = 2,7 kWh` nachgewiesene PV-Energie.
Die `0,3 kWh` Ladeverluste werden nie als nutzbare Energie gutgeschrieben.
Mitgeführt werden aber die Herstellungsemissionen der gesamten dafür
eingesetzten PV-Erzeugung: `3 × 40 = 120 gCO₂e`. So verschwinden die
Herstellungsemissionen der Ladeverluste nicht aus der Rechnung.

Später liefert der Speicher `2 kWh` vollständig an lokale Lasten. Weitere
Quellen und Export sind null. Bei der **jetzt** gültigen Netzintensität
`G = 500 gCO₂e/kWh` gilt:

```text
Brutto vermiedene Netz-Emissionen = 2 × 500 = 1000 gCO₂e
PV-Herstellungsbelastung          = 2 × (40 / 0,9) = 800/9 gCO₂e
Speicher-Herstellungsbelastung    = 2 × 20 = 40 gCO₂e
Speicher-Nettoersparnis          = 1000 - 800/9 - 40
                                = 7840/9 gCO₂e ≈ 0,87111 kgCO₂e
```

Der Quotient `40 / 0,9` gilt hier für die einzige, vollständig bekannte
PV-Ladung. Er ist keine Annahme über die Durchmischung verschiedener
Speicherherkünfte. Im Speicher verbleiben `0,7 kWh` PV mit
`280/9 gCO₂e` noch nicht verbuchter PV-Belastung.

Auch die restlichen `0,7 kWh` werden anschließend bei `G = 500` vollständig
lokal entladen. Damit ist der Zyklus abgeschlossen:

| Speicherergebnis, kumulativ | Nach 2 kWh Entladung | Nach weiteren 0,7 kWh |
| --- | ---: | ---: |
| Anrechenbare PV-Energie in kWh | 2 | 2,7 |
| Brutto vermieden in gCO₂e | 1000 | 1350 |
| PV-Herstellungsbelastung in gCO₂e | 800/9 | 120 |
| Speicher-Herstellungsbelastung in gCO₂e | 40 | 54 |
| Speicher-Netto in gCO₂e | 7840/9 | 1176 |

Für den vollständigen Zyklus wurden `2,7 × 500 - 120 - 54 = 1176 gCO₂e`
Speicherersparnis erzielt. Zusammen mit den separaten `720 gCO₂e` aus
direkter Nutzung sind das **1,896 kgCO₂e**. Die Energie schließt ebenfalls:
`6 = 2 direkt + 2,7 aus dem Speicher + 1 Export + 0,3 Verlust`.
Keine der `3 kWh` Ladeenergie wurde zugleich als direkte PV-Nutzung gezählt.

Nachweis: `test_reference_pv_cycle_books_only_delivered_energy_and_exact_burdens`
in [test_storage_runtime.py](../tests/test_storage_runtime.py) und Abschnitt 9.3
der [Bilanzierungsregeln](decisions/0001-accounting-and-input-contract.md).

## Netzladung, Export und unbekannte Herkunft

Reine Netzladung erzeugt keinen PV-Nachweis. Bei gemischter oder unbekannter
Herkunft kann eine spätere lokale Entladung auch Nicht-PV-Energie enthalten.
CO2 Saver zieht deshalb zuerst die **maximal mögliche Nicht-PV-Menge** von
der garantiert lokalen Entladung ab. Nur ein positiver Rest ist als PV
anrechenbar. Ein prozentualer PV-Anteil des Speicherinhalts wird nicht
unterstellt.

Export wird weder bei direkter PV-Nutzung noch bei Speicherentladung
gutgeschrieben. Der Herkunftsnachweis muss dennoch auch um entladene Energie
sinken, deren lokale PV-Nutzung nicht beweisbar ist. Diese Energie darf später
nicht ein zweites Mal auftauchen. Die ausführliche
[Mehrzyklusreferenz mit Mischladung, Export und Neustart](accounting-reference.md)
zeigt dies mit exakten Zahlen und erklärt auch die konservative Begrenzung
der mitgeführten PV-Herstellungsbelastung.

## Wenn die aktuelle CO₂-Probe fehlt

Ein gültiges Energieintervall kann ohne gültige aktuelle Netz-CO₂-Probe
physisch erfasst werden. Dann steigen die nachgewiesenen PV-Energiemengen und
die zugehörigen **unbewerteten Energiemengen**, aber keine der
Emissionskomponenten. Bei einer Entladung werden Energie und mitgeführte
PV-Belastung trotzdem aus dem Speicherbestand entfernt. Eine spätere gültige
Probe bewertet das frühere Intervall nicht nachträglich. Allein eine fehlende
CO₂-Probe löst keine Speicherquarantäne aus.

Beispiel aus dem Speicherzyklus: Fehlt die Probe für die erste `2-kWh`-Entladung,
bleiben deren Speicher-Emissionen null und `2 kWh` werden als unbewertet
erfasst. Sind nur für die abschließenden `0,7 kWh` wieder gültige
`500 gCO₂e/kWh` vorhanden, werden ausschließlich `350 gCO₂e` brutto,
`280/9 gCO₂e` PV-Belastung und `14 gCO₂e` Speicherbelastung gebucht.
Die ersten `2 kWh` bleiben unbewertet.

Maßgeblich ist die aktuelle Probe des Verarbeitungspolls, geprüft gegen das
physische Energieintervallende. Sie darf weder später als dieses Ende liegen
noch das konfigurierte Höchstalter überschreiten. Alte, fehlende oder ungültige
Proben werden nicht durch einen früher gelesenen Wert ersetzt. Die betroffenen
Ergebnis-Sensoren melden `unavailable`, bis die aktuellen Daten wieder gültig
sind; die gespeicherten Summen bleiben erhalten.

Nachweis: `test_missing_current_grid_consumes_provenance_without_later_revaluation`
in [test_storage_runtime.py](../tests/test_storage_runtime.py).

## Erststart, Unterbrechung und Konfigurationsänderung

Bei der ersten Einrichtung ist der Speicherinhalt unbekannt. CO2 Saver startet
deshalb in **Quarantäne**: Der reale Inhalt könnte zwischen null und der
nutzbaren Kapazität liegen; garantiert anrechenbare PV ist zunächst null.
Beobachtete Ladung und Entladung engen diese Grenzen ein. Unbekannte Energie
wird nie nachträglich zu nachgewiesener PV. Die Quarantäne steuert keine
Geräte und fordert keine automatische Entladung an.

Fehlt dagegen ein notwendiger **Energiemesswert**, fällt ein Zähler zurück
oder ist die Energiebilanz ungültig, wird das gesamte betroffene Intervall
verworfen. Auch eine gleichzeitig mögliche direkte PV-Gutschrift entfällt.
Die Speicherherkunft geht erneut in Quarantäne. Der nächste gültige gemeinsame
Messstand bildet nur eine neue Ausgangsbasis; erst das darauffolgende gültige
Intervall kann wieder gebucht werden. Die Messlücke wird nicht geschätzt.

Fachliche Änderungen wie neue Faktoren, eine andere CO₂-Quelle oder geänderte
Verbrauchsanteile beginnen ein neues **Bilanzsegment**: eine neue Messbasis
und erneut unbekannte Speicherherkunft. Die Änderungen wirken ausschließlich
auf zukünftige Intervalle. Ein bloßes Umbenennen eines Verbrauchers beginnt
kein neues Segment.

**Bereits verbuchte Summen bleiben bei Zählerreset, Unterbrechung und
Segmentwechsel unverändert.** Auch Neustart und Reload übernehmen den
gespeicherten Messzustand, die Summen und den Herkunftsnachweis. Ein Neustart
allein löst keine Quarantäne aus; eine währenddessen entstandene ungültige
Messlücke kann dies jedoch tun. Nur ein ausdrücklich bestätigter
[Reparatur-Reset](../README.md#betrieb-diagnose-und-reparatur) beginnt eine neue
Ergebnisgeneration bei null. Ein negatives Nettoergebnis aus einem späteren
gültigen Intervall bleibt möglich und wird nicht auf null begrenzt.

Die geprüften Regeln stehen in den Abschnitten 7, 10 und 11 der
[Bilanzierungsregeln](decisions/0001-accounting-and-input-contract.md).
