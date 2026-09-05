# Zu CO2 Saver beitragen

CO2 Saver berechnet die CO₂-Ersparnis durch lokal genutzte PV-Energie in Home
Assistant. Beiträge sollen dieses Ziel mit einer möglichst kleinen, fachlich
zusammenhängenden Änderung voranbringen.

## Vor Beginn

1. Lies [AGENTS.md](AGENTS.md), das betreffende [Projekt-Issue](https://github.com/dr-dimitri/co-saver/issues)
   und dessen Vorgänger. Beginne nur mit dem nächsten unblockierten Issue oder
   einer dafür erforderlichen Voraussetzung. Die Akzeptanzkriterien des
   Vorgängers müssen erfüllt sein.
2. Prüfe die [Bilanzierungs-ADR](docs/decisions/0001-accounting-and-input-contract.md)
   und die passenden Projektvorgaben für
   [CO₂-Bilanzierung](.agents/skills/co2-accounting/SKILL.md) und
   [Home-Assistant-Integration](.agents/skills/home-assistant-integration/SKILL.md).
   Benenne das Akzeptanzkriterium, das dein Beitrag erfüllt.
3. Halte offene Produkt-, Mess- oder Bilanzierungsfragen zuerst in einem Issue
   mit Vorgänger, Umfang, Akzeptanzkriterien und ausgeschlossenen Folgearbeiten
   fest. Ändere das vereinbarte Verhalten nicht stillschweigend. Neue Funktionen
   außerhalb der bisherigen Integration benötigen ein eigenes freigegebenes Issue.

Fehlerberichte sollten den verwendeten Home-Assistant- und Integrationsstand,
Topologie, Verbrauchsmodus, reproduzierbare Schritte sowie erwartetes und
tatsächliches Ergebnis enthalten. Verwende kleine synthetische Messbeispiele;
veröffentliche keine persönlichen Namen, Entity-IDs, Speicherdateien oder
Messhistorien. Den Umfang des geschwärzten Diagnoseexports beschreibt die
[README](README.md).

## Änderung und Prüfung

Halte Anforderungen, Implementierung, Tests und Nutzerdokumentation im selben
Beitrag aufeinander abgestimmt. Änderungen an sichtbaren Texten betreffen
`strings.json` sowie die deutschen und englischen Übersetzungen. Bevorzuge
öffentliche Home-Assistant-APIs und füge nur erforderliche Abhängigkeiten hinzu.

Die Bilanz muss konservativ bleiben: Export erhält keine lokale PV-Gutschrift,
Speicherladung erzeugt keine Ersparnis, und dieselbe Energie darf nicht direkt
und erneut bei Entladung gebucht werden. Unbekannte Herkunft wird nicht zu PV
umgedeutet. Negative Nettoergebnisse sind zulässig. Neustarts und Quellzählerresets
dürfen bereits verbuchte Summen nicht verändern.

Die festgelegten Python- und Paketversionen, die Einrichtung der Testumgebung
und sämtliche Prüfkommandos stehen in [docs/testing.md](docs/testing.md).
Führe während der Arbeit die betroffenen Tests aus und vor dem Abschluss die
vollständigen Tests einschließlich Coverage, Formatierung, Lint, Typprüfung und
Hassfest. Tests verwenden synthetische Quellen und benötigen keine Hardware
oder externen Dienste. Schwäche bestehende Prüfungen nicht ab, um einen Fehler
zu verdecken.

Ein Pull Request verknüpft das Issue und erklärt das konkrete Problem, das
resultierende Verhalten und die ausgeführten Prüfungen. Rechenbeispiele müssen
mit der ADR und den Regressionstests übereinstimmen. Vor dem Abschluss erneut
prüfen: Bleibt die Änderung im Issue-Umfang, erhält sie die Bilanzinvarianten,
und sind offene Fragen als Issues erfasst?

## Lizenz

Das Projekt steht ausschließlich unter der **GNU General Public License,
Version 3** (`GPL-3.0-only`); der vollständige Lizenztext steht in
[LICENSE](LICENSE). Beiträge müssen unter dieser Projektlizenz bereitgestellt
werden können. Behalte vorhandene Urheber- und SPDX-Hinweise bei und verwende
für neue Python-Dateien den bestehenden Lizenzkopf mit `GPL-3.0-only`.
Diese Beitragshinweise ändern die Projektlizenz nicht.
