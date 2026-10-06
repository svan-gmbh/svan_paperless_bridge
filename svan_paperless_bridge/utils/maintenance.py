# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
import requests

from svan_paperless_bridge.preview import PREVIEW_METHOD_PATH, is_preview_url


def restore_physical_file_from_paperless(file_doc, settings, document_id=None):
	"""Restore the local original content of ONE file whose physical content has
	already been offloaded to Paperless NGX - the core of
	restore_physical_files_from_paperless() (called there in a loop for every
	affected file), and reused by paperless_delete.py as a safeguard BEFORE
	actually deleting a Paperless NGX document (see the docstring there).

	Downloads the ORIGINAL (not the archived PDF version - see
	/documents/<id>/download/?original=true, the same "original" version that used
	to be requested for Paperless share links) through the Paperless NGX API. The
	content is stored in the local /private/files/ or /files/ folder through
	Frappe's own File.save_file() mechanism - the same logic that applies to a
	regular file upload (including the is_private-dependent target folder and
	content_hash/file_size detection). Afterwards file_url points at a local file
	again.

	file_url is deliberately cleared to "" before the save_file() call and content
	is set directly beforehand (not only as a save_file() parameter):
	File.is_remote_file (frappe/core/doctype/file/file.py) would otherwise still
	treat the preview route that is still in place as a remote file - save_file()
	then aborts as a no-op in its very first line ("if self.is_remote_file:
	return"), before the content parameter passed in is even looked at. With
	file_url cleared, is_remote_file falls back to checking "not self.content" -
	without the content set beforehand, the same no-op path would apply again.

	ignore_existing_file_check=True is required as well: without that flag
	save_file() looks for an existing file with identical content by content_hash -
	and would find this file's OWN, not yet updated database record (whose
	content_hash is from before the deletion and would be identical after the
	download). Since the physical file is missing at that point, save_file() would
	wrongly write only file_url back to the (stale) preview route instead of
	actually writing to disk. overwrite=True preserves the original file name
	without an automatically appended hash suffix (Frappe's normal behaviour for
	upload name collisions, not wanted here).

	Deliberately does NOT touch the sync state (svan_paperless_sync_status,
	svan_paperless_document_id, svan_paperless_uuid,
	svan_paperless_config_version_checked) - that is up to the respective caller.
	Raises on failure (network/HTTP/storage error) instead of logging or committing
	itself - both are the caller's responsibility (see there).

	document_id: the Paperless NGX document to download. Defaults to
	file_doc.svan_paperless_document_id, which the caller has to check beforehand.
	Explicit for a copied attachment (amend) that has no ID of its own yet.
	"""
	document_id = document_id or file_doc.svan_paperless_document_id
	token = settings.get_password("api_token", raise_exception=False)
	url = settings.api_url.rstrip("/") + f"/documents/{document_id}/download/?original=true"

	response = requests.get(
		url,
		headers={"Authorization": f"Token {token}"},
		timeout=settings.api_call_timeout or 30,
		verify=True,
	)
	response.raise_for_status()

	file_doc.file_url = ""
	file_doc.content = response.content
	file_doc.save_file(
		content=response.content,
		overwrite=True,
		ignore_existing_file_check=True,
	)
	file_doc.save(ignore_permissions=True)


