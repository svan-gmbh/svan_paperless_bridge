# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

"""Linked files follow the document type and reference of their Paperless NGX
document while a reference for the import is configured.

After a change in Paperless NGX the files of a document are moved to the business
document the reference now points at, or detached when it points at none, at
several or the document type belongs to no DocType with a reference source.
Detaching deletes the files in ERPNext and keeps the Paperless NGX document; a
later valid reference imports it again (paperless_import.py). Files of a DocType
the reference has no source on are never touched (see reference_config.py).
"""

import frappe

from svan_paperless_bridge.import_rules import find_business_documents
from svan_paperless_bridge.paperless_delete import keeping_paperless_documents
from svan_paperless_bridge.paperless_sync import OUTCOME_OK, push_metadata_update
from svan_paperless_bridge.utils.sync_log import log_sync_event

UNCHANGED = "unchanged"
MOVED = "moved"
DETACHED = "detached"
SKIPPED = "skipped"

SOURCE = "document_relink.relink_document"


def relink_document(document, rules, settings):
	"""Move or detach the files of one Paperless NGX document according to its
	current document type and reference. Returns one of the outcomes above.
	"""
	files = frappe.get_all(
		"File",
		filters={
			"svan_paperless_document_id": str(document["id"]),
			"svan_paperless_sync_status": ["!=", "deleted"],
		},
		fields=[
			"name",
			"file_name",
			"attached_to_doctype",
			"attached_to_name",
			"svan_paperless_sync_status",
			"svan_paperless_needs_update",
		],
	)
	if not files or not _settled(files):
		return SKIPPED

	current = {(f.attached_to_doctype, f.attached_to_name) for f in files}
	if len(current) != 1:
		# Files synced before the 1:1 rule can still share a document across business documents.
		return SKIPPED
	[(doctype, name)] = current
	if not doctype or not rules.covers(doctype):
		return SKIPPED

	matches = rules.matches(document, require_tag=False)
	found = find_business_documents(matches)
	if len(found) == 1:
		if found[0] == (doctype, name):
			return UNCHANGED
		_move(files, (doctype, name), found[0], document, settings)
		return MOVED

	_detach(files, (doctype, name), _detach_reason(matches, found), document, settings)
	return DETACHED


def _settled(files):
	"""Only fully synced files without a pending metadata update move: a pending
	update of ERPNext wins (e.g. the backfill of a new reference).
	"""
	return all(f.svan_paperless_sync_status == "synced" and not f.svan_paperless_needs_update for f in files)


def _move(files, previous, target, document, settings):
	"""Attach all files of the document to target and push target's metadata, so
	nothing of the previous business document remains in Paperless NGX.
	"""
	for f in files:
		file_doc = frappe.get_doc("File", f.name)
		file_doc.add_comment_in_reference_doc("Attachment Removed", file_doc.file_name)
		file_doc.db_set(
			{"attached_to_doctype": target[0], "attached_to_name": target[1]}, update_modified=False
		)
		file_doc.create_attachment_record()
		log_sync_event(
			f.name,
			settings,
			"Info",
			SOURCE,
			title="File moved to another business document",
			details=(
				f"Paperless NGX document ID: {document['id']}\n"
				f"From: {previous[0]} {previous[1]}\nTo: {target[0]} {target[1]}"
			),
		)

	if push_metadata_update(files[0].name, settings, previous_doctype=previous[0]) != OUTCOME_OK:
		# The regular push retries; custom fields only the previous DocType had a source for may remain.
		frappe.db.set_value(
			"File",
			{"name": ["in", [f.name for f in files]]},
			"svan_paperless_needs_update",
			1,
			update_modified=False,
		)
	frappe.db.commit()


def _detach(files, previous, reason, document, settings):
	"""Delete the files in ERPNext; the Paperless NGX document stays as it is."""
	for f in files:
		log_sync_event(
			f.name,
			settings,
			"Info",
			SOURCE,
			title="File detached from its business document",
			details=(
				f"Paperless NGX document ID: {document['id']}\nBusiness document: {previous[0]} {previous[1]}\n"
				f"Reason: {reason}\nThe file was deleted in ERPNext; the document stays in Paperless NGX."
			),
		)
	with keeping_paperless_documents():
		for f in files:
			frappe.delete_doc("File", f.name, ignore_permissions=True)
	frappe.db.commit()


def _detach_reason(matches, found):
	if not matches:
		return "The document type belongs to no DocType with a reference source, or the reference is empty."
	references = ", ".join(f"{rule.doctype}.{rule.source_fieldname} = {value}" for rule, value in matches)
	if not found:
		return f"No business document found ({references})."
	return f"Several business documents found ({references}): " + ", ".join(
		f"{doctype} {name}" for doctype, name in found
	)
