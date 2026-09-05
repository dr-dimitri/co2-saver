# CO2 Saver for Home Assistant

CO2 Saver wird eine Home-Assistant-Custom-Integration, die nachvollziehbar berechnet, wie viele Treibhausgasemissionen durch selbst erzeugten und selbst verbrauchten PV-Strom vermieden werden.

> **Status:** Frühe Entwicklungsphase. Der vollständige Config Flow kann eine
> Anlage mit Messtopologie, optionalem Speicher, Verbrauchern und CO₂-Faktoren
> einrichten. Anlagen mit und ohne Speicher werten ihre Energiezähler aus und
> speichern direkte PV-Nutzung, Speicherherkunft und CO₂-Bilanzen atomar.
> Kumulative Ergebnis-Sensoren veröffentlichen System- und Verbraucherwerte.

## Zielbild

Die Integration wertet Energieflüsse aus vorhandenen Home-Assistant-Entitäten aus und leitet daraus Bruttovermeidung, herstellungsbedingte Emissionen und die Netto-CO₂-Ersparnis ab. Unterstützt werden:

- PV-Erzeugung über Wechselrichter- oder Smartmeter-Sensoren,
- Hausverbrauch sowie optionale weitere Verbraucher,
- wahlweise anteilige Verbraucher innerhalb eines Gesamtzählers oder separat gemessene Verbraucher,
- ein optionaler Stromspeicher mit PV-Herkunftsnachweis,
- konfigurierbare Herstellungsfaktoren für PV-Anlage und Speicher,
- Home-Assistant-Sensoren mit belastbarer Wiederherstellung und Langzeitstatistik.

## Zentrale Bilanzierungsregel

Direkt genutzter PV-Strom kann beim Verbrauch bilanziert werden. In einen Speicher geladener PV-Strom erzeugt zu diesem Zeitpunkt noch keine Einsparung: Die zugehörige Vermeidung wird erst anerkannt, wenn nachweislich PV-stämmige Energie aus dem Speicher an einen erfassten Verbraucher abgegeben wird. Direkte Nutzung und spätere Speicherentladung dürfen niemals doppelt gezählt werden.

Die laufende Auswertung berechnet die mathematisch garantierte direkte
PV-Nutzung. Deren Energie multipliziert mit der
gültigen Netz-CO₂-Intensität ergibt die Bruttovermeidung; der PV-Herstellungsfaktor
wird auf dieselbe Energie genau einmal angewendet. Die Differenz ist die
Netto-Ersparnis und darf negativ sein. Exportierte Energie erhält keine Gutschrift.
Für Haushalt und zusätzliche Verbraucher gelten jeweils eigene garantierte
Flussuntergrenzen. Ihre Energiewerte und der ausdrücklich nicht zuordenbare Rest
ergeben zusammen exakt die systemweite direkte PV-Energie.

Bei einem konfigurierten Speicher führt die Auswertung zusätzlich konservative
Unter- und Obergrenzen für den Bestand und seine nachgewiesene PV-Herkunft.
Ladung erhöht nur den mit dem bestätigten Wirkungsgrad bereinigten Bestand und
hinterlegt dessen PV-Herstellungsbelastung; sie erzeugt keine Speicherersparnis.
Erst eine garantiert PV-stämmige Entladung an einen lokalen Verbraucher wird mit
der dann gültigen Netz-CO₂-Intensität bewertet. Dabei werden die zurückgestellte
PV-Belastung einschließlich Ladeverlusten und der Speicher-Herstellungsfaktor
jeweils einmal berücksichtigt. Netzenergie, unbekannte Herkunft, Verluste und
Speicherexport erhalten keine Gutschrift. Direkte PV-Nutzung und
Speicherentladung bleiben getrennt.

Bei gemischter oder unklarer Herkunft wird keine gleichmäßige Durchmischung
unterstellt. Die Verbraucherenergien und ihr Zuordnungsrest ergeben exakt die
systemweite Speicher-PV-Energie. Die Speicher-Nettoergebnisse einzelner
Verbraucher sind dagegen unabhängige konservative Sichten und dürfen nicht
addiert werden; maßgeblich bleibt die Systembilanz.

