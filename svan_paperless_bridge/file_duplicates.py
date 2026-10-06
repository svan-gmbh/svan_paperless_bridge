# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe

# Hard upper bound of returned rows - the form shows 10 and a "Show All" button.
MAX_DUPLICATES = 100


@frappe.whitelist()
def get_duplicate_files(file_name):
	"""Other File records with the same content hash as file_name, for the
	"Duplicates" section of the File form (see public/js/file_duplicates.js).

	Compares Frappe's own content_hash. Files without one (e.g. attached by URL)
	have no duplicates. Only files the user may read are returned.
	"""
	frappe.has_permission("File", "read", file_name, throw=True)

	content_hash = frappe.db.get_value("File", file_name, "content_hash")
	if not content_hash:
		return []

	return frappe.get_list(
		"File",
		filters={"content_hash": content_hash, "name": ["!=", file_name], "is_folder": 0},
		fields=["name", "file_name", "is_private", "attached_to_doctype", "attached_to_name"],
		order_by="creation desc",
		limit_page_length=MAX_DUPLICATES,
	)
