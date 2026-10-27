# SVAN Paperless NGX Bridge for ERPNext

[Deutsche Version](README.de.md)

Connects the file attachments of your ERPNext business documents with the document management system [Paperless-ngx](https://docs.paperless-ngx.com/).

You decide which DocTypes take part, any of them: sales invoices, purchase orders, projects, customers, or a DocType of your own. Their attachments are filed in Paperless NGX automatically. Tags, correspondent, document type and custom fields come from the business document the file is attached to. Later changes on either side are carried over to the other. The file stays available in ERPNext and opens as usual. The app is built for ERPNext but does not require it: it also runs on a plain Frappe site.

## What it does

- **Automatic filing:** attachments of the chosen DocTypes and file types are uploaded to Paperless NGX together with the metadata of their business document. Tags, correspondents, document types and custom fields missing in Paperless NGX are created there.
- **Metadata in both directions:** changes to tags, party and mapped fields are sent to Paperless NGX. Changes made in Paperless NGX are applied in ERPNext if the mapping is marked *Bidirectional*. Document type and import reference are never written back to the business document; changing them in Paperless NGX moves the file instead (see the import below).
- **Less disk space:** after the upload the file content can be removed from the site. Opening the file then fetches it from Paperless NGX, with an optional check of the user's Paperless NGX permissions.
- **One document per file:** identical files are not merged in Paperless NGX. The File form lists files with identical content in its sidebar section *Duplicates*.
- **Versions:** a file attached again to the same business document under the same name becomes a new version of the existing Paperless NGX document. Frappe may append six characters to the name, e.g. `invoiced3f6ee.pdf`. A version added in Paperless NGX is attached to the business document as well.
- **File names:** the name of the original file is the document title in Paperless NGX, and every file name is the label of its version. Renaming one side renames the other. A title without extension keeps the file's extension.
- **Import from Paperless NGX (optional):** a document created in Paperless NGX that carries the document type and the reference (e.g. the order number) of a business document is attached to it. If document type or reference are changed in Paperless NGX later, the file moves to the matching business document. If none matches, the file is removed from ERPNext and the document stays in Paperless NGX.
- **Archive instead of deletion (default):** deleting a file in ERPNext deletes nothing in Paperless NGX. The version label gets ` (archived)`, and so does the document title once its last file is gone (`Invoice.pdf` becomes `Invoice (archived).pdf`). Archived documents and versions are not imported again until the mark is removed in Paperless NGX. With this option off, deleting a file deletes its version or document in Paperless NGX. In both cases the deletion is refused while Paperless NGX cannot be reached.
- **Deletions in Paperless NGX:** a document deleted in Paperless NGX (moving it to the trash counts) removes its files in ERPNext, a deleted version removes its file.
- **The whitelist decides:** only files of whitelisted DocTypes are synced, in either direction. Removing a DocType from the whitelist leaves its files untouched on both sides. Adding it again sends their metadata to Paperless NGX once more.

Limits worth knowing: Frappe's attachment list shows attachments of the same name only once (the newest), and its delete button there deletes all of them.

## Requirements

- Frappe v16 (ERPNext v16 optional), Python 3.14 or newer
- Paperless NGX 3.0 or newer with an API token of a **superuser**
- `PAPERLESS_CONSUMER_DELETE_DUPLICATES` must stay **off** in Paperless NGX (the default), because every file needs a document of its own
- Optional: app `svan_base`. The app then appears below the *SVAN* entry instead of on its own.

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch development
bench --site $SITE install-app svan_paperless_bridge
```

The installation adds custom fields to File and User and fills the file type whitelist.

## Configuration

Open **Paperless NGX > Settings** (see [Where to find it](#where-to-find-it)):

1. **Basics:** API URL, API token and a default correspondent. Saving tests the connection and checks that the token belongs to a superuser.
2. **DocType Whitelist:** the DocTypes whose attachments are synced. Per row:
   - *Paperless Document Type Name*, which defaults to the DocType's name.
   - Optionally the *Party Fieldname*, the field holding the party (e.g. `customer`). The party becomes the correspondent. A Dynamic Link works as well, e.g. `party_name` on Quotation.

   An empty whitelist syncs nothing.
3. **File Type Whitelist:** prefilled with the types Paperless NGX handles out of the box. An empty list syncs nothing.
4. **After The Transfer / Deleting Files / Preview Access Control:** whether the content is kept only in Paperless NGX, whether deleting archives instead (both on by default), and whether the preview checks Paperless NGX document permissions. For that check each user stores a personal API token in their user form (section *SVAN Paperless NGX*).
5. **Custom fields (optional):** under **Mapping > Custom Fields** choose which ERPNext fields feed which Paperless NGX custom field. One custom field may be fed from several DocTypes. The ID (`name`) of the business document can be a source too.
6. **Feedback from Paperless NGX (optional, recommended):** click *Generate Secret* in the section *Feedback From Paperless NGX (Webhook)*. Then create a workflow in Paperless NGX with trigger *Document Updated* and a webhook action, using the values listed in the help text of that section. Without it, ERPNext only learns of deletions, new versions and renamed tags, custom fields or correspondents; changes to a single document's metadata do not come back.
7. **File import (optional):** in the section *File Import From Paperless NGX* choose a custom field mapping as *Reference For Import*, and optionally an *Import Tag* that a document must carry. Every whitelisted DocType needs a source in that mapping; while the reference is set, files of a DocType without one are not transferred. When you save, a dialog warns about such gaps and about files whose reference has to be sent to Paperless NGX first.

From then on, scheduled jobs do the work, see [When things are transferred](#when-things-are-transferred). The button groups *Sync* and *Maintenance* in the Settings start single steps by hand.

## Where to find it

The settings and lists sit in the sidebar **Paperless NGX**. You reach it as follows:

| Setup | Way there |
|---|---|
| With `svan_base`, app navigation (Frappe 16.50 or newer) | SVAN rail > **Paperless NGX** |
| With `svan_base`, desktop icons | **SVAN** > **Paperless NGX** |
| Without `svan_base`, app navigation | App screen > **SVAN Paperless NGX Bridge** |
| Without `svan_base`, desktop icons | **Paperless NGX**, or **SVAN** > **Paperless NGX** if another SVAN app has created that folder |

| Where | What |
|---|---|
| **Settings** | Connection, whitelists, options, webhook, import, buttons for manual runs |
| **File Type** | Known file types and their MIME types |
| **Mapping > Tags / Custom Fields / Correspondents** | Which ERPNext tag, field or party corresponds to which Paperless NGX entry, and the *Bidirectional* switch per row. Tag and correspondent mappings are created automatically, custom field mappings by hand. |
| **Sync Log** | Every transfer event and error; *Actions > Export as JSON* downloads the selected entries |
| **File** form | Field *Sync Status*, sidebar section *Duplicates*. The preview works as before, even when the content comes from Paperless NGX. |
| **User** form | Section *SVAN Paperless NGX*: personal API token |

Sync status of a file: empty = not synced, `pending` = waiting, `syncing` = in progress, `synced` = done. `broken` marks a known unsolvable case, e.g. a file type Paperless NGX rejects. `deleted` marks a file whose Paperless NGX document is already gone while its deletion in ERPNext is still pending.

<details>
<summary><b>Buttons in the Settings</b></summary>

Only System Managers can use them. Except *Reset Import Queue* and *Reset Broken*, every button starts a background job and returns at once. Results appear in the **Sync Log**.

**Sync** (the scheduled steps, at once and without their time and batch limits):

| Button | What it does | When to use it |
|---|---|---|
| Transfer New Files | Uploads all files waiting for transfer | You do not want to wait for the next five-minute run |
| Finalize Pending Transfers | Asks Paperless NGX whether uploaded files are processed and completes them (status `synced`, content removed if so configured) | Files hang in `syncing` |
| Update Metadata Now | Sends pending metadata changes (tags, party, custom fields, file names) to Paperless NGX | Right after changing many business documents or the reference |
| Reconcile Global Metadata Changes | Applies tags and custom fields deleted or renamed and correspondents renamed in Paperless NGX | After clean-up work in Paperless NGX |
| Compare With Paperless NGX Now | Removes files whose document or version was deleted in Paperless NGX, attaches new versions and takes over renamed titles and labels | After deleting or restoring in Paperless NGX |
| Import From Paperless NGX Now | Looks for documents changed in Paperless NGX since the last run and imports or moves them | Without webhook, or when an import should not wait for the daily run |

**Maintenance:**

| Button | What it does | When to use it |
|---|---|---|
| Reset Import Queue | Gives imports that stopped (no or several matching business documents, error) new attempts | After creating the missing business document or fixing the reference |
| Reset Broken | Sets all files with status `broken` back, so they are checked and transferred again | After the cause was fixed, e.g. a new file type in Paperless NGX |
| Classify Now | Checks against the whitelists which files are to be transferred, without transferring | To see the effect of a whitelist change at once |
| Finalize Stuck Deletions | Completes deletions in ERPNext whose Paperless NGX part has already succeeded | Files remain with status `deleted` |
| Delete Physical Files Retroactively | Removes the local content of already transferred files that still have it (asks first) | After switching on *Keep File Content Only In Paperless NGX* |

*Generate Secret* next to the field *Webhook Secret* creates a new secret. Enter it in the Paperless NGX workflow as well.

</details>

## When things are transferred

<details>
<summary><b>Immediately or with the next run</b></summary>

The scheduled jobs run every five minutes. The import runs once a day as a safety net.

**ERPNext to Paperless NGX**

| Event | When |
|---|---|
| File attached | One attempt right away. If it fails, with the next run. Paperless NGX processes the upload on its own; the next run marks the file `synced` and removes the content if so configured. |
| Tags, party or mapped fields of the business document changed, business document or file renamed | Noted at once, sent with the next run |
| ERPNext tag or party name renamed | Immediately (the Paperless NGX tag or correspondent is renamed) |
| File deleted | Immediately (archive mark or deletion in Paperless NGX) |
| Whitelist changed | With the next run; files of a DocType added again get their metadata sent |

**Paperless NGX to ERPNext**

| Event | When |
|---|---|
| Tags, correspondent, custom field values, title or version label of a document changed | Immediately via webhook; without webhook titles and labels with the next run, the rest not at all |
| Version added or deleted | Immediately via webhook, otherwise with the next run |
| Document deleted (also to the trash), document or version restored from the trash | With the next run (Paperless NGX reports neither) |
| Tag or custom field deleted or renamed, correspondent renamed | With the next run (Paperless NGX reports neither) |
| Document with document type and reference of a business document (import), or its document type or reference changed | Immediately when Paperless NGX reports a change of the document via webhook, otherwise once a day |

</details>

## Updating

1. Take a backup including files. If the content is kept only in Paperless NGX, back up Paperless NGX as well.
2. Pull the new version and run `bench --site $SITE migrate`. This also updates the custom fields and the scheduled jobs.
3. Restart the bench (`bench restart`).

Updating from a version without *Archive Instead Of Deleting* switches it on. From then on, deleting a file in ERPNext no longer deletes anything in Paperless NGX. Switch it off in the Settings to keep the previous behaviour.

After an upgrade of Frappe or Paperless NGX, check that the Paperless NGX webhook workflow and the API token still exist.

## Uninstalling

First restore the content of the files kept only in Paperless NGX. Otherwise their only copy is in Paperless NGX and the files can no longer be opened in ERPNext:

```bash
bench --site $SITE execute svan_paperless_bridge.utils.maintenance.restore_physical_files_from_paperless
bench --site $SITE uninstall-app svan_paperless_bridge
```

Uninstalling removes the app's custom fields on File and User. The documents in Paperless NGX stay.

## Development

Source strings are English, the German translation lives in `svan_paperless_bridge/locale/de.po`. Code style is enforced by `pre-commit` (`pre-commit install`). Architecture and internals: [AGENTS.md](AGENTS.md).

## License

GPL-3.0
