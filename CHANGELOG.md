# Changelog

## 0.1.0 — 2026-09-05

Erste MVP-Version von CO2 Saver für Home Assistant 2026.9.0.

- Direkte, nachgewiesene PV-Nutzung und erst bei lokaler Entladung anrechenbare
  PV-Speicherenergie, mit konservativem Herkunftskonto ohne Doppelzählung.
- Wechselrichter- und Smartmeter-Topologie; Haushalt allein, Gesamtverbrauch mit
  Anteilen oder separat gemessene Verbraucher; optionaler AC-Speicher.
- Aktuelle Netz-CO₂-Quelle sowie explizite PV- und Speicher-Herstellungsfaktoren.
  Energie, Bruttovermeidung, Herstellungsbelastung und negative Nettoergebnisse
  bleiben nachvollziehbar getrennt.
- Zwölf kumulative System-Sensoren und drei Sensoren je aktuellem Verbraucher,
  native Langzeitstatistik, stabile Identitäten und atomare Wiederherstellung.
- UI-Einrichtung, Optionen und Neukonfiguration auf Deutsch und Englisch;
  geschwärzte Diagnosen, sichere Migration und ausdrücklich bestätigte Reparaturen.
- Dokumentierte Handrechnungen, vollständige Szenariomatrix und reproduzierbare
  Tests einschließlich des offiziellen Hassfest-Validators.

Die Integration benötigt synchron erfasste, richtungsgetrennte kumulative
AC-Energiequellen mit echtem gemeinsamem `co2saver_period_end`. Unbekannte
Speicherherkunft, Export und Ladeverluste erhalten keine Gutschrift. Ohne gültige
aktuelle Netz-CO₂-Probe bleibt die betroffene Energie dauerhaft unbewertet.

Lizenz: **GPL-3.0-only**, vollständiger Text in [LICENSE](LICENSE).
Einrichtung, Update und bekannte Grenzen: [Release Notes](docs/releases/0.1.0.md).
