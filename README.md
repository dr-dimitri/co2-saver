# CO2 Saver for Home Assistant

CO2 Saver wird eine Home-Assistant-Custom-Integration, die nachvollziehbar berechnet, wie viele Treibhausgasemissionen durch selbst erzeugten und selbst verbrauchten PV-Strom vermieden werden.

> **Status:** Frühe Entwicklungsphase. Der Config Flow kann Messtopologie,
> PV-/Netzquellen und einen optionalen Stromspeicher als unverbindlichen Entwurf
> prüfen. Er endet bewusst vor der noch ausstehenden Verbraucherkonfiguration:
> Es gibt noch keine fertig konfigurierbare Anlage, laufende Messauswertung oder
> CO₂-Buchung.

## Zielbild

Die Integration soll Energieflüsse aus vorhandenen Home-Assistant-Entitäten auswerten und daraus Bruttovermeidung, herstellungsbedingte Emissionen und die Netto-CO₂-Ersparnis ableiten. Geplant sind:

- PV-Erzeugung über Wechselrichter- oder Smartmeter-Sensoren,
- Hausverbrauch sowie optionale weitere Verbraucher,
- wahlweise anteilige Verbraucher innerhalb eines Gesamtzählers oder separat gemessene Verbraucher,
- ein optionaler Stromspeicher mit PV-Herkunftsnachweis,
- konfigurierbare Herstellungsfaktoren für PV-Anlage und Speicher,
- Home-Assistant-Sensoren mit belastbarer Wiederherstellung und Langzeitstatistik.

## Zentrale Bilanzierungsregel

Direkt genutzter PV-Strom kann beim Verbrauch bilanziert werden. In einen Speicher geladener PV-Strom erzeugt zu diesem Zeitpunkt noch keine Einsparung: Die zugehörige Vermeidung wird erst anerkannt, wenn nachweislich PV-stämmige Energie aus dem Speicher an einen erfassten Verbraucher abgegeben wird. Direkte Nutzung und spätere Speicherentladung dürfen niemals doppelt gezählt werden.

## Aktueller Konfigurationsstand

Die ersten beiden Abschnitte des zusammenhängenden UI-Config-Flows sind
umgesetzt. Zuerst erfasst der Flow genau eine der beiden Messtopologien und
zeigt nur deren Quellenfelder:

| Topologie | Pflichtquellen | Optionale Quelle |
| --- | --- | --- |
| Wechselrichter | PV-Erzeugung, Netzbezug und Netzeinspeisung | keine |
| Smartmeter | Netzbezug und Netzeinspeisung | PV-Erzeugung nur zur Plausibilitätsprüfung |

Alle ausgewählten Quellen müssen in Home Assistants Entity Registry
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
Austauschmerkmal. Wenn der vollständige Flow in Issue #8 später abgeschlossen
wird, wirkt jede fachliche Änderung nur prospektiv über einen neuen
vollständigen Segmentfingerabdruck und einen konservativ quarantänisierten
Speicherbestand; historische Summen werden nicht neu berechnet.

Eine gültige Auswahl mit oder ohne Speicher erreicht lediglich den Platzhalter
für die in Issue #7 folgende Verbraucherkonfiguration. Bis die Schritte #7 und
#8 vollständig umgesetzt sind, erzeugt der Flow keinen Config Entry, keinen
Store, keinen Speicherherkunftsledger, keinen Listener, keinen Polling-Runner und
keine Bilanzbuchung. Auch eine Rekonfiguration bleibt bis zum späteren Abschluss
des gesamten Flows ein Entwurf; vorhandener Store-Locator, historische Summen
und die aktive Konfiguration bleiben unverändert.

## Eingabemodelle

Die geplante Konfiguration unterstützt zwei klar getrennte Verbrauchsmodelle:

1. Ein aggregierter Verbrauchssensor enthält Haus und zusätzliche Verbraucher. Zusätzliche Verbraucher können daraus über definierte Anteile zugeordnet werden; die Summe bleibt auf den gemessenen Gesamtverbrauch begrenzt.
2. Haus und zusätzliche Verbraucher werden mit separaten, nicht überlappenden Sensoren erfasst.

Kumulative Energiequellen werden ausschließlich an UTC-Minutenwechseln als
gemeinsamer Messvektor gelesen; einzelne Home-Assistant-State-Events lösen keine
zusätzliche Auswertung aus. Ein am Taktzeitpunkt ungültiger Eingang verwirft das
Intervall.
Wechselt ein Zähler zwischen den unterstützten Einheiten `Wh`, `kWh` und `MWh`,
wird er konservativ ohne Delta neu gebaselined, damit kein unbewiesener
Maßstabssprung als Energie erscheint.

Die Festlegungen zu Messwerttypen, Emissionsfaktoren, Speicherherkunft, Verlusten und Zeitbezug stehen im angenommenen [Mess- und CO₂-Bilanzierungsvertrag](docs/decisions/0001-accounting-and-input-contract.md). Abhängige Implementierung muss diesen Vertrag einhalten.

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
Messbaseline und spätere Bilanzwerte gemeinsam in einer verifizierten
Transaktion. Er initialisiert einen fehlenden Zustand nur nach ausdrücklich
bestätigter physischer Abwesenheit; ein leeres Ladeergebnis genügt dafür nicht.
Die ersten beiden UI-Schritte für Topologie, PV-/Netzquellen und einen optionalen
Speicher nutzen diesen Vertrag bereits zur aktuellen Validierung, halten ihren
Zustand aber absichtlich nur als Flow-Entwurf. Die restliche Konfiguration folgt
in den Issues #7 und #8, die atomare Aktivierung des Runners ohne Speicher in #9
und mit Speicherbilanz in #10. Ergebnis-Entities folgen in den dafür
vorgesehenen Roadmap-Issues.

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
