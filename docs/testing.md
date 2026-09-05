# Reproduzierbare Prüfungen

Der geprüfte Entwicklungsstand verwendet Python **3.14.7**, Home Assistant
**2026.9.0** und die vollständigen Versionen und SHA-256-Hashes aus
[`requirements-test.txt`](../requirements-test.txt). Die Runtime der Integration
benötigt keine zusätzlichen Pakete. Die Testumgebung ist davon getrennt.

Aus dem Repository-Stammverzeichnis:

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-test.txt
.venv/bin/python -m compileall -q custom_components tests
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/python -m pytest --cov=custom_components.co2saver --cov-report=term-missing
git diff --check
```

Die Python-Version des gewählten Interpreters vorab mit `python3.14 --version`
prüfen. Es ist keine Installation des Projektpakets erforderlich. Tests verwenden
HA-Fixtures, kontrollierte UTC-Zeit und simulierte Quellen. Sie benötigen weder
Hardware noch externe Dienste. `pytest-socket` begrenzt Verbindungen auf lokale
Testserver; der echte Diagnose-Download wird über einen Loopback-HTTP-Server
geprüft. Paketinstallation und Abruf des Validators benötigen Internetzugang.

## Szenarien und Invarianten

Die Runtime-Tests decken alle 18 Kombinationen aus Wechselrichter/Smartmeter,
ohne Speicher/PV-Ladung/gemischter Netz-PV-Ladung und nur Haushalt/Anteilen/separaten
Verbrauchern ab. Separate Verbraucher liefern dabei positive Messbeiträge.

| Nachweis | Tests |
| --- | --- |
| Direkte PV-Nutzung in beiden Topologien und allen Verbrauchsmodi | `tests/test_runtime.py` |
| PV- und Mischladung, lokale Entladung, Teil-/Vollexport und Verluste | `tests/test_storage_runtime.py` |
| Mehrere Speicherzyklen mit überlappendem Restbestand und echtem HA-Neustart | [Referenzrechnung](accounting-reference.md) und ihr verknüpfter Test |
| Exakte Flussgrenzen, Herkunft und unabhängige Verbrauchersichten | `tests/domain/`, `tests/test_evaluation.py`, `tests/test_storage_evaluation.py` |
| Unregelmäßige Zeitpunkte, fehlende Daten, Duplikate und Zählerreset | `tests/measurement/`, Runtime- und Speichertests |
| Faktoränderungen gelten nur für kommende Segmente; gebuchte Summen bleiben erhalten, der Speicher wird quarantänisiert | `tests/test_bootstrap.py`, `tests/test_factor_config_flow.py`, `tests/test_config_factors.py` |
| Config-/Options-Flow und Quellidentität | `tests/test_config_flow.py`, `tests/test_consumer_config_flow.py`, `tests/test_storage_config_flow.py`, `tests/test_factor_config_flow.py` |
| Sensoren, Registry und echte Recorder-Statistik, auch nach Reparaturreset | `tests/test_sensor.py`, `tests/test_sensor_statistics.py` |
| Migration, atomare Speicherung, Reparaturwiederaufnahme und Fehlerisolation | `tests/test_migration.py`, `tests/test_repair_storage.py`, `tests/test_repairs.py`, `tests/test_runtime_health.py` |
| Geschwärzter Diagnoseexport ohne zusätzliche Quellen- oder Speicherlesevorgänge | `tests/test_diagnostics.py` |

Energiebilanzen und Emissionskomponenten werden als exakte rationale Zahlen
geprüft. Direkte und gespeicherte Energie dürfen sich nicht doppeln. Verbraucher-
Energien plus Zuordnungsrest ergeben die Systemenergie; unabhängige konservative
Speicher-Emissionssichten werden gemäß ADR nicht addiert. Ein Neustart oder
Quellzählerreset darf bereits verbuchte Summen nicht zurücksetzen.

## Home-Assistant-Validierung

Die CI verwendet den [offiziellen Hassfest-Validator](https://developers.home-assistant.io/blog/2020/04/16/hassfest/)
für Home Assistant 2026.9.0. Das Container-Image ist durch seinen Digest festgelegt,
damit ein erneuter Lauf denselben Validator verwendet. Mit installiertem Docker
lässt sich dieselbe Prüfung lokal starten:

```bash
docker run --rm \
  --volume "$PWD:/github/workspace" \
  ghcr.io/home-assistant/hassfest@sha256:5344a867c00d7c45b0d8db9f5cb09abe6f65b8e2fc2bad636bb5bd35c1740f2e
```

Hassfest prüft unter anderem Manifest, Plattform- und Übersetzungsschemata. Bei
Custom Integrations umfasst seine Übersetzungsprüfung `strings.json` und Englisch;
die Projektprüfungen sichern zusätzlich die deutsche Struktur und den öffentlichen
HA-Übersetzungszugriff ab. Eine erfolgreiche Prüfung ist keine Zertifizierung
durch Home Assistant. Der Workflow
[`CI`](../.github/workflows/ci.yml) führt Tests und Hassfest für jeden Pull Request,
auf `main` und auf manuelle Anforderung aus. Python, Action-Commits und
Validator-Image sind festgelegt; der vollständige Testlauf verlangt mindestens
95 % Coverage einschließlich Verzweigungen.

## Abhängigkeiten aktualisieren

Eine Änderung des unterstützten HA-Stands oder der Prüfwerkzeuge gehört in ein
eigenes freigegebenes Issue. Erst die direkten Testversionen in `pyproject.toml`
ändern, dann mit dem derzeit gebundenen **uv 0.12.5** den vollständigen Stand
neu auflösen:

```bash
.venv/bin/uv pip compile pyproject.toml --extra test \
  --python-version 3.14.7 --universal --generate-hashes --no-annotate \
  --output-file requirements-test.txt
```

Den erzeugten Unterschied prüfen, in einer frischen Umgebung installieren und
alle obigen Prüfungen erneut ausführen. Bei einem HA-Wechsel auch den offiziellen
Hassfest-Digest und die CI-Versionen aktualisieren. Keine Lockdatei durch ein
ungeprüftes `pip freeze` aus einer persönlichen Umgebung ersetzen.
