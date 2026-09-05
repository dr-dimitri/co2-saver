# CO2 Saver for Home Assistant

CO2 Saver berechnet die CO₂-Ersparnis durch selbst erzeugten PV-Strom, der im
Haushalt oder in konfigurierten zusätzlichen Verbrauchern genutzt wird. Die
Custom Integration liest vorhandene Home-Assistant-Energiesensoren und zeigt
Energie, Bruttovermeidung, Herstellungsbelastung und Nettoersparnis getrennt an.

**Version 0.1.0:** Die erste MVP-Version. Die [Release Notes](docs/releases/0.1.0.md)
beschreiben den geprüften Umfang und seine Messvoraussetzungen.
Geprüft mit Home Assistant **2026.9.0** und Python **3.14.7**; die technische
Mindestbasis ist Home Assistant 2026.9.0 mit Python 3.14.2.

## Installation und erster Start

1. `co2saver-0.1.0.zip` aus [Release v0.1.0](https://github.com/dr-dimitri/co-saver/releases/tag/v0.1.0)
   herunterladen, entpacken und den
   vollständigen Ordner `custom_components/co2saver` in das Konfigurationsverzeichnis
   von Home Assistant kopieren. Bei Home Assistant OS lautet der Zielpfad
   `/config/custom_components/co2saver`. Dort muss `manifest.json` direkt liegen;
   keinen zusätzlichen Repository-Ordner dazwischen ablegen. Den GPLv3-Lizenztext
   aus `LICENSE` mit dem Quellpaket aufbewahren.
2. Home Assistant neu starten. Die Quellintegrationen müssen bereits eingerichtet
   sein und den unten beschriebenen Messvertrag erfüllen.
3. **Einstellungen → Geräte & Dienste → Helfer → Helfer erstellen → CO2 Saver**
   öffnen. Alternativ startet [CO2 Saver einrichten](https://my.home-assistant.io/redirect/config_flow_start/?domain=co2saver)
   denselben Config Flow in der eigenen Home-Assistant-Instanz.
4. Messtopologie, Speicher vorhanden/abwesend, Verbrauchsmodus, zusätzliche
   Verbraucher und CO₂-Faktoren vollständig durchgehen. Die
   [Feldreferenz](docs/configuration.md) beschreibt jede Eingabe und Bestätigung.
   Erst der Abschluss speichert die Anlage; Abbrechen verwirft den Entwurf.
5. Die erste gültige gemeinsame Messung abwarten. Der erste Messvektor bildet die
   Baseline, das folgende vollständige Intervall kann eine Ersparnis liefern.
   Die Auswertung läuft an UTC-Minutenwechseln. Vor gültigen Beobachtungen sind
   die Ergebnis-Sensoren `unavailable`.

Die Einrichtung und spätere Änderungen erfolgen in der Oberfläche; für CO2 Saver
ist kein YAML-Eintrag nötig. Über die Einstellungen des vorhandenen Helfers lassen
sich Verbraucher und Faktoren ändern. **Neu konfigurieren** im Eintragsmenü
öffnet zusätzlich Messtopologie, Quellen und Speicher. Beide Wege gelten nur für
zukünftige Messabschnitte. Deutsch und Englisch sind vollständig übersetzt.

Bei einem Update zuerst Home Assistant sichern, anschließend den Integrationsordner
vollständig durch die neue Version ersetzen und Home Assistant neu starten.
Die vorhandene Anlage weiterverwenden: Löschen und erneutes Anlegen erzeugt eine
neue Bilanzidentität. Der normale Neustart übernimmt kompatible gespeicherte
Summen und Speicherherkunft. Bei einer unbekannten oder beschädigten Version
bleibt der betroffene Eintrag angehalten und zeigt eine Reparaturmeldung.

Zum Entfernen den CO2-Saver-Eintrag in Home Assistant löschen, danach bei Bedarf
nur dessen Integrationsordner entfernen und neu starten. Aufbewahrte Bilanzdateien
und vorhandene Recorder-Historie werden von CO2 Saver nicht automatisch gelöscht.
Manuelle Wiederherstellung oder Bereinigung alter Daten erst nach einer Sicherung
vornehmen; keine Bilanzdateien anderer Anlagen verändern.

## Passt die Messung zur Anlage?

CO2 Saver benötigt **richtungsgetrennte kumulative AC-Energiezähler**. Jeder
Eingang muss ein registrierter `sensor` mit `device_class: energy`,
`state_class: total` oder `total_increasing` und `Wh`, `kWh` oder `MWh` sein.
Momentanleistung in W/kW und vorzeichenbehaftete Nettozähler erfüllen diesen
Vertrag nicht.

Alle benötigten Energiequellen müssen dieselbe echte physische Erfassungsperiode
belegen: Ihr State-Attribut **`co2saver_period_end`** muss exakt dasselbe messseitig
erzeugte UTC-Periodenende enthalten. Eine gemeinsame Nachricht mit atomarem
Messsnapshot ist ein möglicher Quellpfad. Nachträglich gerundete HA-Zeitstempel
oder Empfangszeiten beweisen keine Gleichzeitigkeit. Jede Rolle muss spätestens
alle fünf Minuten neu gemessen und spätestens 60 Sekunden nach Periodenende
veröffentlicht werden; die Veröffentlichungen dürfen höchstens 60 Sekunden
auseinanderliegen. Ohne diesen Nachweis ist die Anlage für den MVP nicht geeignet.

Ein konkretes MQTT-Quellmuster und die exakten Bedingungen stehen im
[Messvertrag](docs/decisions/0001-accounting-and-input-contract.md#21-konkreter-synchroner-quellpfad).
MQTT ist dabei ein Beispiel für die vorgelagerte Quelle; CO2 Saver liest deren
vorhandene HA-Zustände und richtet selbst keine Geräte oder Datenverbindung ein.

| Vorhandene Messung | Topologie und benötigte Quellen |
| --- | --- |
| Eigene kumulative PV-Erzeugung vorhanden | **Wechselrichter:** PV-Erzeugung, Netzbezug, Netzeinspeisung und lokale Verbrauchsmessung. |
| PV aus der vollständigen Standortbilanz ableitbar | **Smartmeter:** Netzbezug, Netzeinspeisung und lokale Verbrauchsmessung; ein optionaler PV-Zähler dient nur der Plausibilitätsprüfung. |
| Zusätzlich ein Speicher vorhanden | In beiden Topologien getrennte AC-Lade-/Entladezähler, nutzbare Kapazität und bestätigter Wirkungsgrad. |

Der lokale Verbrauch enthält weder Speicherladung noch Netzeinspeisung. Für Haus
und Wallbox gibt es zwei ausdrücklich getrennte Modelle:

| Messanordnung | Verbrauchsmodus |
| --- | --- |
| Der Gesamtzähler enthält Haus und Wallbox | **Gesamtverbrauch mit Anteilen:** Ein Zähler misst die gesamte lokale Last. Beispielsweise 25 % Wallbox, 75 % Resthaushalt. Ohne Zusatzverbraucher gehört die gesamte Last zum Haushalt. |
| Der Hauszähler schließt die separat gemessene Wallbox aus | **Separate Verbraucher:** Haus- und Wallboxzähler werden addiert. Beispielsweise 3 kWh Haus plus 1 kWh Wallbox ergeben 4 kWh lokale Last. |

Ein bereits im Hauszähler enthaltener Wallboxverbrauch darf nicht nochmals als
separate Last hinzugerechnet werden. Namen von Sensoren können diese physische
Abgrenzung nicht beweisen; die Oberfläche verlangt eine ausdrückliche Bestätigung.
Alle Felder, zulässigen Werte und Änderungswege stehen in der
[Konfigurationsreferenz](docs/configuration.md).

## Wie die Ersparnis entsteht

**Direkte PV:** Nur garantiert an lokale Lasten gelieferte PV-Energie erhält eine
Gutschrift. Netzexport erhält keine Ersparnis. Für direkte Energie `E` gilt:

```text
Bruttovermeidung = E × aktuelle Netz-CO₂-Intensität
PV-Herstellungsbelastung = E × PV-Herstellungsfaktor
Nettoersparnis = Bruttovermeidung - PV-Herstellungsbelastung
```

**Speicher:** Eine Ladung erzeugt noch keine Ersparnis. Der Speicher führt
konservative Grenzen für Bestand und nachgewiesene PV-Herkunft. Ladeverluste
verringern den verfügbaren Bestand; die zugehörige PV-Herstellungsbelastung bleibt
bis zur anrechenbaren Entladung aufgeschoben. Erst garantiert PV-stämmige Energie
an eine lokale Last wird mit der aktuellen Netzintensität der Entladung bewertet.
Die zugehörige PV-Belastung einschließlich Ladeverlusten und der Speicherfaktor
werden dabei genau einmal abgezogen.

Die Faktoren werden in `gCO₂e/kWh` eingegeben: PV pro erzeugter AC-kWh, Speicher
pro anrechenbarer lokaler PV-Entlade-kWh. Dafür gibt es keine vorbelegten Werte.
Nettoergebnisse dürfen negativ sein. Netzladung und unbekannter Anfangsbestand
werden nicht als PV behandelt; es wird keine gleichmäßige innere Mischung
unterstellt. Ohne Herkunftsnachweis bleibt ein möglicher Nutzen unberücksichtigt.

Die [einfachen Rechenbeispiele](docs/accounting-examples.md) zeigen direkte Nutzung
und einen vollständigen PV-Speicherzyklus mit denselben Zahlen wie die Tests.
Die [mehrzyklische Referenzrechnung](docs/accounting-reference.md) zeigt zusätzlich
Mischladung, Restbestand, Export und einen Neustart. Der vollständige fachliche
Vertrag steht in [ADR-0001 Version 2.2](docs/decisions/0001-accounting-and-input-contract.md).

### Datenlücken und Änderungen

Jeder reguläre Minutenpoll liest ausschließlich die **aktuelle** Netz-CO₂-Probe.
Ihr `last_reported` darf nicht nach dem physischen Ende des Energieintervalls
liegen, und ihr Alter darf die konfigurierte Grenze nicht überschreiten. Eine
CO₂-Konzentration in ppm ist keine Emissionsintensität.

Fehlt eine gültige aktuelle CO₂-Probe, wird die belegte Energie als dauerhaft
unbewertet erfasst. Es gibt weder eine Emissionsbuchung noch einen Rückgriff auf
ältere Proben oder eine spätere Nachbewertung. Die physische Speicherherkunft
wird trotzdem fortgeschrieben; verbrauchte Herkunft und ihre aufgeschobene
Belastung können nicht später nochmals gutgeschrieben werden.

Ein fehlender Energieeingang, Zählerreset, Einheitenwechsel oder eine Datenlücke
führt zur konservativen neuen Baseline; das überbrückende Intervall wird nicht
geschätzt. Beim Erststart, einer unterbrochenen Energiezeitreihe und einer
fachlichen Konfigurationsänderung beginnt der Speicher mit unbekannter Herkunft
und einer möglichen Füllung zwischen null und seiner nutzbaren Kapazität.
Beobachtete Ladung und Entladung engen diese Grenzen anschließend wieder ein.
Gleichzeitige positive Ladung und Entladung sowie widersprüchliche Flüsse
verwerfen das gesamte Intervall, einschließlich direkter PV-Nutzung.

Bereits gebuchte Summen bleiben bei Quellenresets, Reloads, Neustarts und
Faktoränderungen erhalten. Ein normaler Neustart bewahrt auch kompatible
Speicherherkunft. Fachliche Änderungen werden prospektiv in einem neuen Segment
verarbeitet und quarantänisieren die bisherige Herkunft; ein reines Umbenennen
ändert die Bilanz nicht. Verpasste Messtakte werden nicht nachgeholt.

## Ergebnis-Sensoren

Jede Anlage erhält zwölf kumulative System-Sensoren. Die Emissionswerte werden
in `kgCO₂e` und die Energiewerte in `kWh` veröffentlicht. Dieselben Sensoren
existieren auch ohne Speicher; bei gültigen Messungen bleiben die reinen
Speicherwerte dann null:

| Sensor | Einheit | Bedeutung |
| --- | --- | --- |
| Netto-CO₂-Ersparnis | `kgCO₂e` | Summe aus direkter PV- und Speicher-Nettoersparnis. |
| Netto-CO₂-Ersparnis direkte PV | `kgCO₂e` | Direkte Bruttovermeidung abzüglich der zugehörigen PV-Herstellungsbelastung. |
| Netto-CO₂-Ersparnis Speicher | `kgCO₂e` | Vermeidung durch lokale PV-Speicherentladung abzüglich PV- und Speicher-Herstellungsbelastung. |
| Brutto vermiedene CO₂-Emissionen | `kgCO₂e` | Vermiedene Netz-Emissionen aus direkter Nutzung und Speicherentladung, vor Herstellungsbelastungen. |
| PV-Herstellungsbelastung | `kgCO₂e` | Einmal gebuchte PV-Belastung beider Nutzungspfade; beim Speicher einschließlich Ladeverlusten. |
| Speicher-Herstellungsbelastung | `kgCO₂e` | Einmal gebuchte Herstellungsbelastung der anrechenbaren lokalen Speicherentladung. |
| Direkt genutzte PV-Energie | `kWh` | Garantierte direkte PV-Nutzung am Standort. |
| Lokal genutzte PV-Speicherenergie | `kWh` | Garantiert PV-stämmige Speicherentladung an lokale Lasten. |
| Nicht zuordenbare direkte PV-Energie | `kWh` | Garantierte direkte Systemenergie ohne beweisbaren Einzelverbraucher. |
| Nicht zuordenbare PV-Speicherenergie | `kWh` | Garantierte lokale Speicher-PV-Energie ohne beweisbaren Einzelverbraucher. |
| Unbewertete direkte PV-Energie | `kWh` | Bereits physisch erfasste direkte PV-Energie ohne gültige aktuelle CO₂-Probe. |
| Unbewertete PV-Speicherenergie | `kWh` | Bereits physisch erfasste lokale Speicher-PV-Energie ohne gültige aktuelle CO₂-Probe. |

Haushalt und jeder zusätzliche Verbraucher erhalten außerdem je drei Sensoren:
**Netto-CO₂-Ersparnis**, **direkt genutzte PV-Energie** und **lokal genutzte
PV-Speicherenergie**, jeweils mit dem Verbrauchernamen. Die interne, ungerundete
direkte beziehungsweise gespeicherte Energie aller Verbraucher plus dem
jeweiligen Zuordnungsrest entspricht exakt dem Systemwert. Die
verbraucherspezifischen Speicher-Nettoanteile enthalten unabhängige konservative
Belastungsobergrenzen und werden deshalb nicht zum Systemergebnis addiert.
Angezeigte Rundungen verändern die gespeicherte Bilanz nicht.

Netto-Sensoren verwenden `state_class: total`, weil ein Intervall mit höherer
Herstellungsbelastung als Bruttovermeidung ihre Werte verringern darf. Alle
übrigen Sensoren verwenden `total_increasing`: Ihre Werte sind innerhalb einer
Speichergeneration nicht negativ und monoton. Energie-Sensoren tragen die
Device Class `energy`; Emissions-Sensoren verwenden keine
CO₂-Konzentrationsklasse. Home Assistant übernimmt Anzeigepräzision und
Langzeitstatistik; die zugrunde liegenden rationalen Rechenwerte bleiben exakt.

Die Sensoren zeigen nur gemeinsam gespeicherte und zurückgelesene Ergebnisse.
Vor einem gültigen Poll sowie bei ungültigen erforderlichen Quellen oder einem
Speicherfehler sind sie `unavailable`, statt scheinbar gültige Nullen
auszugeben. Ein nachweislich gültiger Nullfluss ist dagegen ein gültiger Wert.
Die verfügbaren historischen Summen bleiben während einer Unterbrechung
erhalten. Sensoren lesen selbst keine Quellen und führen keine Nachberechnung
aus Home-Assistant-Historie durch.

Entity IDs und Anzeigenamen können in Home Assistant angepasst werden;
interne Unique IDs bleiben bei Reload, Neustart und Umbenennung stabil.
Verbraucher behalten beim Umbenennen ihre Zeitreihe. Nach dem Entfernen werden
ihre gespeicherten Werte nicht neu verteilt; ein später neu angelegter
Verbraucher erhält auch bei gleichem Namen eine neue Identität.

Die atomare Speichergeneration ist die einzige Quelle für die Wiederherstellung
der Bilanz; Recorder-Werte werden nicht als Ersatz eingelesen. Ein Quellenreset
oder fachlicher Segmentwechsel setzt Ergebniszähler nicht zurück. Ein
ausdrücklich bestätigter Reparaturreset erzeugt gemäß dem Vertrag eine neue
Generation unter denselben Entity-Identitäten. `total_increasing` beginnt dann
einen neuen Zählerzyklus; Netto-Sensoren veröffentlichen den gespeicherten
Reparaturzeitpunkt als `last_reset`, damit der Reset nicht als negative Ersparnis
gezählt wird.

## Betrieb, Diagnose und Reparatur

Konfiguration, Manifest und Bilanzgeneration sind versioniert. Bekannte ältere
Formate werden nur deterministisch in das unterstützte Format übernommen;
geänderte Dateien werden atomar gespeichert und frisch zurückgelesen. Eine
unbekannte neuere Version oder ein ungültiger Inhalt wird nicht als leerer
Erststart behandelt. Der betroffene Eintrag bleibt angehalten, während andere
Anlagen weiterarbeiten können. Entfernte oder deaktivierte Quellen sowie eine
inkompatible Konfiguration erhalten eigene Reparaturmeldungen und verlangen
eine Korrektur der Einstellungen statt eines Bilanzresets.

Bei ungültigen laufenden Messungen erscheinen begrenzte Qualitätsmeldungen im
Home-Assistant-Protokoll, ohne Rohmesswerte oder Quellenidentitäten auszugeben.
Wiederholte Störungen erzeugen höchstens alle 15 Minuten eine Qualitätswarnung.
Ein Fehler beim Speichern oder Verifizieren stoppt weitere Buchungen und meldet
den Handlungsbedarf über **Reparaturen**.

Die Reparatur einer gespeicherten Bilanz bietet zuerst **Ohne Reset erneut
laden** an. Damit wird ausschließlich die bestehende Generation erneut geprüft.
Erst die getrennte Reset-Option erklärt die Folgen und verlangt eine ausdrücklich
aktivierte Bestätigung. **Erstelle vor einem Reset eine vollständige Sicherung
von Home Assistant.** Abbrechen der Bestätigung verändert die Bilanz nicht.

Ein bestätigter Reset beginnt eine neue Generation mit Ergebnissummen bei null,
einer neuen Messbaseline und konservativ unbekannter Speicherherkunft. Alte
Generationsdateien bleiben zur manuellen Wiederherstellung erhalten. Ein
vorhandenes unlesbares Manifest wird vor dem Ersatz unverändert gesichert und
die Sicherung geprüft; bereits vorhandene Korruptdateien bleiben erhalten.
Ohne gültige Zuordnung wird keine Altgeneration erraten oder automatisch
wiederhergestellt. Ein Konflikt mit einem anderen Eintrag verhindert das
Überschreiben. Die Sensoridentitäten und frühere Recorder-Historie bleiben
erhalten; die Statistik beginnt den oben beschriebenen neuen Zählerzyklus.
Die Reparatur gilt erst als erfolgreich, wenn die neue oder wieder geladene
Generation verifiziert ist und der vollständig abgewartete Reload den Eintrag
erfolgreich geladen hat. Bei einem Fehler bleibt die Meldung offen.
Eine begonnene Reparatur bleibt dauerhaft im Manifest markiert, bis dieser
Abschluss verifiziert wurde. Nach einem Neustart oder in einem neuen Dialog wird
dieselbe Reparaturgeneration fortgeführt; ein fehlgeschlagener Ladeversuch löst
keinen weiteren Reset aus.

Über das Menü des Integrationseintrags lässt sich eine **Diagnosedatei**
herunterladen, auch wenn Setup noch keine Laufzeit erzeugen konnte. Sie enthält
ausschließlich Messtopologie, Verbrauchsmodus, Speicher vorhanden/abwesend,
Versionsangaben, Betriebs- und Messphase, das letzte akzeptierte Periodenende,
Quellenrollen und bereits bekannte Einheiten, begrenzte Diagnosezähler sowie
Angaben dazu, ob ein Herkunftskonto vorhanden oder in Quarantäne ist. Ohne
gespeicherte Beobachtung bleiben Einheiten unbekannt; die aktuelle CO₂-Quelle
wird für die Diagnose nicht zusätzlich gelesen.

Namen und Identitäten werden geschwärzt. Rohzähler, Energie- und Emissionssummen,
Kapazität, Standort, Dateipfade und Messhistorien werden nicht exportiert. Die
Diagnose liest weder Quellen noch Speicherdateien erneut und verändert keine
Bilanz. CO2 Saver arbeitet lokal, betreibt keine Telemetrie und überträgt keine
Daten an externe Dienste. Die konfigurierte Energie- oder CO₂-Quellintegration
kann eigene Datenverbindungen besitzen; CO2 Saver liest deren vorhandene
Home-Assistant-Zustände.

## Entwicklung und Beiträge

Die [Beitragshinweise](CONTRIBUTING.md) beschreiben GPLv3, Issue-Abhängigkeiten,
Fokus und Anforderungen an Änderungen. Verbindlich sind außerdem
[`AGENTS.md`](AGENTS.md) und die [Repository-Skills](.agents/skills).

Die [Prüfanleitung](docs/testing.md) enthält die vollständige Einrichtung der
hashgebundenen Testumgebung, die Szenariomatrix und den offiziellen
Hassfest-Validator. Für eine bereits eingerichtete Umgebung:

```bash
.venv/bin/python -m compileall -q custom_components tests
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/python -m pytest --cov=custom_components.co2saver --cov-report=term-missing
```

Fehler mit reproduzierbaren Schritten und geschwärzter Diagnose in einem
[GitHub-Issue](https://github.com/dr-dimitri/co-saver/issues) melden. Das Projekt
beschränkt sich auf die belegbare CO₂-Bilanzierung des vereinbarten PV-Eigenverbrauchs.

## Lizenz

Dieses Projekt steht ausschließlich unter der [GNU General Public License
Version 3](LICENSE), `GPL-3.0-only`. Der vollständige Lizenztext liegt im Repository.
