# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

from unittest.mock import patch

import frappe

from svan_paperless_bridge.paperless_sync import reconcile_pending_files
from svan_paperless_bridge.tests.paperless_test_case import (
	INVOICE_DOCTYPE,
	PARTY_DOCTYPE,
	PaperlessTestCase,
)


class TestSettings(PaperlessTestCase):
	def test_default_correspondent_is_created_and_resolved(self):
		correspondent = self.paperless.find("correspondents", self.name("Default Correspondent"))

		self.assertEqual(self.settings.svan_default_correspondent_id, str(correspondent["id"]))

	def test_doctype_may_be_whitelisted_only_once(self):
		self.settings.append("whitelisted_doctypes", {"document_type": INVOICE_DOCTYPE})

		self.assertRaises(frappe.ValidationError, self.settings.save)

	def test_party_fieldname_must_be_a_link(self):
		self.settings.whitelisted_doctypes[0].party_fieldname = "reference_no"

		self.assertRaises(frappe.ValidationError, self.settings.save)

	def test_dynamic_link_party_is_accepted_without_rename_support(self):
		self.settings.whitelisted_doctypes[0].party_fieldname = "party_name"
		self.settings.save()

		self.assertEqual(self.settings.whitelisted_doctypes[0].party_rename_supported, 0)

	def test_link_party_with_plain_title_field_supports_rename(self):
		self.assertEqual(self.settings.whitelisted_doctypes[0].party_rename_supported, 1)

	def test_whitelist_change_bumps_config_version(self):
		version = self.settings.sync_config_version
		self.settings.append("whitelisted_doctypes", {"document_type": PARTY_DOCTYPE})
		self.settings.save()

		self.assertEqual(self.settings.sync_config_version, version + 1)

	def test_restricted_service_token_is_warned_about(self):
		self.settings.api_token = self.paperless.create_user()[1]
		frappe.clear_messages()

		# The connection test fails too (no right to read correspondents) and logs that.
		with patch("frappe.log_error"):
			self.settings.save()

		self.assertTrue(any("superuser" in m.get("message", "") for m in frappe.local.message_log))

	def test_file_cannot_be_custom_field_source(self):
		mapping = self._custom_field_mapping("File", "file_name")

		self.assertRaises(frappe.ValidationError, mapping.insert)

	def test_link_source_forces_forward_only_mapping(self):
		mapping = self._custom_field_mapping(INVOICE_DOCTYPE, "party").insert()

		self.assertEqual(mapping.bidirectional, 0)

	def _custom_field_mapping(self, source_doctype, source_fieldname):
		return frappe.get_doc(
			{
				"doctype": "SVAN Paperless Custom Field Mapping",
				"paperless_custom_field_name": self.name("Mapped"),
				"paperless_custom_field_type": "string",
				"bidirectional": 1,
				"field_sources": [{"source_doctype": source_doctype, "source_fieldname": source_fieldname}],
			}
		)


class TestClassification(PaperlessTestCase):
	def test_file_of_unlisted_doctype_is_not_relevant(self):
		file_doc = self.attach_file(self.make_party("Unlisted"))

		reconcile_pending_files()

		self.assertEqual(file_doc.reload().svan_paperless_sync_status, "")

	def test_whitelisting_a_doctype_reclassifies_existing_files(self):
		file_doc = self.attach_file(self.make_party("Listed later"))
		reconcile_pending_files()

		self.settings.append("whitelisted_doctypes", {"document_type": PARTY_DOCTYPE})
		self.settings.save()
		reconcile_pending_files()

		self.assertEqual(file_doc.reload().svan_paperless_sync_status, "pending")

	def test_file_type_outside_whitelist_is_not_relevant(self):
		file_doc = self.attach_file(self.make_invoice(), file_name="data.csv", content=b"a,b\n1,2\n")

		reconcile_pending_files()

		self.assertEqual(file_doc.reload().svan_paperless_sync_status, "")

	def test_empty_file_type_whitelist_allows_nothing(self):
		self.settings.set("whitelisted_file_types", [])
		self.settings.save()

		file_doc = self.attach_file(self.make_invoice())
		reconcile_pending_files()

		self.assertEqual(file_doc.reload().svan_paperless_sync_status, "")
