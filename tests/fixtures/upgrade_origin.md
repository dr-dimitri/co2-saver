# Herkunft des Upgrade-Fixtures

`upgrade_filled_storage.json` wurde für Issue #15 mit dem unveränderten
Entwicklungsstand nach Issue #11 erzeugt:

- Quellcommit: `e77c740201b6565c3cd43ff2894e742b2a2706e9`.
- Git-Tree von `custom_components/co2saver`:
  `193ff9f72ea89c1804eb0e57a1c7442981601833`.
- Laufzeit: Home Assistant `2026.9.0`, Testplugin `0.13.363`, Python `3.14`.
- Config Entry, Manifest-Payload, Generations-Payload und HA-Store-Container:
  jeweils Version `1.1`.

Der Commit wurde mit `git archive` in ein temporäres Verzeichnis entpackt.
Alle 33 Dateien des Integrationsverzeichnisses und der beiden verwendeten
historischen Testhelfer wurden anschließend byteweise gegen den Commit
geprüft. Nur der zusätzliche Test
[`upgrade_capture.py`](upgrade_capture.py) wurde als
`tests/test_upgrade_capture.py` in dieses Archiv kopiert. Er verwendet das
damalige öffentliche Setup, den Minutenpoll, den Bilanzkern und die
Persistenzimplementierung; keine aktuelle Integration wird importiert.

Die Registry-Quellen und Verbrauchswerte sind vollständig synthetisch. Die
Integration vergibt ihre IDs regulär. Das Capture deaktiviert ausdrücklich
den standardmäßigen In-Memory-Store des HA-Testplugins und verwendet echte
Dateien in einem isolierten temporären Home-Assistant-Verzeichnis. Nach
erfolgreichem Unload werden die beiden unveränderten UTF-8-Store-Dateien als
Strings im JSON-Fixture abgelegt, zusammen mit Config Entry, Quellenidentitäten,
Zählerständen und letztem Periodenende. Das Fixture wurde weder aus dem
aktuellen Codec rekonstruiert noch von einem neueren Schema zurückgerechnet.

## Historische Zeitreihe

Das Szenario verwendet Wechselrichterdaten, Gesamtverbrauch mit `25 %`
Wallboxanteil, `10 kWh` nutzbare Kapazität, `90 %` Wirkungsgrad sowie die
Herstellungsfaktoren `40 gCO₂e/kWh` PV und `20 gCO₂e/kWh` Speicher.
Alle Energiezähler beginnen bei `100 kWh`.

| Periodenende UTC | Beobachtung |
| --- | --- |
| 2026-09-05 12:01 | Erste vollständige Baseline, keine Buchung. |
| 2026-09-05 12:02 | 10 kWh unbekannte Entladung an 10 kWh lokale Last; Speicher damit nachweislich leer, keine PV-Gutschrift. |
| 2026-09-05 12:03 | 6 kWh PV, 2 kWh Last, 3 kWh Ladung und 1 kWh Export bei Netzintensität 400 gCO₂e/kWh. |
| 2026-09-05 12:04 | 1 kWh Entladung vollständig an lokale Last bei Netzintensität 500 gCO₂e/kWh. |

Nach der letzten Zeile enthält die Generationsrevision `5` bereits
`2 kWh` direkte PV mit `800 - 80 = 720 gCO₂e` Ersparnis sowie
`1 kWh` Speicher-PV mit `500 - 400/9 - 20 = 3920/9 gCO₂e` Ersparnis.
Der Speicher enthält noch `1,7 kWh` garantiert PV und `680/9 gCO₂e`
aufgeschobene PV-Belastung. Das initialisierte Manifest hat Revision `3`.

SHA-256 der im Fixture enthaltenen ursprünglichen Store-Bytes:

| Datei | SHA-256 |
| --- | --- |
| Manifest | `369a8857c19a7655ba840df44c66adf282058707ba018554c7837024c4773832` |
| Generation | `38cde8e736ef9187ef116977e9f929064ec28f9b2681798cec24df115f31ba1c` |

## Nachweis im aktuellen Stand

[`test_release_upgrade.py`](../test_release_upgrade.py) installiert genau
diese Store-Bytes und Quellenidentitäten in einer frischen HA-Testinstanz.
Das aktuelle Setup migriert nur das Manifest auf Payload `1.2`, Revision `4`.
Die gesamte Generation, einschließlich Herkunft, Verbraucherwerten, Baselines,
Summen und Revision, bleibt bytegleich. Ein weiterer Reload migriert nicht
erneut. Ein identischer Messvektor erzeugt keine zweite Buchung.

Erst eine weitere lokale `1-kWh`-Entladung erhöht die Generation auf Revision
`6`: kumulativ `2 kWh` Speicher-PV, `1000 gCO₂e` Bruttovermeidung,
`800/9 gCO₂e` PV-Belastung, `40 gCO₂e` Speicherbelastung und
`7840/9 gCO₂e` Speicher-Netto. Die zuvor gebuchten direkten Werte bleiben
unverändert; `0,7 kWh` PV mit `280/9 gCO₂e` Belastung verbleiben im Speicher.

Der normale Upgrade-Test benötigt nur das mitgelieferte JSON-Fixture und
die Testabhängigkeiten. Er benötigt weder Git noch Netzwerk, das Archiv
des Altstands oder einen bestimmten Checkoutpfad.

## Fixture erneut erzeugen

Die folgenden Befehle werden im Repository ausgeführt. Die Testabhängigkeiten
müssen bereits in `.venv` vorhanden und der Quellcommit lokal verfügbar sein.
Die Integration und historischen Testhelfer im Archiv werden nicht geändert:

```bash
upgrade_repo="$(pwd)"
upgrade_archive="$(mktemp -d)"
git archive e77c740201b6565c3cd43ff2894e742b2a2706e9 | tar -x -C "$upgrade_archive"
cp tests/fixtures/upgrade_capture.py "$upgrade_archive/tests/test_upgrade_capture.py"
(
  cd "$upgrade_archive"
  CO2SAVER_UPGRADE_FIXTURE="$upgrade_repo/tests/fixtures/upgrade_filled_storage.json" \
    "$upgrade_repo/.venv/bin/python" -m pytest tests/test_upgrade_capture.py -q
)
```

Eine erneute Erzeugung vergibt neue zufällige Quell-, Entry- und Store-IDs.
Die fachlichen Werte bleiben gleich; die dokumentierten Byte-Hashes müssen
bei einem bewussten Fixture-Ersatz entsprechend neu geprüft werden.
