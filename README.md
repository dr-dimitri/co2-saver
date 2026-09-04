# CO2 Saver for Home Assistant

CO2 Saver wird eine Home-Assistant-Custom-Integration, die nachvollziehbar berechnet, wie viele Treibhausgasemissionen durch selbst erzeugten und selbst verbrauchten PV-Strom vermieden werden.

> **Status:** Planungs- und Projektaufbauphase. Die Integration ist noch nicht installierbar.

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

## Entwicklung

Verbindliche Arbeitsregeln stehen in [`AGENTS.md`](AGENTS.md). Die Repo-Skills unter [`.agents/skills`](.agents/skills) enthalten die fachlichen CO₂-Bilanzierungsregeln und die Home-Assistant-Entwicklungskonventionen. Die [GitHub-Issues](https://github.com/dr-dimitri/co-saver/issues) bilden eine strikt abhängige Umsetzungskette; gearbeitet wird jeweils nur am nächsten nicht blockierten Issue.

## Lizenz

Dieses Projekt steht unter der [GNU General Public License Version 3](LICENSE), ausschließlich in Version 3 (`GPL-3.0-only`).
