"""Switch the archive on (SVAN Paperless Settings.archive_deleted_files): a single
DocType does not receive the default of a field added later. From now on deleting a
file in ERPNext no longer deletes anything in Paperless NGX.
"""

import frappe

from svan_paperless_bridge.utils.patch_helpers import tracert

SETTINGS = "SVAN Paperless Settings"


def execute():
	tracert(f"START {__file__}")

	if frappe.db.exists("Singles", {"doctype": SETTINGS, "field": "archive_deleted_files"}):
		tracert("archive_deleted_files already set - nothing to do")
	else:
		frappe.db.set_single_value(SETTINGS, "archive_deleted_files", 1)
		tracert("archive_deleted_files set to 1")

	tracert(f"END {__file__}")
