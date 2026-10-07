# SVAN Paperless NGX Bridge für ERPNext

[English version](README.md)

Eine Frappe-App, die Dateianhänge und [Paperless-ngx](https://docs.paperless-ngx.com/) synchron hält.

Anhänge der von dir gewählten DocTypes (z. B. Ausgangsrechnung, Lieferantenbestellung) werden automatisch zu Paperless NGX übertragen. Schlagworte, Korrespondent, Dokumenttyp und Zusatzfelder kommen vom Beleg, an dem die Datei hängt, und spätere Änderungen auf einer der beiden Seiten werden auf die andere übertragen. Die Datei bleibt in Frappe wie gewohnt abrufbar: Der Inhalt wird aus Paperless NGX geladen.

## Das bringt die App

- **Automatischer Upload** von Anhängen nach DocType- und Dateityp-Whitelist, mit Metadaten aus dem Beleg.
- **Metadaten-Abgleich in beide Richtungen** für Schlagworte, Korrespondenten und Zusatzfeld-Werte - pro Mapping steuerbar, du entscheidest also, was Paperless NGX in Frappe ändern darf.
- **Weniger Speicherbedarf:** Nach dem Upload kann der Dateiinhalt von der Frappe-Site entfernt werden und wird bei Bedarf aus Paperless NGX geliefert.
- **Sicheres Löschen:** Das Löschen einer Datei in Frappe löscht zuerst das Dokument in Paperless NGX und wird blockiert, falls das nicht möglich ist.
- **Ein Dokument pro Datei:** Identische Dateien werden nicht zusammengelegt; Dubletten werden im File-Formular angezeigt.

## Voraussetzungen

- Frappe v16, Python 3.14 oder neuer, App `svan_base`
- Paperless NGX 3.0 oder neuer mit API-Token eines **Superuser**-Kontos
- In Paperless NGX muss `PAPERLESS_CONSUMER_DELETE_DUPLICATES` **aus** bleiben (Standard)

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch development
bench --site $SITE install-app svan_paperless_bridge
```

Die Installation legt die Zusatzfelder an File und User an und füllt die Dateityp-Whitelist.

## Einrichtung

Öffne den Workspace **Paperless NGX** (unter dem Eintrag *SVAN* im App-Menü) und dort die **Einstellungen**:

1. **Grundlagen:** API-URL und API-Token eintragen und einen Standard-Korrespondenten wählen. Beim Speichern wird die Verbindung getestet.
2. **DocType-Whitelist:** DocTypes eintragen, deren Anhänge synchronisiert werden sollen. Optional das Link-Feld mit der Partei (z. B. `customer`) angeben, die zum Korrespondenten wird. Dynamische Links gehen auch (z. B. `party_name` am Angebot, dessen Partei-Typ aus `quotation_to` kommt).
3. **Dateityp-Whitelist:** vorbefüllt mit den Typen, die Paperless NGX ohne Zusatzkonfiguration verarbeitet; bei Bedarf anpassen.
4. **Nach der Übertragung / Vorschau:** festlegen, ob die physische Datei nach der Übertragung gelöscht wird und ob für die Vorschau die Berechtigungen aus Paperless NGX geprüft werden. Für die Prüfung hinterlegt jeder Nutzer ein persönliches Token in seinem eigenen User-Formular (Abschnitt *SVAN Paperless NGX*).
5. **Rücksynchronisation (optional):** In den Einstellungen ein Webhook-Secret erzeugen und in Paperless NGX einen Workflow anlegen (Trigger *Document Updated*, Aktion *Webhook*). Die genauen Werte stehen im Hilfetext des Abschnitts *Rückmeldung von Paperless NGX (Webhook)*.

Die Dateien werden danach alle fünf Minuten per Scheduler übertragen. Die Button-Gruppe **Synchronisation** in den Einstellungen startet jeden Schritt manuell.

## Wo finde ich was?

| Wo | Was |
|---|---|
| Workspace *Paperless NGX* > **Einstellungen** | Verbindung, Whitelists, Optionen, Webhook, manuelle Sync-Buttons |
| > **Dateityp** | Bekannte Dateitypen mit MIME-Typ |
| > **Mapping** > **Schlagworte / Zusatzfelder / Korrespondenten** | Welches Frappe-Schlagwort, -Feld bzw. welche Partei zu welcher Paperless-NGX-Entität gehört, samt Schalter *Bidirektional* pro Mapping. Zusatzfeld-Mappings pflegst du von Hand, Schlagwort- und Korrespondenten-Mappings entstehen automatisch. |
| > **Sync Log** | Alle Sync-Ereignisse und Fehler |
| **File**-Formular und -Liste | *Sync-Status*, *Dokumenten-ID* und *Metadaten-Update ausstehend*. Status: `pending` > `syncing` > `synced`; `broken` steht für einen bekannten unlösbaren Fall, `deleted` für eine Datei, deren Paperless-Dokument bereits entfernt wurde |
| **File**-Formular | Die Vorschau funktioniert nach dem Sync wie zuvor, obwohl der Inhalt jetzt aus Paperless NGX kommt |
| **File**-Formular, Seitenleiste | Bereich **Dubletten**: weitere Dateien mit identischem Inhalt |
| **User**-Formular | Persönliches Paperless-NGX-API-Token |

## Aktualisieren

1. Backup inklusive Dateien erstellen. Werden physische Dateien nach der Übertragung entfernt, liegt ihre einzige Kopie in Paperless NGX - also auch dort sichern.
2. Neue Version holen und `bench --site $SITE migrate` ausführen. Das übernimmt auch geänderte Scheduler-Jobs.
3. `migrate` aktualisiert die Zusatzfelder an File und User **nicht**. Nach einem Update zusätzlich ausführen:
   `bench --site $SITE execute svan_paperless_bridge.custom_fields.create_svan_paperless_custom_fields`
4. Bench neu starten (`bench restart`). Danach das **Sync Log** auf Fehler prüfen.

Nach einem Frappe- oder Paperless-NGX-Upgrade: `migrate` wie üblich ausführen und prüfen, ob der Webhook-Workflow in Paperless NGX und das API-Token noch vorhanden sind.

**Vor dem Deinstallieren der App:** die von der Site entfernten Dateien wiederherstellen, sonst liegt ihr Inhalt nur noch in Paperless NGX:
`bench --site $SITE execute svan_paperless_bridge.utils.maintenance.restore_physical_files_from_paperless`

## Entwicklung

Quelltexte sind englisch; Deutsch steckt in `svan_paperless_bridge/locale/de.po`. Der Code-Stil wird per `pre-commit` erzwungen (`pre-commit install`). Architektur und Interna stehen in [AGENTS.md](AGENTS.md).

## Lizenz

GPL-3.0