## Aktueller Konfigurationsstand

Der zusammenhängende UI-Config-Flow ist vollständig umgesetzt.
Zuerst erfasst der Flow genau eine der beiden Messtopologien und zeigt nur deren
Quellenfelder:

| Topologie | Pflichtquellen | Optionale Quelle |
| --- | --- | --- |
| Wechselrichter | PV-Erzeugung, Netzbezug und Netzeinspeisung | keine |
| Smartmeter | Netzbezug und Netzeinspeisung | PV-Erzeugung nur zur Plausibilitätsprüfung |

Alle ausgewählten Energiequellen müssen in Home Assistants Entity Registry
eingetragene, richtungsgetrennte kumulative AC-Energiesensoren sein. Zulässig
sind ausschließlich `sensor`-Entities mit `device_class: energy`,
`state_class: total` oder `total_increasing` und der Einheit `Wh`, `kWh` oder
`MWh`. Ein vorzeichenbehafteter Nettozähler erfüllt diesen Vertrag nicht.

Jede Rolle muss mindestens alle fünf Minuten neu gemessen und höchstens 60
Sekunden nach Ende ihrer Messperiode in Home Assistant veröffentlicht werden;
auch die Veröffentlichungszeitpunkte der Rollen dürfen höchstens 60 Sekunden
auseinanderliegen. Entscheidend ist ein bei allen Rollen exakt identisches
State-Attribut `co2saver_period_end`: Es muss das echte, messseitig erzeugte
UTC-Ende derselben physischen Erfassungsperiode enthalten. Eine gerundete
Home-Assistant-Zeit oder ein aus dem Empfangszeitpunkt abgeleiteter Wert reicht
nicht aus. Der Flow verlangt die ausdrückliche Bestätigung dieses
Synchronitätsvertrags. Ein konkretes MQTT-Referenzmuster steht im
[Mess- und CO₂-Bilanzierungsvertrag](docs/decisions/0001-accounting-and-input-contract.md#21-konkreter-synchroner-quellpfad).

Im zweiten Abschnitt muss ausdrücklich angegeben werden, ob ein Stromspeicher
vorhanden ist; für eine neue Einrichtung gibt es keine stillschweigende
Vorauswahl. Bei einem Speicher verlangt der Flow:

- einen kumulativen Zähler für die AC-Energie am Speichereingang beim Laden,
- einen getrennten kumulativen Zähler für die AC-Energie am Speicherausgang beim
  Entladen,
- die nutzbare, am AC-Ausgang abgebbare Kapazität von `0.1` bis `1000 kWh` ohne
  Standardwert,
- einen sichtbar bestätigten AC-Rundtrip-Wirkungsgrad von mehr als `0` bis
  einschließlich `100 %`; die UI schlägt `90 %` vor.

Lade- und Entladezähler müssen verschiedene Entities sein und gemeinsam mit
allen zuvor gewählten PV-/Netzrollen denselben oben beschriebenen Quellen- und
Synchronitätsvertrag erfüllen. Insbesondere müssen sie exakt dasselbe echte
physische `co2saver_period_end` liefern. Kapazität und Wirkungsgrad werden an
der UI-Grenze direkt als rohe Dezimaltexte geprüft und anschließend exakt
kanonisiert; binäre Gleitkommawerte werden nicht übernommen. Es gilt der
Dezimalpunkt; Komma, Exponentialschreibweise sowie führende oder nachgestellte
Leerzeichen werden abgelehnt.

Wird bei der Rekonfiguration eines vorhandenen Speichers weiterhin ein Speicher
konfiguriert, muss zusätzlich ausgewählt werden, ob es sich um denselben
physischen Speicher handelt oder ob er ersetzt wurde. Einen Austausch hinter
unveränderten Zähler-Entities kann CO2 Saver nicht automatisch erkennen.
Derselbe Speicher behält seine interne Identität; beim erstmaligen Hinzufügen
oder ausdrücklich bestätigten Austausch erzeugt der Flow genau einmal eine neue
Identität im Entwurf. Diese Auswahl ist kein dauerhaft gespeichertes
Austauschmerkmal. Beim Abschluss des vollständigen Flows wirkt jede fachliche Änderung nur prospektiv über einen neuen
vollständigen Segmentfingerabdruck und einen konservativ quarantänisierten
Speicherbestand; historische Summen werden nicht neu berechnet.

Nach den Verbrauchern folgt die Konfiguration der Emissionsfaktoren:

| Feld | Einheit und gültiger Bereich |
| --- | --- |
| Netz-CO₂-Sensor | Registrierter `sensor` in `gCO2e/kWh`, `gCO2eq/kWh`, `gCO₂e/kWh` oder der entsprechenden Kilogrammvariante; normalisiert `0` bis `5000 g CO₂e/kWh` |
| Maximales Quellenalter | Ganze Minuten von `1` bis `1440`, sichtbarer Vorschlag `60` |
| PV-Herstellungsfaktor | Expliziter Dezimaltext von `0` bis `5000 g CO₂e/kWh`, pro erzeugter AC-kWh |
| Speicher-Herstellungsfaktor | Nur mit Speicher: expliziter Dezimaltext von `0` bis `5000 g CO₂e/kWh`, pro anrechenbar entladener AC-kWh |

Es gibt keine vorbelegten Herstellungsfaktoren. Dezimalpunkt, Wertebereich und
aktuelle Sensorqualität werden vor dem Speichern geprüft. Ein `ppm`-Sensor ist
keine Netz-CO₂-Quelle. Die Netzprobe muss endlich, nicht negativ, verfügbar und
über `last_reported` zeitlich gültig sein; eine Device Class wird nicht verlangt.

Bei der laufenden Messauswertung liest jeder reguläre UTC-Minutenpoll
genau einmal die aktuelle Netz-CO₂-Probe. Sie darf nicht nach dem
physischen Ende des dann vollständig verarbeiteten Energieintervalls liegen
und dessen konfiguriertes Höchstalter nicht überschreiten; beide Grenzen gelten
einschließlich Gleichheit. Ältere Proben werden nicht zwischengespeichert oder
bei `unknown`, `unavailable` oder anderen ungültigen aktuellen Werten verwendet.
Das gilt auch nach Neustart, Reload und Segmentwechsel sowie bei erst später
vervollständigten Energiekandidaten. Ohne zulässige aktuelle Probe bleibt die
gutschriftfähige Energie dauerhaft unbewertet, ohne Emissionsbuchung oder spätere
Nachbewertung. Die Entscheidung ist in
[ADR-0001 Version 2.2](docs/decisions/0001-accounting-and-input-contract.md#43-zeitliche-zuordnung)
festgehalten. Auch ohne gültige CO₂-Probe wird die physische Speicherherkunft
fortgeschrieben und die zur unbewerteten Entladung gehörende PV-Belastung
endgültig aus dem Herkunftskonto entfernt.

Zwischenschritte bleiben unverbindliche Entwürfe. Erst der vollständig geprüfte
Abschluss reserviert unter einem gemeinsamen Lock die Anlagenkennung und ein
neues Manifest. Nach überprüftem Zurücklesen wird der Config Entry erzeugt. Setup
bindet das Manifest an diesen Entry und initialisiert oder übernimmt genau die
dort bezeichnete Generation. Sie enthält die Messphase, den vollständigen
Segmentfingerabdruck, Speicherherkunft, Summen und Diagnosen. Anschließend startet
für jede Anlage genau ein Messtimer am UTC-Minutenraster. Bei einem konfigurierten
Speicher werden direkte Nutzung und Speicherherkunft aus demselben vollständigen
Messvektor gemeinsam ausgewertet.

Über **Konfigurieren** können Verbraucher und Faktoren bearbeitet werden;
**Neu konfigurieren** führt zusätzlich durch Topologie und Speicher. Abbrechen
verändert keine aktive Einstellung. Jeder fachliche Wechsel beginnt beim Reload
ein neues zukünftiges Segment mit neuer Messbaseline und konservativer
Speicherquarantäne. Reine Anzeigenamen- und Entity-ID-Änderungen erhalten das
Segment. Historische Summen und der unveränderliche Store-Locator bleiben erhalten.

Ein fehlender, beschädigter oder fremder Zustand wird niemals automatisch auf
null gesetzt. Setup bleibt dann ohne Listener und Ergebniswerte angehalten.
Home Assistant zeigt eine Reparaturmeldung mit einem erneuten Ladeversuch und
einem gesondert zu bestätigenden Reset. Entfernte Quellen verlangen eine
Neukonfiguration; Umbenennungen werden über ihre stabile Registry-ID aufgelöst.

## Eingabemodelle

Die Konfiguration unterstützt zwei klar getrennte Verbrauchsmodelle:

1. **Gesamtmessung mit Anteilen:** Ein kumulativer Zähler erfasst den gesamten
   lokalen Verbrauch von Haushalt und allen zusätzlichen Verbrauchern. Benannte
   zusätzliche Verbraucher erhalten jeweils einen exakten Anteil von `0` bis
   `100 %`; ihre Summe darf `100 %` nicht überschreiten. Der Rest ist
   Haushaltsverbrauch.
2. **Separate Zähler:** Ein kumulativer Zähler erfasst ausschließlich den
   Haushalt. Jeder zusätzliche Verbraucher besitzt einen eigenen, physisch nicht
   überlappenden kumulativen Zähler. Der lokale Gesamtverbrauch ist die Summe
   dieser Eingänge.

In beiden Modi schließen Verbrauchszähler Speicherladung und Netzeinspeisung
aus. Im separaten Modus schließt der Haushaltszähler außerdem alle zusätzlichen
Verbraucher aus. Alle Lastrollen müssen verschiedene Entities verwenden, an
derselben atomaren physischen Erfassung wie PV-, Netz- und gegebenenfalls
Speicherrollen teilnehmen und exakt dasselbe `co2saver_period_end` sowie die
bereits beschriebenen Frische- und Veröffentlichungsgrenzen erfüllen. Weil
Entity-Namen keine physische Überschneidungsfreiheit beweisen, verlangt der Flow
hierfür eine ausdrückliche Bestätigung.

Anteile werden wie die Speicherparameter als rohe Dezimaltexte mit Punkt
validiert; Komma, Exponentialschreibweise, binäre Gleitkommawerte und umgebende
Leerzeichen sind nicht zulässig. Sie zerlegen ausschließlich die gemessene
lokale Energie. Die garantierte systemweite CO₂-Ersparnis wird nicht einfach
proportional auf Verbraucher verteilt, sondern später aus den konservativen
verbraucherspezifischen Flussuntergrenzen berechnet.

Zusätzliche Verbraucher sind optional und können im Flow hinzugefügt,
umbenannt, neu zugeordnet oder entfernt werden. Jeder Verbraucher besitzt eine
stabile interne UUID: Bearbeiten und Umbenennen erhalten sie, Entfernen beendet
die zugehörige künftige Zeitreihe, und erneutes Hinzufügen erzeugt auch bei
gleichem Namen eine neue UUID. Beim Wechsel des Verbrauchsmodus bleiben UUIDs
und Namen erhalten; inkompatible Zuordnungen und der Haushaltszähler werden
gelöscht und müssen vollständig neu zugewiesen werden. Historische Werte werden
nicht verändert.

Kumulative Energiequellen werden ausschließlich an UTC-Minutenwechseln als
gemeinsamer Messvektor gelesen; einzelne Home-Assistant-State-Events lösen keine
zusätzliche Auswertung aus. Ein am Taktzeitpunkt ungültiger Eingang verwirft das
Intervall.
Wechselt ein Zähler zwischen den unterstützten Einheiten `Wh`, `kWh` und `MWh`,
wird er konservativ ohne Delta neu gebaselined, damit kein unbewiesener
Maßstabssprung als Energie erscheint.

Fehlende oder ungültige Energiequellen, Zählerresets und Datenlücken erzeugen
keine geschätzte Ersparnis. Nach einer Unterbrechung dient der erste neue gültige
Gesamtvektor ausschließlich als Recovery-Baseline; erst das folgende vollständige
Intervall kann wieder gebucht werden. Neustart und Reload stellen Messzustand
und bisherige Summen samt Speicherherkunft gemeinsam wieder her. Ein Speicher
beginnt bei der Einrichtung, bei einem fachlichen Segmentwechsel und beim
Eintritt in eine unterbrochene Energiezeitreihe konservativ in Quarantäne:
Sein möglicher Bestand reicht von leer bis zur nutzbaren Kapazität, und keine
Energie ist als PV nachgewiesen. Beobachtete Ladung und Entladung engen diese
Schranken wieder ein. Auch gleichzeitige positive Ladung und Entladung oder ein
Widerspruch zu den Bestandsschranken verwerfen das gesamte Intervall,
einschließlich möglicher direkter Ersparnis. Unload entfernt zuerst den Timer
und wartet einen bereits laufenden Commit ab. Verpasste Takte werden nicht
nachgeholt.

Die Festlegungen zu Messwerttypen, Emissionsfaktoren, Speicherherkunft, Verlusten und Zeitbezug stehen im angenommenen [Mess- und CO₂-Bilanzierungsvertrag](docs/decisions/0001-accounting-and-input-contract.md). Abhängige Implementierung muss diesen Vertrag einhalten.

## Ergebnis-Sensoren

Jede Anlage erhält zwölf kumulative System-Sensoren. Die Emissionswerte werden
in `kgCO₂e` und die Energiewerte in `kWh` veröffentlicht:

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

## Fachlicher Kern

Das Domänenmodell unter `custom_components/co2saver/domain` verarbeitet bereits
gebildete Energieintervalle ohne Zugriff auf Home-Assistant-Zustände. Energie wird
intern exakt in `kWh`, Emissionen in `gCO₂e` und Faktoren in `gCO₂e/kWh`
gerechnet. Es ermittelt nur mathematisch garantierte Flussuntergrenzen, führt
nicht sicher zuordenbare lokale Energie separat und hält den Herkunftsnachweis
eines optionalen Speichers als konservative Schranken.

Das Modul `custom_components/co2saver/measurement` liest injizierte kumulative
Energiequellen am UTC-Minutenraster, bildet daraus restartfest und fail-closed
exakte Intervalle und stellt einen versionierten Home-Assistant-Store-Adapter
bereit. Der Store erhält den Codec für den vollständigen Zustand und speichert
Messbaseline, Kandidaten, Diagnosen und kumulative Bilanzwerte gemeinsam in einer
verifizierten Transaktion. Er initialisiert einen fehlenden Zustand nur nach ausdrücklich
bestätigter physischer Abwesenheit; ein leeres Ladeergebnis genügt dafür nicht.
Der vollständige UI-Flow nutzt diesen Vertrag zur aktuellen Validierung.
Manifest, Eigentümerbindung und Generation werden atomar gespeichert und jeweils
frisch zurückgelesen. Erst nach bestätigtem Zurücklesen übernimmt die Laufzeit
die neuen Summen. Ein fehlgeschlagener Commit oder abweichendes Read-back stoppt
weitere Reads und Buchungen. Direkte Ersparnis, Speicherbewegungen, deren
Emissionskomponenten und unbewertete Energie verwenden denselben
Transaktionspfad. Ergebnis-Sensoren übernehmen erst den verifizierten Zustand.

## Entwicklung

Verbindliche Arbeitsregeln stehen in [`AGENTS.md`](AGENTS.md). Die Repo-Skills unter [`.agents/skills`](.agents/skills) enthalten die fachlichen CO₂-Bilanzierungsregeln und die Home-Assistant-Entwicklungskonventionen. Die [GitHub-Issues](https://github.com/dr-dimitri/co-saver/issues) bilden eine strikt abhängige Umsetzungskette; gearbeitet wird jeweils nur am nächsten nicht blockierten Issue.

Unterstützte Mindestbasis ist Home Assistant 2026.9.0 mit Python 3.14.2.
Die Entwicklungs- und CI-Umgebung ist auf Home Assistant 2026.9.0 festgelegt:

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --editable '.[test]'
```

Die vollständigen lokalen Prüfungen entsprechen der CI:

```bash
.venv/bin/python -m compileall -q custom_components tests
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pytest --cov=custom_components.co2saver --cov-report=term-missing
```

## Lizenz

Dieses Projekt steht unter der [GNU General Public License Version 3](LICENSE), ausschließlich in Version 3 (`GPL-3.0-only`).
