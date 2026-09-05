# Einrichtung und Feldreferenz

Diese Referenz beschreibt die vorhandenen Eingabefelder von CO2 Saver. Die
[README](../README.md) erklärt die Installation und das Anlegen über
**Einstellungen → Geräte & Dienste → Helfer → Helfer erstellen → CO2 Saver**.
Für nachvollziehbare Energie- und CO₂-Ergebnisse gibt es außerdem die
[geprüfte Referenzzeitreihe](accounting-reference.md).

Die Angaben in `Code-Schreibweise` hinter einem Feldnamen identifizieren das Feld
eindeutig; sie sind keine zusätzlich einzugebenden Werte. Felder sind Pflicht,
soweit sie nicht ausdrücklich als optional oder bedingt gekennzeichnet sind.

## Messdaten vor der Einrichtung prüfen

Alle benötigten Energiequellen müssen bereits in Home Assistant vorhanden sein.
CO2 Saver erwartet **kumulative, richtungsgetrennte AC-Energiezähler**. Eine
Leistung in `W` oder `kW`, ein vorzeichenbehafteter Nettozähler oder ein
Raumluft-CO₂-Sensor in `ppm` erfüllt diesen Vertrag nicht.

Für jede ausgewählte Energiequelle gelten dieselben Anforderungen:

| Merkmal | Erforderlicher Inhalt |
| --- | --- |
| Entity | Aktive `sensor`-Entity mit Eintrag in der Entity Registry. |
| Geräteklasse | `device_class: energy`. |
| Zustandsklasse | `state_class: total` oder `total_increasing`. Auch ein `total`-Zähler muss für seine Richtung monoton wachsen; eine Abnahme gilt als Reset. |
| Einheit | `Wh`, `kWh` oder `MWh`. Unterschiedliche Quellen dürfen unterschiedliche dieser Einheiten verwenden. |
| Zustand | Endlicher, nichtnegativer Zahlenwert; weder `unknown` noch `unavailable`. |
| Physische Messperiode | State-Attribut `co2saver_period_end` mit dem tatsächlichen Ende der Messperiode als Zeitstempel mit Zeitzone, nach UTC umrechenbar, beispielsweise `2026-09-05T12:00:00Z`. |
| Gemeinsame Erfassung | Alle gewählten Energierollen stammen aus derselben atomaren physischen Erfassung und weisen exakt dasselbe Messperiodenende aus. Das gilt auch für Speicher, Lasten und eine optionale PV-Plausibilitätsquelle. |
| Messrhythmus | Mindestens alle fünf Minuten eine neue Messung, auch bei unverändertem Zählerstand. Die für die Einrichtung verwendete Messung darf höchstens fünf Minuten alt sein. |
| Veröffentlichung | Home Assistant stellt `last_reported` bereit. Die Veröffentlichung erfolgt höchstens 60 Sekunden nach dem Messperiodenende; zwischen den Veröffentlichungen aller Rollen liegen höchstens 60 Sekunden. Messung und Veröffentlichung dürfen nicht in der Zukunft liegen, die Veröffentlichung nicht vor der Messung. |
| Eindeutige Rollen | Jede Energierolle verwendet eine andere Quell-Entity. Die Messumfänge dürfen dieselbe Energie nicht als mehrere lokale Verbraucher erfassen. |

