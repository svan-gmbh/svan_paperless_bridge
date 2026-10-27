# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

"""Versions of a linked Paperless NGX document that no file holds become further
files of the business document, like a version uploaded in ERPNext
(file_versions.py), whatever their age.

A version whose label carries the archive mark (archive.py) is skipped: a file
deleted in ERPNext leaves its version marked, and it comes back once the mark is
removed. The webhook imports them at once, the comparison every five minutes
(paperless_compare.py) catches the rest, e.g. a version restored from the trash,
which fires nothing in Paperless NGX. A version has no metadata of its own, so the
business document stays as it is.
"""

import frappe

from svan_paperless_bridge.archive import is_archived
from svan_paperless_bridge.file_versions import held_version_ids, lock_attachments
from svan_paperless_bridge.paperless_import import create_version_file
from svan_paperless_bridge.utils.sync_log import log_sync_event


def import_new_versions(document, settings, source):
	"""Import the versions of document no file holds, oldest first. Returns the
	number of imported versions.
	"""
	document_id = str(document["id"])
	try:
		business_document = _business_document(document_id, settings)
		if not business_document:
			return 0
		attachments = frappe.get_all(
			"File",
			filters={"attached_to_doctype": business_document[0], "attached_to_name": business_document[1]},
			fields=["svan_paperless_document_id", "svan_paperless_version_id", "svan_paperless_sync_status"],
		)
		if not _unheld_versions(document, held_version_ids(attachments, document_id)):
			return 0

		attachments = lock_attachments(*business_document)
		if any(
			f.svan_paperless_sync_status == "syncing" and not f.svan_paperless_version_id for f in attachments
		):
			# An upload of this business document may be a version of it, which must not come back as a
			# version from Paperless NGX. Taken on the next change or comparison.
			frappe.db.commit()
			return 0
		versions = _unheld_versions(document, held_version_ids(attachments, document_id))
	except Exception:
		frappe.db.rollback()
		_log_failure(document_id, settings, source)
		return 0

	imported = 0
	for version in versions:
		frappe.db.savepoint("svan_paperless_version_import")
		try:
			file_doc = create_version_file(document, str(version["id"]), *business_document, settings)
		except Exception:
			frappe.db.rollback(save_point="svan_paperless_version_import")
			_log_failure(document_id, settings, source, version["id"])
			# Importing a later version first would skip this one for good.
			break
		log_sync_event(
			file_doc.name,
			settings,
			"Info",
			source,
			title="Version imported from Paperless NGX",
			details=(
				f"Paperless NGX document ID: {document_id}\nVersion ID: {version['id']}\n"
				f"Business document: {business_document[0]} {business_document[1]}"
			),
		)
		imported += 1
	frappe.db.commit()
	return imported


def _business_document(document_id, settings):
	"""(doctype, name) all files of the document are attached to, if whitelisted."""
	attached_to = {
		(f.attached_to_doctype, f.attached_to_name)
		for f in frappe.get_all(
			"File",
			filters={
				"svan_paperless_document_id": document_id,
				"svan_paperless_sync_status": ["!=", "deleted"],
			},
			fields=["attached_to_doctype", "attached_to_name"],
		)
	}
	if len(attached_to) != 1:
		# Files synced before the 1:1 rule can still share a document across business documents.
		return None
	[(doctype, name)] = attached_to
	whitelisted = {row.document_type for row in settings.whitelisted_doctypes}
	if doctype not in whitelisted or not frappe.db.exists(doctype, name):
		return None
	return doctype, name


def _unheld_versions(document, held):
	"""Archived versions never come back on their own, whether the archive is on or not."""
	if not held:
		return []
	return sorted(
		(
			version
			for version in document.get("versions") or []
			if str(version["id"]) not in held and not is_archived(version.get("version_label"))
		),
		key=lambda version: int(version["id"]),
	)


def _log_failure(document_id, settings, source, version_id=None):
	version = f"\nVersion ID: {version_id}" if version_id else ""
	log_sync_event(
		None,
		settings,
		"Error",
		source,
		title="Importing a new version failed - will be retried on the next comparison",
		details=f"Paperless NGX document ID: {document_id}{version}\n\n{frappe.get_traceback()}",
	)
