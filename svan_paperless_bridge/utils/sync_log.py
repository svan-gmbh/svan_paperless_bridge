# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe


def log_sync_event(file_name, settings, log_type, source, title, details=None):
	"""Record a sync event in the DocType "SVAN Paperless Sync Log".

	Kept in a module of its own (instead of remaining part of paperless_sync.py)
	so that both paperless_sync.py and tag_sync.py can use this function without
	creating a circular import: paperless_sync.py imports
	get_paperless_tag_ids_for_document() from tag_sync.py (for uploading tags
	along with a file), while tag_sync.py needs log_sync_event() for its own
	logging - a direct import of paperless_sync.py from tag_sync.py would no
	longer be possible.

	An error inside this function is logged the standard Frappe way
	(frappe.log_error, including a traceback) so that a logging failure never
	interrupts the sync operation that called it.

	title is truncated to the field length of "title" (a Data field, see
	svan_paperless_sync_log.json) before saving: some titles contain dynamic
	values (e.g. HTTP status, exception messages). Without this truncation an
	overly long title would make the insert fail with a database error ("Data
	too long for column") and thereby prevent the actual error from being
	logged at all.
	"""
	try:
		title = _truncate_to_field_length("SVAN Paperless Sync Log", "title", title)
		frappe.get_doc(
			{
				"doctype": "SVAN Paperless Sync Log",
				"log_datetime": frappe.utils.now_datetime(),
				"log_type": log_type,
				"source": source,
				"reference_file": file_name,
				"title": title,
				"details": details,
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(
			title=f"SVAN Paperless Sync Log: logging failed ({source})",
			message=frappe.get_traceback(),
		)


def _truncate_to_field_length(doctype, fieldname, value):
	"""Truncate value to the field length stored in the DocType (if set).

	Data fields without an explicit "length" use Frappe's default of 140
	characters. If value is already shorter, it is returned unchanged.
	"""
	if not value:
		return value

	max_length = frappe.get_meta(doctype).get_field(fieldname).length or 140
	if len(value) <= max_length:
		return value

	return value[: max_length - 1] + "…"
