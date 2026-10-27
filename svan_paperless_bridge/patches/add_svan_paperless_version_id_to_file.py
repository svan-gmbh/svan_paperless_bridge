"""Add File.svan_paperless_version_id and fill it for files already linked to a
Paperless NGX document: they hold its original, whose version ID equals the
document ID. Without it, the poll job would query the task of a file stuck in
"syncing" again, and the preview would show the newest version instead.
"""

import frappe

from svan_paperless_bridge.custom_fields import create_svan_paperless_custom_fields
from svan_paperless_bridge.utils.patch_helpers import tracert


def execute():
	tracert(f"START {__file__}")

	create_svan_paperless_custom_fields()
	tracert("Custom fields created/updated")

	frappe.db.sql(
		"""
		UPDATE `tabFile`
		SET svan_paperless_version_id = svan_paperless_document_id
		WHERE IFNULL(svan_paperless_document_id, '') != ''
		  AND IFNULL(svan_paperless_version_id, '') = ''
		"""
	)
	tracert(f"Version ID set on {frappe.db._cursor.rowcount} file(s)")

	tracert(f"END {__file__}")
