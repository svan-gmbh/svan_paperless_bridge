# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

from unittest.mock import patch

import frappe

from svan_paperless_bridge.document_access import has_paperless_document_access, set_my_paperless_api_token
from svan_paperless_bridge.preview import stream
from svan_paperless_bridge.tests.paperless_test_case import UNREACHABLE_API_URL, PaperlessTestCase, make_pdf


class TestPreview(PaperlessTestCase):
	def setUp(self):
		super().setUp()
		self.settings.delete_physical_file = 1
		self.settings.save()
		self.content = make_pdf()
		self.file_doc = self.synced_file(self.make_invoice(), content=self.content)

	def test_offloaded_file_is_streamed_from_paperless(self):
		stream(self.file_doc.name)

		self.assertEqual(frappe.response["filecontent"], self.content)

	def test_document_gone_in_paperless_breaks_file(self):
		self.paperless.delete_document(self.file_doc.svan_paperless_document_id)

		self.assertRaises(frappe.ValidationError, stream, self.file_doc.name)
		self.assertEqual(self.file_doc.reload().svan_paperless_sync_status, "broken")


class TestDocumentAccess(PaperlessTestCase):
	def setUp(self):
		super().setUp()
		self.file_doc = self.synced_file(self.make_invoice())
		self.document_id = self.file_doc.svan_paperless_document_id
		self.paperless_user_id, self.paperless_token = self.paperless.create_user()
		self.settings.enforce_document_permissions = 1
		frappe.set_user(self.make_user())

	def test_check_is_skipped_when_not_enforced(self):
		self.settings.enforce_document_permissions = 0

		self.assertTrue(has_paperless_document_access(self.file_doc, self.settings))

	def test_user_without_personal_token_has_no_access(self):
		self.assertFalse(has_paperless_document_access(self.file_doc, self.settings))

	def test_paperless_permission_of_user_decides(self):
		set_my_paperless_api_token(self.paperless_token)
		self.assertFalse(has_paperless_document_access(self.file_doc, self.settings))

		self.paperless.patch_document(
			self.document_id,
			set_permissions={"view": {"users": [self.paperless_user_id], "groups": []}},
		)
		self.assertTrue(has_paperless_document_access(self.file_doc, self.settings))

	def test_unreachable_paperless_denies_access(self):
		set_my_paperless_api_token(self.paperless_token)
		self.settings.api_url = UNREACHABLE_API_URL

		with patch("frappe.log_error") as log_error:
			self.assertFalse(has_paperless_document_access(self.file_doc, self.settings))
		log_error.assert_called_once()
