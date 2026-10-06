# SVAN Paperless NGX Bridge

[Deutsche Version](README.de.md)

A Frappe app that keeps file attachments and [Paperless-ngx](https://docs.paperless-ngx.com/) in sync.

Attachments of the DocTypes you choose (e.g. Sales Invoice, Purchase Order) are uploaded to Paperless NGX automatically. Tags, correspondent, document type and custom fields are taken from the business document the file is attached to, and changes made later on either side are carried over to the other. The file stays available in Frappe: you open it as usual, the content is streamed from Paperless NGX.

## What you get

- **Automatic upload** of attachments by DocType and file type whitelist, with metadata from the business document.
- **Two-way metadata sync** for tags, correspondents and custom field values - controlled per mapping, so you decide what Paperless NGX may change in Frappe.
- **Saved disk space:** after the upload the file content can be removed from the Frappe site and is served from Paperless NGX on demand.
- **Safe deletion:** deleting a File in Frappe deletes the Paperless NGX document first, and is blocked if that is not possible.
- **One document per file:** identical files are not merged; duplicates are shown on the File form.

## Requirements

- Frappe v16, Python 3.14 or newer, app `svan_base`
- Paperless NGX 3.0 or newer with an API token of a **superuser** account
- In Paperless NGX, `PAPERLESS_CONSUMER_DELETE_DUPLICATES` must stay **off** (the default)

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch development
bench --site $SITE install-app svan_paperless_bridge
```

The installation creates the custom fields on File and User and fills the file type whitelist.

## Configuration

Open the **Paperless NGX** workspace (below the *SVAN* entry in the app menu) and go to **Settings**:

1. **Basics:** enter API URL and API token and choose a default correspondent. Saving tests the connection.
2. **DocType Whitelist:** add the DocTypes whose attachments should be synced. Optionally name the link field holding the party (e.g. `customer`), which becomes the correspondent. Dynamic links work too (e.g. `party_name` on Quotation, whose party type comes from `quotation_to`).
3. **File Type Whitelist:** prefilled with the types Paperless NGX handles out of the box; adjust if needed.
4. **After the transfer / Preview:** choose whether the physical file is deleted after the transfer, and whether Paperless NGX document permissions are checked on preview. For the permission check, each user stores a personal token in their own User form (section *SVAN Paperless NGX*).
5. **Sync back (optional):** generate a webhook secret in the Settings and create a workflow in Paperless NGX (trigger *Document Updated*, action *Webhook*). The exact values are listed in the help text of the section *Feedback From Paperless NGX (Webhook)*.

Files are then transferred by scheduled jobs every five minutes. The **Sync** button group in the Settings starts each step manually.

## Where to find things

| Where | What |
|---|---|
| Workspace *Paperless NGX* > **Settings** | Connection, whitelists, options, webhook, manual sync buttons |
| > **File Type** | Known file types and their MIME types |
| > **Mapping** > **Tags / Custom Fields / Correspondents** | Which Frappe tag, field or party corresponds to which Paperless NGX entity, and the *Bidirectional* switch per mapping. Custom Field mappings are maintained by hand; tag and correspondent mappings are created automatically. |
| > **Sync Log** | Every sync event and error |
| **File** form and list | *Sync Status*, *Document ID* and *Metadata Update Pending*. Status: `pending` > `syncing` > `synced`; `broken` marks a known unsolvable case, `deleted` a file whose Paperless NGX document is already gone |
| **File** form | Preview works as before after the sync, even though the content now comes from Paperless NGX |
| **File** form, sidebar | Section **Duplicates**: other files with identical content |
| **User** form | Personal Paperless NGX API token |

## Updating

1. Take a backup including files. If physical files are removed after the transfer, their only copy lives in Paperless NGX, so back that up too.
2. Pull the new version and run `bench --site $SITE migrate`. This also applies changed scheduler jobs.
3. `migrate` does **not** update the custom fields on File and User. After an update, run:
   `bench --site $SITE execute svan_paperless_bridge.custom_fields.create_svan_paperless_custom_fields`
4. Restart the bench (`bench restart`). Check the **Sync Log** for errors afterwards.

After a Frappe or Paperless NGX upgrade: run `migrate` as usual and make sure the Paperless NGX webhook workflow and the API token still exist.

**Before uninstalling the app:** restore the files that were removed from the site, otherwise their content is only in Paperless NGX:
`bench --site $SITE execute svan_paperless_bridge.utils.maintenance.restore_physical_files_from_paperless`

## Development

Source strings are English; German lives in `svan_paperless_bridge/locale/de.po`. Code style is enforced by `pre-commit` (`pre-commit install`). Architecture and internals are documented in [AGENTS.md](AGENTS.md).

## License

GPL-3.0
