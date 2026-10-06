# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe

from svan_paperless_bridge.paperless_sync import reconcile_metadata_changes
from svan_paperless_bridge.tests.paperless_test_case import INVOICE_DOCTYPE, PaperlessTestCase


class TestReconcile(PaperlessTestCase):
	"""Changes Paperless NGX sends no webhook for."""

	def setUp(self):
		super().setUp()
		self.field_id = self.paperless.create("custom_fields", self.name("Reference"), data_type="string")
		frappe.get_doc(
			{
				"doctype": "SVAN Paperless Custom Field Mapping",
				"paperless_custom_field_id": self.field_id,
				"paperless_custom_field_name": self.name("Reference"),
				"paperless_custom_field_type": "string",
				"field_sources": [{"source_doctype": INVOICE_DOCTYPE, "source_fieldname": "reference_no"}],
			}
		).insert()
		self.party = self.make_party("ACME Ltd")
		self.invoice = self.make_invoice(party=self.party.name, reference_no="RE-1")
		self.invoice.add_tag(self.name("Urgent"))
		self.synced_file(self.invoice)

	def test_renamed_tag_is_renamed_in_frappe(self):
		self._rename("tags", "Urgent", "Very Urgent")

		reconcile_metadata_changes(enforce_limits=False)

		self.assertEqual(self._tags(), [self.name("Very Urgent")])

	def test_deleted_tag_is_removed_from_business_document(self):
		self._delete("tags", "Urgent")

		reconcile_metadata_changes(enforce_limits=False)

		self.assertEqual(self._tags(), [])

	def test_renamed_correspondent_renames_party(self):
		self._rename("correspondents", "ACME Ltd", "ACME Group")

		reconcile_metadata_changes(enforce_limits=False)

		self.assertEqual(self.party.reload().party_title, self.name("ACME Group"))

	def test_deleted_correspondent_clears_party_link(self):
		self._delete("correspondents", "ACME Ltd")

		reconcile_metadata_changes(enforce_limits=False)

		self.assertFalse(self.invoice.reload().party)

	def test_deleted_custom_field_clears_values(self):
		self._delete("custom_fields", "Reference")

		reconcile_metadata_changes(enforce_limits=False)

		self.assertFalse(self.invoice.reload().reference_no)

	def test_renamed_custom_field_updates_mapping(self):
		self._rename("custom_fields", "Reference", "Invoice Number")

		reconcile_metadata_changes(enforce_limits=False)

		mapping_name = frappe.db.get_value(
			"SVAN Paperless Custom Field Mapping",
			{"paperless_custom_field_id": self.field_id},
			"paperless_custom_field_name",
		)
		self.assertEqual(mapping_name, self.name("Invoice Number"))

	def _rename(self, endpoint, label, new_label):
		entity_id = self.paperless.find(endpoint, self.name(label))["id"]
		self.paperless.rename(endpoint, entity_id, self.name(new_label))

	def _delete(self, endpoint, label):
		self.paperless.delete(endpoint, self.paperless.find(endpoint, self.name(label))["id"])

	def _tags(self):
		return frappe.get_all(
			"Tag Link",
			filters={"document_type": INVOICE_DOCTYPE, "document_name": self.invoice.name},
			pluck="tag",
		)