def restore_physical_files_from_paperless():
	"""Restore the local original content for ALL files whose physical content has
	already been offloaded to Paperless NGX (for testing/debugging) - undoes
	paperless_sync.finalize_sync(..., delete_physical_file=True). Calls
	restore_physical_file_from_paperless() per file (see there for the actual
	restore logic).

	Identifies the affected files by the file_url prefix of the internal preview
	route (is_preview_url() in preview.py).

	Deliberately does NOT touch the sync state (svan_paperless_sync_status,
	svan_paperless_document_id, svan_paperless_uuid,
	svan_paperless_config_version_checked) - see reset_sync_state() for that. The
	recommended order for a full reset to the state before the Paperless NGX sync
	is: call THIS function FIRST, then reset_sync_state() - its WHERE clause
	deliberately excludes "synced" files whose file_url still points at the preview
	route (see there); after this restore, file_url points at a local file again
	and the file is reset along with the others by the following
	reset_sync_state() run.

	Processes file by file with its own try/except (a failure on one file does not
	abort the run) and commits individually, analogous to
	retroactively_delete_physical_files() in paperless_sync.py. If
	frappe.get_doc("File", ...).save() fails AFTER save_file() has already written
	to disk, an unreferenced physical file is left behind (there is no rollback of
	the content already written) - accepted deliberately for a pure
	testing/debugging tool instead of building a more complex two-phase safeguard
	for it.

	Usage: bench --site <site> execute svan_paperless_bridge.utils.maintenance.restore_physical_files_from_paperless
	"""
	settings = frappe.get_single("SVAN Paperless Settings")

	file_names = frappe.get_all(
		"File",
		filters={
			"svan_paperless_sync_status": "synced",
			"file_url": ["like", f"/api/method/{PREVIEW_METHOD_PATH}%"],
			"is_folder": 0,
		},
		pluck="name",
	)
	print(f"{len(file_names)} file(s) with offloaded content found")

	for file_name in file_names:
		file_doc = frappe.get_doc("File", file_name)

		if not is_preview_url(file_doc.file_url):
			continue

		if not file_doc.svan_paperless_document_id:
			print(f"{file_name}: no svan_paperless_document_id - skipped")
			continue

		try:
			restore_physical_file_from_paperless(file_doc, settings)
		except Exception:
			print(f"{file_name}: restore failed\n{frappe.get_traceback()}")
			frappe.db.rollback()
			continue

		print(f"{file_name}: restored -> {file_doc.file_url}")
		frappe.db.commit()


def reset_sync_state():
	"""Reset the Paperless sync state (for testing/debugging).

	Deletes all entries from "SVAN Paperless Sync Log" as well as all rows of the
	two pure cache mapping DocTypes (tags, correspondents) and resets the sync
	status, Paperless NGX UUID, the lazily resolved Paperless NGX document ID (see
	paperless_sync.poll_syncing_files()) and the configuration stamp on all files
	(except folders). The sync status is deliberately set to "" (empty) rather than
	"pending": only the next scheduled run reclassifies via
	reconcile_file_classification() which files are actually relevant (whitelisted,
	not uploaded to Dropbox/Google Drive) - see paperless_sync.py.

	Of the files with status "synced", only the subset whose physical content was
	actually DELETED when the sync completed is deliberately excluded (see the
	WHERE clause below; see paperless_sync.finalize_sync(): delete_physical_file
	was active at that time, and file_url has pointed at the internal preview route
	since - recognizable by the prefix "/api/method/{PREVIEW_METHOD_PATH}", see
	is_preview_url() in preview.py). Like "broken" in
	reconcile_file_classification(), this is a terminal state: the original local
	file no longer exists, resetting it would wrongly classify it as "pending"
	again and the next sync run would try to upload content that is already present
	in Paperless NGX as a supposedly new file. To reset the sync state for those
	files as well (e.g. to test the complete sync including the upload again), call
	restore_physical_files_from_paperless() first - afterwards file_url points at a
	local file again and that subset is covered by a following reset_sync_state()
	run as usual.

	"synced" files whose physical content was KEPT (delete_physical_file was
	disabled, file_url still points at the local file) are NOT affected by this and
	are reset as usual - there is no risk for them, as their local file is present
	unchanged. file_url is never touched (not even for that subset) - it never held
	the preview route to begin with, only the local file path.

	"SVAN Paperless Custom Field Mapping" (including its child table "SVAN
	Paperless Custom Field Source") is deliberately NOT deleted: unlike
	tag/correspondent mappings, custom field mappings are not a pure cache that can
	be rebuilt at any time but configuration maintained by the user (name, data
	type, source fields) - that must not be lost to a reset. Only the two lazily
	resolved sync fields paperless_custom_field_id and last_synced are reset, so
	that the next relevant file upload looks for or creates the custom field in
	Paperless NGX again (see custom_field_sync.py).

	The whitelist configuration in "SVAN Paperless Settings" (whitelisted_doctypes,
	whitelisted_file_types, party_fieldname, paperless_document_type_name) is
	deliberately left untouched as well - only the state synced from the respective
	Paperless NGX APIs or mapped per document is reset, not the configuration
	maintained or prefilled by the user. Analogous to paperless_custom_field_id,
	the lazily resolved paperless_document_type_id of every whitelist row is reset
	though, so that the next relevant file upload looks for or creates the document
	type in Paperless NGX again (see document_type_sync.py).

	Usage: bench --site <site> execute svan_paperless_bridge.utils.maintenance.reset_sync_state
	"""
	frappe.db.delete("SVAN Paperless Sync Log")
	frappe.db.delete("SVAN Paperless Tag Mapping")
	frappe.db.delete("SVAN Paperless Correspondent Mapping")

	frappe.db.sql(
		"""
		UPDATE `tabSVAN Paperless Custom Field Mapping`
		SET paperless_custom_field_id = 0,
		    last_synced = NULL
		"""
	)

	frappe.db.sql(
		"""
		UPDATE `tabSVAN Paperless Whitelisted DocType`
		SET paperless_document_type_id = 0
		"""
	)

	frappe.db.sql(
		"""
		UPDATE `tabFile`
		SET svan_paperless_sync_status = '',
		    svan_paperless_uuid = '',
		    svan_paperless_document_id = '',
		    svan_paperless_config_version_checked = 0
		WHERE is_folder = 0
		  AND NOT (
		      svan_paperless_sync_status = 'synced'
		      AND file_url LIKE %(preview_url_prefix)s
		  )
		""",
		{"preview_url_prefix": f"/api/method/{PREVIEW_METHOD_PATH}%"},
	)

	frappe.db.commit()