Der Zeitstempel muss vom Messgerät oder Gateway stammen. Das nachträgliche
Kopieren unabhängig aktualisierter Zähler und das Anhängen einer gerundeten
Home-Assistant-Uhrzeit erzeugen keine gemeinsame Messperiode. Der dokumentierte
[MQTT-Referenzpfad](decisions/0001-accounting-and-input-contract.md#21-konkreter-synchroner-quellpfad)
übernimmt Werte und Periodenende aus derselben Gateway-Nachricht. Auch eine native
Integration kann diese Voraussetzungen erfüllen.

CO2 Saver liest die aktuellen Quellen zu den UTC-Minutenwechseln. Meldungen
zwischen diesen Zeitpunkten lösen keine zusätzlichen Buchungen aus. Die
Energie-Frischegrenzen sind fest; das später einstellbare Höchstalter der
Netz-CO₂-Probe verändert sie nicht.

## Die passende Messtopologie wählen

Die Topologie bestimmt, wie die PV-Erzeugung ermittelt wird. **Netzbezug,
Netzeinspeisung und lokaler Verbrauch werden in beiden Topologien benötigt.**
Ein Smartmeter mit ausschließlich einem Nettofluss reicht nicht aus.

| Auswahl | Geeignet, wenn … | Benötigte Energiequellen |
| --- | --- | --- |
| **Wechselrichter (PV-Erzeugung gemessen)** (`inverter`) | die kumulative PV-Erzeugung am AC-Ausgang des Wechselrichters direkt gemessen wird. | PV-Erzeugung, Netzbezug, Netzeinspeisung und Verbrauch entsprechend dem gewählten Verbrauchsmodell. |
| **Smartmeter (PV-Erzeugung abgeleitet)** (`smart_meter`) | die vollständige Standortbilanz gemessen wird, aus der sich PV-Erzeugung ableiten lässt. | Netzbezug, Netzeinspeisung und Verbrauch entsprechend dem gewählten Verbrauchsmodell. Ein PV-Zähler ist optional und dient ausschließlich der Plausibilitätsprüfung. |

Mit Speicher kommen in beiden Fällen getrennte Lade- und Entladezähler hinzu.
Beim Smartmeter ergibt sich die Energie eines Intervalls aus:

```text
PV-Erzeugung = lokaler Verbrauch + Speicherladung + Netzeinspeisung
              − Netzbezug − Speicherentladung
```

Ohne Speicher sind Lade- und Entladeenergie null. Die Bilanz muss den gesamten
betrachteten Standort umfassen. Ein zusätzlich gewählter PV-Plausibilitätszähler
kann einen Widerspruch aufdecken; er ersetzt oder korrigiert den abgeleiteten
PV-Wert nicht. Die festen Plausibilitätsgrenzen stehen im
[Mess- und Bilanzvertrag](decisions/0001-accounting-and-input-contract.md#33-smartmeterbasierte-topologie).

## Einrichtung, Optionen und Neukonfiguration

| Ablauf | Einstellbare Bereiche | Reihenfolge |
| --- | --- | --- |
| **Neuen Helfer anlegen** | Alle Quellen, Topologie, Speicher, Verbraucher und CO₂-Faktoren. | Topologie → Energiequellen → Speicherwahl → gegebenenfalls Speicherquellen → Verbrauchsmodell → Lastzähler → zusätzliche Verbraucher → CO₂-Faktoren. |
| **Optionen des vorhandenen Helfers** | Verbrauchsmodell, Lastzähler, zusätzliche Verbraucher, Netz-CO₂-Quelle, deren Höchstalter und Herstellungsfaktoren. | Verbrauchsmodell → Lastzähler → zusätzliche Verbraucher → CO₂-Faktoren. |
| **Neu konfigurieren** im Menü des bestehenden Eintrags | Zusätzlich Topologie, PV-/Netzquellen, Speicherwahl, Speicherquellen, Kapazität, Wirkungsgrad und physischer Speicheraustausch. | Dieselbe vollständige Reihenfolge wie bei der Ersteinrichtung. Bei einem bereits konfigurierten Speicher kommt die ausdrückliche Identitätsauswahl hinzu. |

Die einzelnen Schritte bearbeiten zunächst einen Entwurf. Erst der Abschluss
von **CO₂-Faktoren konfigurieren** übernimmt die vollständigen Einstellungen.
Bei Änderungen wird anschließend neu geladen. Ein Abbruch vor diesem Abschluss
ändert die aktive Konfiguration und die historischen Ergebnisse nicht.

Vorhandene Werte werden beim Bearbeiten vorgeschlagen. Die Bestätigungen zum
physischen Messumfang müssen erneut abgegeben werden. Ein Vorschlag ist kein
Ersatz für eine ausdrückliche Auswahl oder Bestätigung.

## 1. Messtopologie und Energiequellen

Das erste Feld heißt in der Ersteinrichtung **Messtopologie auswählen**, bei einer
Neukonfiguration **Messtopologie ändern**.

| Feld | Bedeutung und Vorschlag |
| --- | --- |
| **Messtopologie** (`topology`) | Wähle **Wechselrichter (PV-Erzeugung gemessen)** oder **Smartmeter (PV-Erzeugung abgeleitet)**. Bei der Ersteinrichtung gibt es keine Vorauswahl; bei einer Neukonfiguration wird die bisherige Topologie vorgeschlagen. |

Im Schritt **Kumulative Energiequellen auswählen** sind alle Quellen
Energie-Entities mit dem oben beschriebenen Messvertrag:

| Feld | Sichtbarkeit und Bedeutung |
| --- | --- |
| **PV-Erzeugung** (`pv_generation`) | Nur beim Wechselrichter, dort Pflicht: kumulative PV-Energie am AC-Ausgang des Wechselrichters. |
| **Netzbezug** (`grid_import`) | In beiden Topologien Pflicht: kumulative, ausschließlich aus dem Netz bezogene AC-Energie am Netzanschlusspunkt. |
| **Netzeinspeisung** (`grid_export`) | In beiden Topologien Pflicht: kumulative, ausschließlich ins Netz eingespeiste AC-Energie am Netzanschlusspunkt. |
| **PV-Erzeugung (optionale Plausibilitätsprüfung)** (`pv_plausibility`) | Nur beim Smartmeter, optional: kumulative PV-Energie zur Prüfung der abgeleiteten Erzeugung. Auch diese Quelle muss vollständig synchronisiert und aktuell sein. |
| **Ich bestätige, dass alle ausgewählten Quellen den Synchronitätsvertrag erfüllen** (`synchronous_sources_confirmed`) | Ausdrücklich anzukreuzen. Bestätigt dieselbe atomare Messung und das exakte gemeinsame `co2saver_period_end` aller ausgewählten Energierollen. Anfangs nicht angekreuzt. |

Es werden keine Quell-Entities automatisch gewählt. Bei einer Neukonfiguration
werden die bisherigen Quellen unter ihren aktuellen Entity-IDs vorgeschlagen.
Eine bereits von einem anderen CO2-Saver-Eintrag verwendete Netzgrenze kann nicht
nochmals als eigene Anlage angelegt werden.

## 2. Stromspeicher

| Feld | Auswahl und Vorschlag |
| --- | --- |
| **Stromspeicher** (`battery_present`) | **Ohne Stromspeicher** oder **Mit Stromspeicher** ausdrücklich auswählen. Bei der Ersteinrichtung gibt es keine Vorauswahl, bei einer Neukonfiguration wird die bisherige Wahl vorgeschlagen. |

**Ohne Stromspeicher** führt direkt zum Verbrauchsmodell. **Mit Stromspeicher**
öffnet **Stromspeicher konfigurieren**:

| Feld | Einheit, Grenzen und Bedeutung | Vorschlag |
| --- | --- | --- |
| **Speicherladung** (`battery_charge`) | Kumulative, richtungsgetrennte Energie am **AC-Eingang** des Speichers; `Wh`, `kWh` oder `MWh`. | Bisherige Quelle beim Bearbeiten, sonst keine. |
| **Speicherentladung** (`battery_discharge`) | Kumulative, richtungsgetrennte Energie am **AC-Ausgang** des Speichers; `Wh`, `kWh` oder `MWh`. | Bisherige Quelle beim Bearbeiten, sonst keine. |
| **Nutzbare Ausgangskapazität** (`usable_capacity_kwh`) | Nutzbare Energie am AC-Ausgang, **0.1 bis 1000 kWh**, einschließlich beider Grenzen. Eine DC-Nennkapazität darf nicht ohne passenden Bezug als AC-Ausgangskapazität übernommen werden. | Bisheriger Wert beim Bearbeiten; bei einer neuen Batterie kein Standardwert. |
| **AC-Rundtrip-Wirkungsgrad** (`round_trip_efficiency_percent`) | Prozentwert **größer als 0 bis einschließlich 100**. Beschreibt die Umwandlung von gemessener AC-Ladeenergie in am Ausgang nutzbare Speicherenergie. | **90 %** bei einer neuen Batterie; der Wert muss im Formular bestätigt werden. Beim Bearbeiten der bisherige Wert. |
| **Ich bestätige die Richtungen der Speicherquellen und den Synchronitätsvertrag** (`battery_sources_confirmed`) | Bestätigt getrennte, korrekt zugeordnete Lade- und Entladerichtungen sowie dieselbe atomare Messperiode wie bei den anderen Energiequellen. | Anfangs nicht angekreuzt; Bestätigung erforderlich. |
| **Physischer Speicher** (`battery_identity`) | Nur beim Neukonfigurieren eines bereits vorhandenen Speichers: **Derselbe physische Speicher** oder **Physischer Speicher wurde ersetzt**. Die Auswahl ist auch dann erforderlich, wenn die Zähler-Entities gleich geblieben sind. | Keine Vorauswahl. |

Kapazität und Wirkungsgrad werden als einfache Dezimalzahlen eingegeben:
beispielsweise `10.5` und `90`, ohne Einheit im Textfeld. Verwende einen **Punkt**
statt eines Kommas; Leerzeichen, Exponentialschreibweise wie `1e2` sowie Angaben
wie `NaN` sind nicht zulässig.

CO2 Saver fragt keinen anfänglichen Ladezustand und keinen angenommenen PV-Anteil
ab. Unbekannter Anfangsbestand wird konservativ behandelt. **Speicherladung
bucht keine CO₂-Ersparnis.** Erst später an eine lokale Last abgegebene, sicher
PV-stämmige Energie kann eine Gutschrift erhalten. Netzladung, Verluste und
Entladung ins Netz begründen keine solche Gutschrift. Die
[Referenzzeitreihe](accounting-reference.md) zeigt das Herkunftskonto und die
verzögerten Herstellungsbelastungen mit exakten Zahlen.

Bei einem physischen Austausch ist **Physischer Speicher wurde ersetzt** zu
wählen, auch wenn die alten Zähler weiterverwendet werden. So kann die Herkunft
des alten Bestands nicht als Bestand der Ersatzbatterie fortgeführt werden.

## 3. Haushalt und zusätzliche Verbraucher

Im Schritt **Messung des lokalen Verbrauchs auswählen** gibt es genau ein Feld:

| Feld | Auswahl und Vorschlag |
| --- | --- |
| **Messung des lokalen Verbrauchs** (`mode`) | **Gesamtmessung mit Anteilen** (`aggregate_shares`) oder **Separate Zähler** (`separate_meters`). Bei der Ersteinrichtung keine Vorauswahl; beim Bearbeiten wird der bisherige Modus vorgeschlagen. |

Die Modelle unterscheiden sich im Umfang des Lastzählers:

| Gewünschte Zuordnung | Passende Einrichtung | Beispiel eines Intervalls |
| --- | --- | --- |
| **Nur Haushalt** | Wähle den passenden Lastzähler und schließe das Verbrauchermenü ohne zusätzliche Verbraucher ab. Ohne zusätzliche Verbraucher können beide Modi einen reinen Haushaltszähler verwenden. | `4 kWh` lokale Last gehören vollständig zum Haushalt. |
| **Haushalt und Wallbox sind im Gesamtzähler enthalten** | **Gesamtmessung mit Anteilen**: ein Lastzähler einschließlich Wallbox; die Wallbox als benannten Verbraucher mit festem Prozentanteil ergänzen. | Gesamtlast `4 kWh`, Wallboxanteil `25 %`: Wallbox `1 kWh`, Haushalt `3 kWh`. Es wird kein zusätzlicher Wallboxzähler addiert. |
| **Haushalt und Wallbox werden überschneidungsfrei gemessen** | **Separate Zähler**: Haushaltszähler ohne Wallbox; zusätzlich einen eigenen Wallboxzähler auswählen und seinen Messumfang bestätigen. | Haushalt `3 kWh` plus Wallbox `1 kWh` ergibt `4 kWh` lokale Last. |

Die Prozentangabe teilt **gemessene Energie**, nicht pauschal die
CO₂-Ersparnis. Verbraucher erhalten unabhängig nachweisbare PV-Untergrenzen.
Deshalb kann ein Teil der systemweit anrechenbaren Energie keinem einzelnen
Verbraucher sicher zugeordnet werden. Ein Wallboxzähler darf im separaten Modus
nicht zusätzlich zu einem Haushaltszähler verwendet werden, der dieselbe Wallbox
bereits enthält.

### Lastzähler auswählen

| Feld | Bedeutung |
| --- | --- |
| **Zähler für gesamten lokalen Verbrauch** (`household_source`) | Im Anteilsmodus: kumulative Energie für Haushalt **und alle zusätzlichen Verbraucher zusammen**. Speicherladung und Netzeinspeisung sind ausgeschlossen. |
| **Ich bestätige den gesamten lokalen Messumfang und den Synchronitätsvertrag** (`load_measurement_confirmed`) | Im Anteilsmodus ausdrücklich bestätigen, dass der Zähler den beschriebenen Gesamtumfang und dieselbe atomare Messung wie die übrigen Quellen erfasst. Anfangs nicht angekreuzt. |
| **Zähler nur für Haushaltsverbrauch** (`household_source`) | Bei separaten Zählern: kumulative Energie **nur des Haushalts**, ohne zusätzliche Verbraucher, Speicherladung und Netzeinspeisung. |
| **Ich bestätige den reinen Haushaltsumfang und den Synchronitätsvertrag** (`load_measurement_confirmed`) | Bei separaten Zählern ausdrücklich bestätigen, dass der Haushalt keinen zusätzlichen Verbraucher überlappt und die gemeinsame Messperiode eingehalten wird. Anfangs nicht angekreuzt. |

Für beide Zähler gelten `Wh`, `kWh` oder `MWh` und der vollständige
[Energiequellenvertrag](#messdaten-vor-der-einrichtung-prüfen). Eine bestehende
Quelle wird beim Bearbeiten vorgeschlagen; nach einem Moduswechsel muss sie neu
gewählt werden.

### Zusätzliche Verbraucher verwalten

Das Menü **Zusätzliche Verbraucher** zeigt den aktuellen Entwurf. Das Feld
**Aktion** (`action`) bietet **Verbraucher hinzufügen**, **Verbraucher bearbeiten**,
**Verbraucher entfernen** und **Verbraucherentwurf abschließen**. Ein Abschluss
mit leerer Liste ist zulässig.

Beim Hinzufügen oder beim Bearbeiten der Details erscheinen:

| Feld | Sichtbarkeit, Eingabe und Grenzen |
| --- | --- |
| **Name** (`name`) | In beiden Modi Pflicht: ein nicht leerer Anzeigename, etwa `Wallbox`. Umgebende Leerzeichen werden entfernt. Beim Bearbeiten wird der bestehende Name vorgeschlagen. |
| **Anteil am gesamten lokalen Verbrauch** (`share_percent`) | Nur im Anteilsmodus: **0 bis 100 %**, einschließlich beider Grenzen. Einfache Dezimalzahl mit Punkt, ohne Leerzeichen oder Exponentialschreibweise. Alle zusätzlichen Anteile zusammen dürfen **höchstens 100 %** betragen; der Rest gehört zum Haushalt. Beim Hinzufügen kein Standardwert, beim Bearbeiten der bisherige Anteil. |
| **Separater Verbrauchszähler** (`source`) | Nur bei separaten Zählern: eigene kumulative Energiequelle in `Wh`, `kWh` oder `MWh`, ausschließlich für diesen Verbraucher. Beim Hinzufügen keine Vorauswahl, beim Bearbeiten die bisherige Quelle. |
| **Ich bestätige, dass dieser separate Verbrauchszähler nicht überlappt und synchronisiert ist** (`consumer_measurement_confirmed`) | Nur bei separaten Zählern: bestätigt den ausschließlichen Messumfang ohne Haushalt, andere Verbraucher, Speicherladung oder Netzeinspeisung und dieselbe atomare Messperiode. Anfangs nicht angekreuzt; ausdrücklich erforderlich. |

**Verbraucher bearbeiten** öffnet zunächst die Auswahl **Zusätzlicher
Verbraucher** (`consumer_id`); danach folgen die obigen Detailfelder. Du wählst
dabei einen vorhandenen Listeneintrag und musst keine technische ID eingeben.

**Verbraucher entfernen** enthält zwei Pflichtfelder:

| Feld | Bedeutung |
| --- | --- |
| **Zusätzlicher Verbraucher** (`consumer_id`) | Den zu entfernenden Listeneintrag wählen. |
| **Ich bestätige die Entfernung aus diesem Verbraucherentwurf** (`confirm_removal`) | Die Entfernung ausdrücklich bestätigen; anfangs nicht angekreuzt. Sie wird erst mit dem Abschluss des gesamten Ablaufs wirksam. |

Ein Umbenennen oder Bearbeiten erhält die Identität des Verbrauchers. Entfernen
beendet nach dem Speichern seine künftige Ergebniszeitreihe; vorhandene
historische Werte bleiben erhalten. Ein später neu hinzugefügter Verbraucher
bekommt auch bei gleichem Namen eine neue Identität.

Beim Wechsel zwischen Anteilen und separaten Zählern bleiben bestehende Namen
und Verbraucheridentitäten erhalten. Haushaltsquelle und inkompatible
Zuordnungen werden im Entwurf geleert: Wähle den Lastzähler neu und bearbeite die
verbleibenden Verbraucher, um ihre neuen Anteile oder Quellen anzugeben.

## 4. Netz-CO₂-Quelle und Herstellungsfaktoren

**CO₂-Faktoren konfigurieren** ist in allen drei Abläufen der letzte Schritt.
Die Netzintensität kommt aus einer bereits vorhandenen Sensorquelle. Es gibt
kein Feld für einen statischen Netz-CO₂-Faktor.

| Feld | Einheit, Grenzen und Bedeutung | Vorschlag |
| --- | --- | --- |
| **Netz-CO₂-Intensität (g CO₂e/kWh oder kg CO₂e/kWh)** (`grid_intensity_source`) | Registrierter, aktiver `sensor` mit endlichem, nichtnegativem CO₂e-Intensitätswert. Nach Umrechnung zulässig: **0 bis 5000 g CO₂e/kWh**; das entspricht **0 bis 5 kg CO₂e/kWh**. Die Quelle benötigt einen gültigen `last_reported`-Zeitpunkt. | Bisherige Quelle beim Bearbeiten, sonst keine. |
| **Maximales Alter der Netz-CO₂-Probe (Minuten)** (`grid_max_age_minutes`) | **Ganze Minuten von 1 bis 1440**, einschließlich beider Grenzen. Betrifft ausschließlich die Netzintensität. | **60 Minuten** bei der Ersteinrichtung; beim Bearbeiten der bisherige Wert. |
| **PV-Herstellungsfaktor (g CO₂e/kWh)** (`pv_factor`) | **0 bis 5000 g CO₂e/kWh**, pro erzeugter **AC-kWh** PV-Energie. Der Herstellungsanteil für eingelagerten PV-Strom wird mit seinem Ursprung fortgeführt; Ladeverluste gehen dadurch in die Belastung der später nutzbaren Energie ein. | Kein Herstellungsstandardwert. Ausdrücklich angeben; beim Bearbeiten wird der bisherige Wert vorgeschlagen. |
| **Speicher-Herstellungsfaktor (g CO₂e/kWh)** (`battery_factor`) | Nur mit Speicher, dann Pflicht: **0 bis 5000 g CO₂e/kWh**, pro **anrechenbar entladener AC-kWh**. Kein Faktor pro Kapazität, Ladung oder beliebigem Netzstromdurchsatz. | Kein Herstellungsstandardwert. Ausdrücklich angeben; beim Bearbeiten wird der bisherige Wert vorgeschlagen. |

Herstellungsfaktoren werden als einfache Dezimalzahlen mit Punkt eingegeben,
beispielsweise `40` oder `20.5`. Komma, Leerzeichen und Exponentialschreibweise
werden abgelehnt. `0` ist ein ausdrücklicher gültiger Wert und bedeutet, dass
für diesen Herstellungsanteil kein Abzug erfolgt.

Für die Netzquelle akzeptiert CO2 Saver `gCO2e/kWh`, `gCO2eq/kWh` und
`gCO₂e/kWh` sowie dieselben Schreibweisen mit `kg` statt `g`. Jeweils ist auch
ein Leerzeichen zwischen Masse und CO₂-Kürzel zulässig, etwa `kg CO₂e/kWh`.
Eine Einheit wie `ppm`, `g/kWh` ohne CO₂e-Bezug oder eine bloße Emissionsmasse
ist nicht kompatibel. Für diese Quelle wird keine bestimmte Geräteklasse oder
Anbieterintegration verlangt. Der Energiequellenvertrag mit
`co2saver_period_end` gilt für sie nicht; ihr Zeitpunkt ist `last_reported`.

Bei der Einrichtung wird die aktuelle Netzprobe gegen die aktuelle Uhrzeit
geprüft. Während der Auswertung darf die im jeweiligen Minutenpoll vorliegende
Probe nicht nach dem **physischen Energieintervallende** liegen und nicht älter
als das eingestellte Höchstalter sein. Eine ältere zuvor gelesene Probe wird
nicht ersatzweise verwendet. Ungültige oder fehlende Netzintensität lässt die
betroffene Energie unbewertet; spätere gültige Werte führen zu keiner
Nachbewertung. Für Speicherenergie zählt die Netzintensität bei der Entladung,
nicht bei der Ladung.

## Nach dem Speichern und bei Eingabefehlern

Wirksame Änderungen an Quellen, Verbrauchszuordnung, Faktoren oder
Speicherparametern beginnen ein neues zukünftiges Messsegment. Historische
Summen werden nicht neu berechnet. Der erste vollständige gültige Snapshot des
neuen Segments dient als Baseline; erst eine nachfolgende zulässige Differenz
kann weitere Energie buchen. Vorhandener Speicherbestand wird bei einem solchen
Segmentwechsel konservativ als unbekannt behandelt. Eine reine Umbenennung
eines Verbrauchers startet dagegen kein neues Messsegment.

Entity-IDs dürfen umbenannt werden: Die vorhandene Quellenbindung folgt dem
Registry-Eintrag. Eine andere Quelle oder einen physischen Speicheraustausch
stellst du über die dafür vorgesehenen Felder ein.

Ändert ein Energiezähler während des Betriebs seine deklarierte Einheit, wird
das betroffene Intervall verworfen und eine neue Baseline benötigt. Das gilt
auch für einen Wechsel zwischen den zulässigen Einheiten `Wh`, `kWh` und `MWh`;
historische Summen bleiben erhalten.

| Rückmeldung im Formular | Was zu prüfen ist |
| --- | --- |
| Quelle nicht registriert, deaktiviert, fehlend oder nicht verfügbar | Registry-Eintrag, Aktivierung und aktuellen Zustand der betreffenden Quelle prüfen. Eine ausgewählte Entity allein beweist noch keine gültige Messung. |
| Ungültige Geräteklasse, Zustandsklasse, Einheit oder Zahl | Die Anforderungen in der Quellen- beziehungsweise Faktortabelle prüfen. Energie, Leistung und CO₂-Konzentration sind verschiedene Größen. |
| Fehlendes oder ungültiges `co2saver_period_end`, nicht synchronisierte oder veraltete Quellen | Die messseitige Erzeugung des gemeinsamen Zeitstempels, den Messrhythmus und die Veröffentlichungszeiten korrigieren. |
| Bestätigung erforderlich | Den tatsächlichen Messumfang beziehungsweise die Richtung prüfen und das zugehörige Bestätigungsfeld setzen. |
| Anteile überschreiten 100 % | Die Summe aller zusätzlichen Anteile reduzieren; der Haushaltsanteil ergibt sich aus dem Rest. |
| Vollständiger Quellenvektor ungültig | Auch Quellen aus früheren Schritten prüfen. Zum Ändern einer dort gewählten Quelle den Entwurf abbrechen und den passenden Ablauf neu öffnen. |
| Konfiguration wurde inzwischen geändert | Den veralteten Entwurf schließen und mit den aktuellen Einstellungen neu beginnen. |
| Speichern und Zurücklesen fehlgeschlagen | Lokalen Speicherzustand prüfen und erneut versuchen. Ein unbestätigt gespeicherter Zwischenstand wird nicht als gültige neue Bilanz veröffentlicht. |

Für die exakten Bilanzregeln und den Umgang mit Resets, Ausfällen sowie
Reparaturen gilt der
[Mess- und Bilanzvertrag](decisions/0001-accounting-and-input-contract.md).
