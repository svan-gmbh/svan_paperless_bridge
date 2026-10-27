# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder import Interval
from frappe.query_builder.functions import Now

DOCTYPE = "SVAN Paperless Sync Log"
# owner/modified_by are left out: user IDs are personal data.
EXPORT_FIELDS = (
	"name",
	"creation",
	"log_datetime",
	"log_type",
	"source",
	"reference_file",
	"title",
	"details",
	"context",
)


class SVANPaperlessSyncLog(Document):
	@staticmethod
	def clear_old_logs(days=90):
		"""Called by Frappe's Log Settings (see default_log_clearing_doctypes in hooks.py)."""
		table = frappe.qb.DocType(DOCTYPE)
		frappe.db.delete(table, filters=(table.creation < (Now() - Interval(days=days))))


@frappe.whitelist()
def export_sync_logs(names):
	"""Download the given entries as a JSON array."""
	names = frappe.parse_json(names)
	if not names:
		frappe.throw(_("Select the entries to export."))

	logs = frappe.get_list(
		DOCTYPE,
		filters={"name": ["in", names]},
		fields=list(EXPORT_FIELDS),
		order_by="log_datetime asc, creation asc",
		limit_page_length=0,
	)
	for log in logs:
		log.context = frappe.parse_json(log.context) if log.context else None

	timestamp = frappe.utils.now_datetime().strftime("%Y%m%d-%H%M%S")
	frappe.response.filename = f"svan-paperless-sync-log-{timestamp}.json"
	# Real UTF-8 instead of \u escapes; no BOM, which JSON forbids (RFC 8259).
	frappe.response.filecontent = frappe.as_json(logs, ensure_ascii=False).encode("utf-8")
	frappe.response.content_type = "application/json; charset=utf-8"
	frappe.response.type = "download"