def reset_settings_to_fresh_install():
	"""Reset "SVAN Paperless Settings" to exactly the state right after a fresh app
	installation (for testing/debugging) - WITHOUT actually uninstalling and
	reinstalling the app. Unlike reset_sync_state(), this discards the
	configuration maintained by the user itself (api_url, api_token,
	default_correspondent, whitelisted_doctypes, file type whitelist), not just the
	synced cache state.

	To do so it deletes all persisted data of this single DocType including its
	child tables and the associated file type whitelist state (Singles rows,
	DocType/file type whitelist rows, all "SVAN Paperless File Type" records, the
	encrypted API token in __Auth) and then calls install.after_install() - the
	same function "bench install-app" calls during a real installation. This
	produces exactly the same state as a real reinstallation (including
	sync_config_version, which is determined by the field default AND the
	subsequent whitelist population) without keeping those values duplicated or
	hardcoded here - they would otherwise silently drift apart whenever the DocType
	defaults or populate_default_whitelist() change.

	Does NOT touch the sync log or the tag/correspondent/custom field mappings -
	see reset_sync_state() for those.

	Usage: bench --site <site> execute svan_paperless_bridge.utils.maintenance.reset_settings_to_fresh_install
	Afterwards also run "bench --site <site> clear-cache" so that the Desk UI shows
	the reset state immediately (the deletion happens via raw SQL, bypassing the
	ORM's Redis cache).
	"""
	from frappe.utils.password import remove_encrypted_password

	from svan_paperless_bridge.install import after_install

	remove_encrypted_password("SVAN Paperless Settings", "SVAN Paperless Settings", "api_token")

	frappe.db.delete("Singles", {"doctype": "SVAN Paperless Settings"})
	frappe.db.delete("SVAN Paperless Whitelisted DocType")
	frappe.db.delete("SVAN Paperless Whitelisted File Type")
	frappe.db.delete("SVAN Paperless File Type")

	frappe.clear_document_cache("SVAN Paperless Settings", "SVAN Paperless Settings")

	after_install()

	frappe.db.commit()
