# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
from frappe.tests import UnitTestCase

from svan_paperless_bridge.document_access import set_my_paperless_api_token
from svan_paperless_bridge.file_versions import normalized_name
from svan_paperless_bridge.paperless_delete import (
	PaperlessOriginalHasVersionsError,
	delete_keeping_paperless_original,
	handle_file_on_trash,
)
from svan_paperless_bridge.paperless_sync import sync_metadata_updates, sync_pending_files
from svan_paperless_bridge.preview import stream
from svan_paperless_bridge.tests.paperless_test_case import PaperlessTestCase, make_pdf


class TestNormalizedName(UnitTestCase):
	def test_frappe_suffix_is_removed(self):
		self.assertEqual(normalized_name("invoiced3f6ee.pdf"), "invoice.pdf")

	def test_name_without_suffix_is_kept(self):
		self.assertEqual(normalized_name("invoice.pdf"), "invoice.pdf")

	def test_name_of_only_six_hex_characters_is_kept(self):
		self.assertEqual(normalized_name("abcdef.pdf"), "abcdef.pdf")

	def test_upper_case_hex_is_no_suffix(self):
		self.assertEqual(normalized_name("invoiceD3F6EE.pdf"), "invoiceD3F6EE.pdf")


class TestVersions(PaperlessTestCase):
	def setUp(self):
		super().setUp()
		self.invoice = self.make_invoice()
		self.original_content = make_pdf()
		self.original = self.synced_file(self.invoice, content=self.original_content)
		self.document_id = self.original.svan_paperless_document_id

	def test_same_name_becomes_new_version(self):
		version = self.synced_file(self.invoice)

		self.assertEqual(version.svan_paperless_document_id, self.document_id)
		self.assertNotEqual(version.svan_paperless_version_id, self.document_id)
		self.assertEqual(self.original.reload().svan_paperless_version_id, self.document_id)
		labels = {str(v["id"]): v["version_label"] for v in self._versions()}
		self.assertEqual(labels[version.svan_paperless_version_id], version.file_name)

	def test_other_name_becomes_document_of_its_own(self):
		other = self.synced_file(self.invoice, file_name="other.pdf")

		self.assertNotEqual(other.svan_paperless_document_id, self.document_id)

	def test_same_name_on_other_business_document_is_no_version(self):
		other = self.synced_file(self.make_invoice())

		self.assertNotEqual(other.svan_paperless_document_id, self.document_id)

	def test_preview_shows_own_version(self):
		version_content = make_pdf()
		version = self.synced_file(self.invoice, content=version_content)

		self.assertEqual(self._preview(self.original), self.original_content)
		self.assertEqual(self._preview(version), version_content)

	def test_version_waits_for_its_original(self):
		invoice = self.make_invoice()
		with self.paperless_unreachable():
			original = self.attach_file(invoice)
			version = self.attach_file(invoice)

		sync_pending_files(enforce_limits=False)

		self.assertTrue(original.reload().svan_paperless_uuid)
		self.assertFalse(version.reload().svan_paperless_uuid)
		self.assertEqual(self.wait_for_sync(original), "synced")
		self.assertEqual(self.run_sync(version), "synced")
		self.assertEqual(
			version.reload().svan_paperless_document_id, original.reload().svan_paperless_document_id
		)

	def test_gone_original_document_means_document_of_its_own(self):
		self.paperless.delete_document(self.document_id)

		version = self.synced_file(self.invoice)

		self.assertNotEqual(version.svan_paperless_document_id, self.document_id)

	def test_metadata_is_pushed_once_per_document(self):
		version = self.synced_file(self.invoice)
		frappe.db.set_value(
			"File", {"name": ["in", [self.original.name, version.name]]}, "svan_paperless_needs_update", 1
		)

		sync_metadata_updates(enforce_limits=False)

		pushes = frappe.get_all(
			"SVAN Paperless Sync Log",
			filters={
				"title": "Metadata update successful",
				"reference_file": ["in", [self.original.name, version.name]],
			},
		)
		self.assertEqual(len(pushes), 1)
		self.assertEqual(version.reload().svan_paperless_needs_update, 0)
		self.assertEqual(self.original.reload().svan_paperless_needs_update, 0)

	def test_deleting_version_deletes_only_that_version(self):
		version = self.synced_file(self.invoice)

		frappe.delete_doc("File", version.name)

		self.assertEqual([str(v["id"]) for v in self._versions()], [self.document_id])

	def test_deleting_original_with_versions_asks_first(self):
		self.synced_file(self.invoice)

		self.assertRaises(PaperlessOriginalHasVersionsError, frappe.delete_doc, "File", self.original.name)

		self.assertTrue(frappe.db.exists("File", self.original.name))
		self.assertEqual(self.original.reload().svan_paperless_sync_status, "synced")

	def test_confirmed_original_deletion_keeps_the_document(self):
		version = self.synced_file(self.invoice)

		delete_keeping_paperless_original(self.original.name)

		self.assertFalse(frappe.db.exists("File", self.original.name))
		self.assertEqual(len(self._versions()), 2)
		frappe.delete_doc("File", version.name)
		self.assertIsNone(self.paperless.document(self.document_id))

	def test_new_file_after_original_deletion_joins_the_remaining_version(self):
		self.synced_file(self.invoice)
		delete_keeping_paperless_original(self.original.name)

		third = self.synced_file(self.invoice)

		self.assertEqual(third.svan_paperless_document_id, self.document_id)
		self.assertEqual(len(self._versions()), 3)

	def _versions(self):
		return self.paperless.document(self.document_id)["versions"]

	def _preview(self, file_doc):
		stream(file_doc.name)
		return frappe.response["filecontent"]


class TestVersionDeletePermissions(PaperlessTestCase):
	def setUp(self):
		super().setUp()
		invoice = self.make_invoice()
		self.original = self.synced_file(invoice)
		self.version = self.synced_file(invoice)
		self.document_id = self.original.svan_paperless_document_id
		self.settings.enforce_document_permissions = 1
		self.settings.save()
		self.paperless_user_id, paperless_token = self.paperless.create_user()
		frappe.set_user(self.make_user())
		set_my_paperless_api_token(paperless_token)
		self.paperless.patch_document(
			self.document_id,
			set_permissions={
				"view": {"users": [self.paperless_user_id], "groups": []},
				"change": {"users": [self.paperless_user_id], "groups": []},
			},
		)

	def test_version_deletion_without_permission_is_blocked(self):
		self.assertRaises(frappe.PermissionError, self._trash, self.version)
		self.assertEqual(len(self.paperless.document(self.document_id)["versions"]), 2)

	def test_permission_is_checked_before_the_question(self):
		self.assertRaises(frappe.PermissionError, self._trash, self.original)

	def test_owner_is_asked_about_the_original(self):
		self.paperless.patch_document(self.document_id, owner=self.paperless_user_id)

		self.assertRaises(PaperlessOriginalHasVersionsError, self._trash, self.original)

	def _trash(self, file_doc):
		handle_file_on_trash(file_doc.reload())
