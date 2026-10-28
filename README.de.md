# SVAN Paperless NGX Bridge für ERPNext

[English version](README.md)

Verbindet die Dateianhänge deiner ERPNext-Datensätze mit dem Dokumentenmanagement [Paperless-ngx](https://docs.paperless-ngx.com/).

Du legst fest, welche DocTypes teilnehmen, und zwar beliebige: Ausgangsrechnungen, Bestellungen, Projekte, Kunden oder ein eigener DocType. Deren Anhänge werden automatisch in Paperless NGX abgelegt. Schlagworte, Korrespondent, Dokumenttyp und Zusatzfelder kommen von dem Datensatz, an dem die Datei hängt. Spätere Änderungen auf einer Seite werden auf die andere übertragen. Die Datei bleibt in ERPNext verfügbar und öffnet sich wie gewohnt. Die App ist für ERPNext gebaut, setzt es aber nicht voraus: Sie läuft auch auf einer reinen Frappe-Site.

## Das leistet die App

- **Automatische Ablage:** Anhänge der gewählten DocTypes und Dateitypen werden mit den Metadaten ihres Datensatzes zu Paperless NGX hochgeladen. Fehlende Schlagworte, Korrespondenten, Dokumenttypen und Zusatzfelder legt die App in Paperless NGX an.
- **Metadaten in beide Richtungen:** Änderungen an Schlagworten, Partei und zugeordneten Feldern gehen an Paperless NGX. Änderungen in Paperless NGX werden in ERPNext übernommen, wenn die Zuordnung als *Bidirektional* markiert ist. Dokumenttyp und Import-Referenz werden nie in den Datensatz zurückgeschrieben; ändert man sie in Paperless NGX, wandert stattdessen die Datei (siehe Import).
- **Weniger Speicherplatz:** Nach dem Upload kann der Dateiinhalt von der Site entfernt werden. Beim Öffnen holt ERPNext ihn dann aus Paperless NGX, auf Wunsch mit Prüfung der Paperless-NGX-Berechtigungen des Benutzers.
- **Ein Dokument je Datei:** Gleiche Dateien werden in Paperless NGX nicht zusammengelegt. Das Datei-Formular zeigt Dateien mit gleichem Inhalt in seinem Seitenbereich *Dubletten*.
- **Versionen:** Eine Datei, die erneut unter demselben Namen an denselben Datensatz angehängt wird, wird eine neue Version des vorhandenen Paperless-NGX-Dokuments. Frappe kann dabei sechs Zeichen an den Namen hängen, z. B. `rechnungd3f6ee.pdf`. Eine in Paperless NGX hinzugefügte Version wird ebenfalls an den Datensatz angehängt.
- **Dateinamen:** Der Name der Originaldatei ist der Dokumenttitel in Paperless NGX, jeder Dateiname die Bezeichnung seiner Version. Wer eine Seite umbenennt, benennt die andere mit um. Ein Titel ohne Dateiendung behält die Endung der Datei.
- **Import aus Paperless NGX (optional):** Ein in Paperless NGX angelegtes Dokument mit dem Dokumenttyp und der Referenz (z. B. der Auftragsnummer) eines Datensatzes wird an diesen angehängt. Werden Dokumenttyp oder Referenz später in Paperless NGX geändert, wandert die Datei zum passenden Datensatz. Passt keiner, wird die Datei aus ERPNext entfernt; das Dokument bleibt in Paperless NGX.
- **Archivieren statt löschen (Standard):** Das Löschen einer Datei in ERPNext löscht in Paperless NGX nichts. Die Versionsbezeichnung erhält ` (archived)`, der Dokumenttitel ebenso, sobald die letzte Datei weg ist (aus `Rechnung.pdf` wird `Rechnung (archived).pdf`). Archivierte Dokumente und Versionen werden nicht erneut importiert, bis die Markierung in Paperless NGX entfernt wird. Ist die Option aus, löscht das Löschen einer Datei auch ihre Version bzw. ihr Dokument in Paperless NGX. In beiden Fällen wird das Löschen abgelehnt, solange Paperless NGX nicht erreichbar ist.
- **Löschungen in Paperless NGX:** Ein in Paperless NGX gelöschtes Dokument (auch das Verschieben in den Papierkorb) entfernt seine Dateien in ERPNext, eine gelöschte Version ihre Datei.
- **Die Whitelist entscheidet:** Nur Dateien von DocTypes auf der Whitelist werden abgeglichen, in beide Richtungen. Wird ein DocType von der Whitelist genommen, bleiben seine Dateien auf beiden Seiten unverändert. Kommt er wieder dazu, gehen ihre Metadaten erneut an Paperless NGX.

Gut zu wissen: Frappes Anhangliste zeigt Anhänge gleichen Namens nur einmal (den neuesten), und ihr Löschknopf dort löscht alle davon.

## Voraussetzungen

- Frappe v16 (ERPNext v16 optional), Python 3.14 oder neuer
- Paperless NGX 3.0 oder neuer mit dem API-Token eines **Superusers**
- `PAPERLESS_CONSUMER_DELETE_DUPLICATES` muss in Paperless NGX **aus** bleiben (Standard), denn jede Datei braucht ein eigenes Dokument
- Optional: App `svan_base`. Die App erscheint dann unter dem Eintrag *SVAN* statt für sich allein.

## Installation

Nimm den Branch zu deiner Frappe-Hauptversion: `version-16` für Frappe v16 (`staging-16` enthält den nächsten Stand, bevor er in `version-16` ankommt).

```bash
cd $PFAD_ZU_DEINER_BENCH
bench get-app $URL_DIESES_REPOS --branch version-16
bench --site $SITE install-app svan_paperless_bridge
```

Die Installation legt Zusatzfelder an Datei (File) und Benutzer (User) an und füllt die Dateityp-Whitelist.

## Einrichtung

Öffne **Paperless NGX > Einstellungen** (siehe [Wo du was findest](#wo-du-was-findest)):

1. **Grundlagen:** API URL, API Token und einen Standard-Korrespondenten (wird in Paperless NGX angelegt, falls es ihn nicht gibt). Beim Speichern wird die Verbindung getestet; gehört das Token keinem Superuser, erscheint eine Warnung.
2. **DocType-Whitelist:** die DocTypes, deren Anhänge abgeglichen werden. Je Zeile:
   - *Paperless Dokumententyp-Name*, vorbelegt mit dem Namen des DocTypes.
   - Optional der *Partei-Feldname*, also das Feld mit der Partei (z. B. `customer`). Die Partei wird zum Korrespondenten. Auch ein Dynamic Link geht, z. B. `party_name` am Angebot. Ohne Partei-Feld gilt der Standard-Korrespondent.

   Eine leere Whitelist gleicht nichts ab.
3. **Dateityp-Whitelist:** vorbelegt mit den Typen, die Paperless NGX von Haus aus verarbeitet. Eine leere Liste gleicht nichts ab.
4. **Nach der Übertragung / Löschen von Dateien / Vorschau-Zugriffskontrolle:**
   - ob der Inhalt nur in Paperless NGX vorgehalten wird (Standard: an),
   - ob Löschen stattdessen archiviert (Standard: an),
   - ob die Vorschau die Dokumentberechtigungen von Paperless NGX prüft (Standard: an). Dafür hinterlegt jeder Benutzer ein persönliches API-Token in seinem Benutzer-Formular (Abschnitt *SVAN Paperless NGX*).
5. **Zusatzfelder (optional):** Unter **Mapping > Zusatzfelder** legst du fest, welche ERPNext-Felder welches Paperless-NGX-Zusatzfeld befüllen. Ein Zusatzfeld kann aus mehreren DocTypes gespeist werden. Auch die ID (`name`) des Datensatzes kann Quelle sein.
6. **Rückmeldung von Paperless NGX (optional, empfohlen):** Klicke im Abschnitt *Rückmeldung von Paperless NGX (Webhook)* auf *Secret erzeugen*. Lege danach in Paperless NGX einen Workflow mit dem Auslöser *Document Updated* und einer Webhook-Aktion an, mit den Werten aus dem Hilfetext dieses Abschnitts. Ohne den Webhook erfährt ERPNext von Änderungen in Paperless NGX nur über den zeitgesteuerten Abgleich (gelöschte Dokumente und Versionen, neue Versionen, Titel und Bezeichnungen, gelöschte oder umbenannte Schlagworte, Zusatzfelder und Korrespondenten) und den täglichen Import. Schlagworte, Korrespondent und Zusatzfeld-Werte eines einzelnen Dokuments kommen dann nicht zurück.
7. **Dateiimport (optional):** Wähle im Abschnitt *Dateiimport aus Paperless NGX* eine Zusatzfeld-Zuordnung als *Referenz für den Import*. Optional kommt ein *Import-Schlagwort* hinzu, das ein Dokument tragen muss. Jeder DocType der Whitelist braucht eine Quelle in dieser Zuordnung. Solange die Referenz gesetzt ist, werden Dateien eines DocTypes ohne Quelle nicht übertragen. Beim Speichern warnt ein Dialog vor solchen Lücken und vor Dateien, deren Referenz erst an Paperless NGX gehen muss. *Versuche, solange kein Business-Dokument gefunden wird* (Standard 10) begrenzt, wie oft ein Dokument ohne passenden Datensatz erneut versucht wird.

Ab dann arbeiten zeitgesteuerte Aufträge, siehe [Wann übertragen wird](#wann-übertragen-wird). Die Schaltflächengruppen *Synchronisation* und *Wartung* in den Einstellungen starten einzelne Schritte von Hand.

## Wo du was findest

Einstellungen und Listen liegen in der Seitenleiste **Paperless NGX**. So kommst du hin:

| Aufbau | Weg |
|---|---|
| Mit `svan_base`, App-Navigation (ab Frappe 16.50) | SVAN-Leiste > **Paperless NGX** |
| Mit `svan_base`, Desktop-Symbole | **SVAN** > **Paperless NGX** |
| Ohne `svan_base`, App-Navigation | App-Übersicht > **SVAN Paperless NGX Bridge** |
| Ohne `svan_base`, Desktop-Symbole | **Paperless NGX**, oder **SVAN** > **Paperless NGX**, wenn eine andere SVAN-App diesen Ordner angelegt hat |

| Wo | Was |
|---|---|
| **Einstellungen** | Verbindung, Whitelists, Optionen, Webhook, Import, Schaltflächen für manuelle Läufe |
| **Dateityp** | Bekannte Dateitypen und ihre MIME-Typen |
| **Mapping > Schlagworte / Zusatzfelder / Korrespondenten** | Welches ERPNext-Schlagwort, -Feld bzw. welche Partei welchem Eintrag in Paperless NGX entspricht, und der Schalter *Bidirektional* je Zeile. Schlagwort- und Korrespondenten-Zuordnungen entstehen automatisch, Zusatzfeld-Zuordnungen von Hand. |
| **Sync Log** | Jeder Übertragungsvorgang und jeder Fehler; *Aktionen > Als JSON exportieren* lädt die ausgewählten Einträge herunter. Einträge älter als 90 Tage entfernt Frappes Log-Bereinigung. |
| **Datei**-Formular | Feld *Sync-Status*, Seitenbereich *Dubletten*. Die Vorschau funktioniert wie zuvor, auch wenn der Inhalt aus Paperless NGX kommt. |
| **Benutzer**-Formular | Abschnitt *SVAN Paperless NGX*: persönliches API-Token |

Sync-Status einer Datei: leer = wird nicht abgeglichen, `pending` = wartet, `syncing` = läuft, `synced` = erledigt. `broken` steht für einen bekannten, nicht lösbaren Fall, z. B. einen Dateityp, den Paperless NGX ablehnt. `deleted` steht für eine Datei, deren Paperless-NGX-Dokument schon gelöscht ist, während ihr Löschen in ERPNext noch aussteht.

<details>
<summary><b>Schaltflächen in den Einstellungen</b></summary>

Nur für System-Manager und nur bei gespeicherten Einstellungen. Bis auf *Import-Warteschlange zurücksetzen* und *Broken zurücksetzen* startet jede Schaltfläche einen Hintergrundauftrag und kehrt sofort zurück. Ergebnisse stehen im **Sync Log**.

**Synchronisation** (die zeitgesteuerten Schritte, sofort und ohne deren Zeit- und Mengenbegrenzung):

| Schaltfläche | Was sie tut | Wann sinnvoll |
|---|---|---|
| Neue Dateien übertragen | Lädt alle Dateien hoch, die auf die Übertragung warten | Du willst nicht auf den nächsten Fünf-Minuten-Lauf warten |
| Ausstehende Übertragungen abschließen | Fragt bei Paperless NGX nach, ob hochgeladene Dateien verarbeitet sind, und schließt sie ab (Status `synced`, Inhalt ggf. entfernt) | Dateien hängen in `syncing` |
| Metadaten jetzt aktualisieren | Schickt ausstehende Metadaten-Änderungen (Schlagworte, Partei, Zusatzfelder, Dateinamen) an Paperless NGX | Direkt nach Änderungen an vielen Datensätzen oder an der Referenz |
| Globale Metadaten-Änderungen abgleichen | Übernimmt in Paperless NGX gelöschte oder umbenannte Schlagworte und Zusatzfelder sowie umbenannte Korrespondenten | Nach Aufräumarbeiten in Paperless NGX |
| Jetzt mit Paperless NGX abgleichen | Entfernt Dateien, deren Dokument oder Version in Paperless NGX gelöscht wurde, hängt neue Versionen an und übernimmt geänderte Titel und Bezeichnungen | Nach Löschen oder Wiederherstellen in Paperless NGX |
| Jetzt aus Paperless NGX importieren | Sucht in Paperless NGX nach Dokumenten, die seit dem letzten Lauf geändert wurden, und importiert oder verschiebt sie | Ohne Webhook, oder wenn ein Import nicht auf den täglichen Lauf warten soll |

**Wartung:**

| Schaltfläche | Was sie tut | Wann sinnvoll |
|---|---|---|
| Import-Warteschlange zurücksetzen | Gibt gestoppten Importen (kein oder mehrere passende Datensätze, Fehler) neue Versuche | Nachdem der fehlende Datensatz angelegt oder die Referenz korrigiert wurde |
| Broken zurücksetzen | Setzt alle Dateien mit Status `broken` zurück, damit sie erneut geprüft und übertragen werden | Nachdem die Ursache behoben ist, z. B. ein neuer Dateityp in Paperless NGX |
| Jetzt klassifizieren | Prüft anhand der Whitelists, welche Dateien zu übertragen sind, ohne zu übertragen | Um die Wirkung einer Whitelist-Änderung sofort zu sehen |
| Hängengebliebene Löschungen abschließen | Schließt Löschungen in ERPNext ab, deren Paperless-NGX-Teil schon gelungen ist | Dateien bleiben mit Status `deleted` stehen |
| Physische Dateien rückwirkend löschen | Entfernt den lokalen Inhalt bereits übertragener Dateien, die ihn noch haben (mit Rückfrage) | Nach dem Einschalten von *Dateiinhalt nur in Paperless NGX vorhalten* |

*Secret erzeugen* neben dem Feld *Webhook-Secret* erzeugt ein neues Secret. Trage es auch im Workflow von Paperless NGX ein.

</details>

## Wann übertragen wird

<details>
<summary><b>Sofort oder mit dem nächsten Lauf</b></summary>

Die zeitgesteuerten Aufträge laufen alle fünf Minuten, der Import einmal täglich als Sicherheitsnetz.

**ERPNext nach Paperless NGX**

| Ereignis | Wann |
|---|---|
| Datei angehängt | Ein Versuch sofort, scheitert er, mit dem nächsten Lauf. Paperless NGX verarbeitet den Upload selbstständig. Der nächste Lauf setzt die Datei auf `synced` und entfernt ggf. den Inhalt. |
| Schlagworte, Partei oder zugeordnete Felder des Datensatzes geändert, Datensatz oder Datei umbenannt | Sofort vorgemerkt, mit dem nächsten Lauf übertragen |
| ERPNext-Schlagwort oder Parteiname umbenannt | Sofort (Schlagwort bzw. Korrespondent in Paperless NGX wird umbenannt) |
| Datei gelöscht | Sofort (Archiv-Markierung bzw. Löschen in Paperless NGX) |
| Whitelist geändert | Mit dem nächsten Lauf; Dateien eines wieder aufgenommenen DocTypes schicken ihre Metadaten erneut |

**Paperless NGX nach ERPNext**

| Ereignis | Wann |
|---|---|
| Schlagworte, Korrespondent, Zusatzfeld-Werte, Titel oder Versionsbezeichnung eines Dokuments geändert | Sofort per Webhook. Ohne Webhook kommen Titel und Bezeichnungen mit dem nächsten Lauf, der Rest gar nicht. |
| Version hinzugefügt oder gelöscht | Sofort per Webhook, sonst mit dem nächsten Lauf |
| Dokument gelöscht (auch in den Papierkorb), Dokument oder Version aus dem Papierkorb wiederhergestellt | Mit dem nächsten Lauf (Paperless NGX meldet beides nicht) |
| Schlagwort oder Zusatzfeld gelöscht bzw. umbenannt, Korrespondent umbenannt | Mit dem nächsten Lauf (Paperless NGX meldet beides nicht) |
| Dokument mit Dokumenttyp und Referenz eines Datensatzes (Import), oder dessen Dokumenttyp bzw. Referenz geändert | Sofort, sobald Paperless NGX per Webhook eine Änderung des Dokuments meldet, sonst einmal täglich |

</details>

## Aktualisieren

1. Sicherung inklusive Dateien anlegen. Liegt der Inhalt nur in Paperless NGX, auch Paperless NGX sichern.
2. Neue Version des App-Branches holen, z. B. `git -C apps/svan_paperless_bridge pull`.
3. `bench --site $SITE migrate` (aktualisiert auch die Zusatzfelder und die zeitgesteuerten Aufträge) und `bench build --app svan_paperless_bridge` ausführen.
4. Bench neu starten (`bench restart`).

`bench update` erledigt die Schritte 2 bis 4 für alle Apps der Bench auf einmal.

Ein Update von einer Version ohne *Archivieren statt löschen* schaltet die Option ein. Danach löscht das Löschen einer Datei in ERPNext nichts mehr in Paperless NGX. Wer das bisherige Verhalten behalten will, schaltet sie in den Einstellungen aus.

## Deinstallieren

Vorher den Inhalt der Dateien zurückholen, die nur in Paperless NGX liegen. Sonst liegt ihre einzige Kopie in Paperless NGX, und die Dateien lassen sich in ERPNext nicht mehr öffnen:

```bash
bench --site $SITE execute svan_paperless_bridge.utils.maintenance.restore_physical_files_from_paperless
bench --site $SITE uninstall-app svan_paperless_bridge
```

Die Deinstallation entfernt die Zusatzfelder der App an Datei und Benutzer. Die Dokumente in Paperless NGX bleiben.

## Entwicklung

Quelltexte sind englisch, die deutsche Übersetzung steht in `svan_paperless_bridge/locale/de.po`. Der Code-Stil wird per `pre-commit` durchgesetzt (`pre-commit install`). Architektur und Interna: [AGENTS.md](AGENTS.md).

## Lizenz

GPL-3.0
