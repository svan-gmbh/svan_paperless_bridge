# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe

from svan_paperless_bridge.paperless_compare import compare_with_paperless
from svan_paperless_bridge.tests.paperless_test_case import PaperlessTestCase

SYNC_LOG = "SVAN Paperless Sync Log"


class TestPaperlessCompare(PaperlessTestCase):
	def setUp(self):
		super().setUp()
		self.settings.delete_physical_file = 1
		self.settings.save()
		self.invoice = self.make_invoice()
		self.original = self.synced_file(self.invoice)
		self.document_id = self.original.svan_paperless_document_id

	def test_deleted_document_deletes_its_files(self):
		version = self.synced_file(self.invoice)
		self.paperless.delete_document(self.document_id)

		compare_with_paperless(enforce_limits=False)

		self.assertFalse(frappe.db.exists("File", self.original.name))
		self.assertFalse(frappe.db.exists("File", version.name))
		self.assertTrue(
			frappe.db.exists(
				SYNC_LOG,
				{
					"reference_file": self.original.name,
					"title": "Document deleted in Paperless NGX - file deleted",
				},
			)
		)

	def test_document_in_trash_counts_as_deleted(self):
		self.paperless.trash_document(self.document_id)

		compare_with_paperless(enforce_limits=False)

		self.assertFalse(frappe.db.exists("File", self.original.name))

	def test_file_of_existing_document_is_kept(self):
		compare_with_paperless(enforce_limits=False)

		self.assertTrue(frappe.db.exists("File", self.original.name))

	def test_broken_file_of_deleted_document_is_deleted(self):
		self.original.db_set("svan_paperless_sync_status", "broken")
		self.paperless.delete_document(self.document_id)

		compare_with_paperless(enforce_limits=False)

		self.assertFalse(frappe.db.exists("File", self.original.name))

	def test_deleted_version_deletes_only_its_file(self):
		version = self.synced_file(self.invoice)
		self.paperless.delete_version(self.document_id, version.svan_paperless_version_id)

		compare_with_paperless(enforce_limits=False)

		self.assertFalse(frappe.db.exists("File", version.name))
		self.assertTrue(frappe.db.exists("File", self.original.name))

	def test_webhook_deletes_file_of_deleted_version(self):
		version = self.synced_file(self.invoice)
		self.paperless.delete_version(self.document_id, version.svan_paperless_version_id)

		self.webhook(self.document_id)

		self.assertFalse(frappe.db.exists("File", version.name))
		self.assertTrue(frappe.db.exists("File", self.original.name))

	def test_unreachable_paperless_deletes_nothing(self):
		self.paperless.delete_document(self.document_id)

		with self.paperless_unreachable():
			compare_with_paperless(enforce_limits=False)

		self.assertTrue(frappe.db.exists("File", self.original.name))

	def test_queue_entry_of_deleted_import_is_dropped(self):
		frappe.get_doc(
			{
				"doctype": "SVAN Paperless Import",
				"paperless_document_id": int(self.document_id),
				"status": "imported",
			}
		).insert(ignore_permissions=True)
		self.paperless.delete_document(self.document_id)

		compare_with_paperless(enforce_limits=False)

		self.assertFalse(frappe.db.exists("SVAN Paperless Import", self.document_id))
