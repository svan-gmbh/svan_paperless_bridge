# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe

from svan_paperless_bridge.document_access import set_my_paperless_api_token
from svan_paperless_bridge.paperless_delete import handle_file_on_trash
from svan_paperless_bridge.tests.paperless_test_case import PaperlessTestCase

SYNC_LOG = "SVAN Paperless Sync Log"


class TestDelete(PaperlessTestCase):
	def test_unsynced_file_is_deleted_without_paperless(self):
		with self.paperless_unreachable():
			file_doc = self.attach_file(self.make_invoice())

			frappe.delete_doc("File", file_doc.name)

		self.assertFalse(frappe.db.exists("File", file_doc.name))

	def test_file_with_running_upload_cannot_be_deleted(self):
		file_doc = self.attach_file(self.make_invoice())
		self.assertEqual(file_doc.reload().svan_paperless_sync_status, "syncing")

		self.assertRaises(frappe.ValidationError, frappe.delete_doc, "File", file_doc.name)
		self.wait_for_sync(file_doc)

	def test_deleting_file_deletes_paperless_document(self):
		file_doc = self.synced_file(self.make_invoice())
		document_id = file_doc.svan_paperless_document_id

		frappe.delete_doc("File", file_doc.name)

		self.assertIsNone(self.paperless.document(document_id))
		self.assertFalse(frappe.db.exists("File", file_doc.name))

	def test_document_already_gone_in_paperless_does_not_block(self):
		file_doc = self.synced_file(self.make_invoice())
		self.paperless.delete_document(file_doc.svan_paperless_document_id)

		frappe.delete_doc("File", file_doc.name)

		self.assertFalse(frappe.db.exists("File", file_doc.name))

	def test_unreachable_paperless_blocks_deletion(self):
		file_doc = self.synced_file(self.make_invoice())

		with self.paperless_unreachable():
			self.assertRaises(frappe.ValidationError, frappe.delete_doc, "File", file_doc.name)

	def test_offloaded_content_already_gone_in_paperless_does_not_block(self):
		self.settings.delete_physical_file = 1
		self.settings.save()
		file_doc = self.synced_file(self.make_invoice())
		self.paperless.delete_document(file_doc.svan_paperless_document_id)

		frappe.delete_doc("File", file_doc.name)

		self.assertFalse(frappe.db.exists("File", file_doc.name))

	def test_log_entries_of_blocked_deletions_survive_the_rollback(self):
		self.settings.delete_physical_file = 1
		self.settings.save()
		invoice = self.make_invoice()
		file_docs = [self.synced_file(invoice, file_name=name) for name in ("a.pdf", "b.pdf")]
		frappe.db.savepoint("before_delete")

		with self.paperless_unreachable():
			for file_doc in file_docs:
				self.assertRaises(frappe.ValidationError, frappe.delete_doc, "File", file_doc.name)
		# What a full rollback does, without losing the test's own data.
		frappe.db.rollback(save_point="before_delete")
		frappe.db.after_rollback.run()

		for file_doc in file_docs:
			self.assertTrue(frappe.db.exists("File", file_doc.name))
			self.assertEqual(
				frappe.db.count(
					SYNC_LOG,
					{"reference_file": file_doc.name, "title": "Restoring the local original content failed"},
				),
				1,
			)

	def test_offloaded_content_is_restored_before_deletion(self):
		self.settings.delete_physical_file = 1
		self.settings.save()
		file_doc = self.synced_file(self.make_invoice())

		handle_file_on_trash(file_doc.reload())

		self.assertFalse(file_doc.reload().file_url.startswith("/api/method/"))
		self.assertEqual(file_doc.svan_paperless_sync_status, "deleted")


class TestDeleteWithDocumentPermissions(PaperlessTestCase):
	def setUp(self):
		super().setUp()
		self.settings.enforce_document_permissions = 1
		self.settings.save()
		self.file_doc = self.synced_file(self.make_invoice())
		self.document_id = self.file_doc.svan_paperless_document_id
		self.paperless_user_id, self.paperless_token = self.paperless.create_user()
		self.user = self.make_user()

	def test_user_without_personal_token_is_blocked(self):
		frappe.set_user(self.user)

		self.assertRaises(frappe.ValidationError, handle_file_on_trash, self.file_doc)
		self.assertIsNotNone(self.paperless.document(self.document_id))

	def test_user_with_view_permission_only_is_blocked(self):
		self.paperless.patch_document(
			self.document_id,
			set_permissions={"view": {"users": [self.paperless_user_id], "groups": []}},
		)
		self._login_with_token()

		self.assertRaises(frappe.PermissionError, handle_file_on_trash, self.file_doc)
		self.assertIsNotNone(self.paperless.document(self.document_id))

	def test_owner_of_document_deletes(self):
		self.paperless.patch_document(self.document_id, owner=self.paperless_user_id)
		self._login_with_token()

		handle_file_on_trash(self.file_doc)

		self.assertIsNone(self.paperless.document(self.document_id))

	def test_system_manager_deletes_with_service_token(self):
		handle_file_on_trash(self.file_doc)

		self.assertIsNone(self.paperless.document(self.document_id))

	def _login_with_token(self):
		frappe.set_user(self.user)
		set_my_paperless_api_token(self.paperless_token)
