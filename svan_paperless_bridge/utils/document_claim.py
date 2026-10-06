# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe


def is_document_id_held_by_other_file(document_id, file_name=None):
	"""True if a File other than file_name already references the Paperless NGX
	document.

	Every File owns its own Paperless NGX document (1:1) - tags, correspondent and
	custom field values of that document come from exactly one business document.
	Checked wherever a document ID is about to be assigned to a File.
	"""
	filters = {"svan_paperless_document_id": document_id}
	if file_name:
		filters["name"] = ["!=", file_name]
	return bool(frappe.db.exists("File", filters))
