# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

from urllib.parse import parse_qs, urlparse

import frappe
import requests

from svan_paperless_bridge.utils.maintenance import restore_physical_file_from_paperless

RETRY = "retry"


def prepare_amend_copy_for_upload(file_doc, settings, log):
	"""Turn a File copied by an amend into a freshly created one.

	Frappe's copy_attachments_from_amended_from() copies every attachment as a NEW
	File but takes file_url over unchanged - if the original was already offloaded
	to Paperless NGX, the copy points at this app's preview route and has no local
	content. The URL carries the original's File name (query parameter "file_name",
	see preview.get_preview_url()).

	The original's current content is downloaded from Paperless NGX and stored
	locally, then the sync fields are reset: the status is "pending" like for a file
	the user just created, so the next scheduled run uploads the copy as a document of
	its own. The original keeps its document.

	log(log_type, title, details) writes the sync log entry (see query_uuid()).

	Returns RETRY on a transient failure (the caller queues the file again).
	Otherwise None: the copy was reset, or marked "broken" because the original has
	no synced document (any more) - a retry cannot change that.
	"""
	original_name = parse_qs(urlparse(file_doc.file_url).query).get("file_name", [None])[0]
	original = (
		frappe.db.get_value(
			"File",
			original_name,
			["svan_paperless_document_id", "svan_paperless_sync_status"],
			as_dict=True,
		)
		if original_name
		else None
	)

	if not (
		original and original.svan_paperless_document_id and original.svan_paperless_sync_status == "synced"
	):
		_mark_broken(file_doc, f"its original '{original_name}' has no synced document", log)
		return None

	try:
		restore_physical_file_from_paperless(
			file_doc, settings, document_id=original.svan_paperless_document_id
		)
	except requests.HTTPError as error:
		if error.response is not None and error.response.status_code == 404:
			_mark_broken(file_doc, f"the document of its original '{original_name}' no longer exists", log)
			return None
		return _log_restore_failure(log)
	except Exception:
		return _log_restore_failure(log)

	file_doc.db_set(
		{
			"svan_paperless_document_id": "",
			"svan_paperless_uuid": "",
			"svan_paperless_sync_status": "pending",
			# 0 is below any real sync_config_version, so the next classification run
			# re-checks the whitelists before the upload (see reconcile_file_classification()).
			"svan_paperless_config_version_checked": 0,
			"svan_paperless_needs_update": 0,
		},
		update_modified=False,
	)
	log(
		"Info",
		"Copied attachment (amend) reset for a fresh upload",
		f"File '{file_doc.file_name}' (attached to {file_doc.attached_to_doctype} "
		f"{file_doc.attached_to_name}) was copied from '{original_name}'. Its content was "
		f"restored from Paperless NGX document {original.svan_paperless_document_id} and all "
		"sync fields were reset (status 'pending'), so it is uploaded as a document of its own on the next run.",
	)
	return None


def _log_restore_failure(log):
	log(
		"Error",
		"Restoring the original content failed - queued for the next scheduled run",
		frappe.get_traceback(),
	)
	return RETRY


def _mark_broken(file_doc, reason, log):
	file_doc.db_set("svan_paperless_sync_status", "broken", update_modified=False)
	log(
		"Error",
		"Copied attachment (amend) cannot be resolved - marked as 'broken'",
		f"File '{file_doc.file_name}' (attached to {file_doc.attached_to_doctype} "
		f"{file_doc.attached_to_name}) points at this app's preview route "
		f"(file_url='{file_doc.file_url}'), but {reason} in Paperless NGX. "
		"The attachment has to be uploaded again manually on the amended document.",
	)
