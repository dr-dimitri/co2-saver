# CO2 Saver for Home Assistant

CO2 Saver wird eine Home-Assistant-Custom-Integration, die nachvollziehbar berechnet, wie viele Treibhausgasemissionen durch selbst erzeugten und selbst verbrauchten PV-Strom vermieden werden.

> **Status:** Frühe Entwicklungsphase. Das Integrationsgerüst ist ladbar und
> entladbar; der Home-Assistant-unabhängige Fachkern wird entlang der
> Issue-Roadmap schrittweise an Messaufnahme und Entities angebunden.

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

## Eingabemodelle

Die geplante Konfiguration unterstützt zwei klar getrennte Verbrauchsmodelle:

1. Ein aggregierter Verbrauchssensor enthält Haus und zusätzliche Verbraucher. Zusätzliche Verbraucher können daraus über definierte Anteile zugeordnet werden; die Summe bleibt auf den gemessenen Gesamtverbrauch begrenzt.
2. Haus und zusätzliche Verbraucher werden mit separaten, nicht überlappenden Sensoren erfasst.

Die Festlegungen zu Messwerttypen, Emissionsfaktoren, Speicherherkunft, Verlusten und Zeitbezug stehen im angenommenen [Mess- und CO₂-Bilanzierungsvertrag](docs/decisions/0001-accounting-and-input-contract.md). Abhängige Implementierung muss diesen Vertrag einhalten.

## Fachlicher Kern

Das Domänenmodell unter `custom_components/co2saver/domain` verarbeitet bereits
gebildete Energieintervalle ohne Zugriff auf Home-Assistant-Zustände. Energie wird
intern exakt in `kWh`, Emissionen in `gCO₂e` und Faktoren in `gCO₂e/kWh`
gerechnet. Es ermittelt nur mathematisch garantierte Flussuntergrenzen, führt
nicht sicher zuordenbare lokale Energie separat und hält den Herkunftsnachweis
eines optionalen Speichers als konservative Schranken. Messwertaufnahme,
Persistenz, Konfiguration und Ergebnis-Entities folgen in den jeweils dafür
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
