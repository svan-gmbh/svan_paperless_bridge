# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe

# Entries written once the open transaction is rolled back (see log_sync_event_after_rollback()).
AFTER_ROLLBACK_FLAG = "svan_paperless_logs_after_rollback"


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

	Every entry also stores a snapshot of the file, its business document and the
	mapping state (see log_context.collect_log_context()) for later analysis of
	exported logs.
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
				"context": _collect_context(file_name, settings),
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(
			title=f"SVAN Paperless Sync Log: logging failed ({source})",
			message=frappe.get_traceback(),
		)


def log_sync_event_after_rollback(file_name, settings, log_type, source, title, details=None):
	"""Write the entry once the open transaction is rolled back, e.g. before a throw
	(which rolls back an entry logged now) or from an after_rollback callback.
	Committing before the throw instead would also commit what the transaction did
	so far. Nothing is written if it is committed.
	"""
	pending = frappe.flags.get(AFTER_ROLLBACK_FLAG)
	if pending is None:
		pending = frappe.flags[AFTER_ROLLBACK_FLAG] = []
		frappe.db.after_rollback.add(lambda: _write_after_rollback(settings))
		frappe.db.after_commit.add(lambda: frappe.flags.pop(AFTER_ROLLBACK_FLAG, None))
	pending.append((file_name, log_type, source, title, details))


def _write_after_rollback(settings):
	if len(frappe.db.after_rollback):
		# The commit would discard the callbacks still queued, e.g. a File restoring its content.
		frappe.db.after_rollback.add(lambda: _write_after_rollback(settings))
		return
	for file_name, log_type, source, title, details in frappe.flags.pop(AFTER_ROLLBACK_FLAG, None) or []:
		if file_name and not frappe.db.exists("File", file_name):
			# Created in the rolled back transaction.
			file_name = None
		log_sync_event(file_name, settings, log_type, source, title, details)
	frappe.db.commit()


def _collect_context(file_name, settings):
	"""A failing snapshot must not cost the log entry itself."""
	# Lazy: log_context imports correspondent_sync, which imports this module.
	from svan_paperless_bridge.utils.log_context import collect_log_context

	try:
		return frappe.as_json(collect_log_context(file_name, settings), ensure_ascii=False)
	except Exception:
		return frappe.as_json({"collect_error": frappe.get_traceback()}, ensure_ascii=False)


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
