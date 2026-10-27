# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe

from svan_paperless_bridge.paperless_delete import handle_file_on_trash
from svan_paperless_bridge.paperless_sync import attempt_immediate_sync
from svan_paperless_bridge.preview import is_preview_url

DEFAULT_SOURCE = "erpnext"


def set_default_source(doc, method=None):
	"""Set File.svan_source to "erpnext" on insert if the field is still empty.
	A caller that has already set svan_source explicitly before the insert
	(e.g. in future Paperless NGX triggering a file record with "paperless")
	must not be overwritten by this.
	"""
	if not doc.svan_source:
		doc.svan_source = DEFAULT_SOURCE


def keep_content_hash(doc, method=None):
	"""Frappe (from 16.52) clears content_hash on every save of a remote file, and
	the preview route of an offloaded file counts as one. The hash still describes
	the content kept in Paperless NGX: duplicates, versions and the check of pending
	uploads rely on it. Runs after File.validate().
	"""
	if doc.content_hash or not is_preview_url(doc.file_url):
		return
	before = doc.get_doc_before_save()
	if before and before.content_hash:
		doc.content_hash = before.content_hash
	elif not doc.is_new():
		doc.content_hash = frappe.db.get_value("File", doc.name, "content_hash")


def flag_name_change(doc, method=None):
	"""A file renamed in ERPNext transfers its name with the next metadata push
	(name_sync.py). A rename taken from Paperless NGX sets the synced name along.
	"""
	if (
		doc.get("svan_paperless_synced_name")
		and doc.file_name != doc.svan_paperless_synced_name
		and doc.get("svan_paperless_sync_status") == "synced"
	):
		doc.svan_paperless_needs_update = 1


def sync_immediately(doc, method=None):
	"""Trigger an optimistic first attempt with a shortened timeout right after
	a file was created (see paperless_sync.attempt_immediate_sync()), instead of
	waiting for the next scheduled run (every 5 minutes). If the attempt fails,
	the file stays "pending" and is picked up by the scheduled job as usual -
	creating the file is never prevented by this, and it is never marked as
	"broken".

	Skipped for a file that already has its document, i.e. one imported from
	Paperless NGX (see paperless_import.py).
	"""
	if doc.get("svan_paperless_document_id"):
		return
	attempt_immediate_sync(doc.name)


def delete_from_paperless(doc, method=None):
	"""Delete the Paperless NGX document linked to doc (if any) BEFORE the File
	record itself is deleted - see paperless_delete.handle_file_on_trash().
	Raises (and thereby prevents the entire deletion in ERPNext) if the deleting
	user is not allowed to do so according to Paperless NGX, or if Paperless NGX
	was unreachable.
	"""
	handle_file_on_trash(doc)
