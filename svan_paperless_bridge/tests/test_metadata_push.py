# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe

from svan_paperless_bridge.paperless_sync import sync_metadata_updates
from svan_paperless_bridge.tests.paperless_test_case import (
	INVOICE_DOCTYPE,
	PaperlessTestCase,
	custom_field_values,
)


class TestMetadataPush(PaperlessTestCase):
	def setUp(self):
		super().setUp()
		frappe.get_doc(
			{
				"doctype": "SVAN Paperless Custom Field Mapping",
				"paperless_custom_field_name": self.name("Reference"),
				"paperless_custom_field_type": "string",
				"field_sources": [{"source_doctype": INVOICE_DOCTYPE, "source_fieldname": "reference_no"}],
			}
		).insert()
		self.party = self.make_party("ACME Ltd")
		self.invoice = self.make_invoice(party=self.party.name, reference_no="RE-1")

	def test_changed_source_field_is_pushed(self):
		file_doc = self.synced_file(self.invoice)

		self._set_reference("RE-2")
		self.assertEqual(file_doc.reload().svan_paperless_needs_update, 1)
		sync_metadata_updates(enforce_limits=False)

		self.assertEqual(file_doc.reload().svan_paperless_needs_update, 0)
		self.assertIn(self._reference("RE-2"), custom_field_values(self.document_of(file_doc)))

	def test_unwatched_field_change_does_not_flag(self):
		file_doc = self.synced_file(self.invoice)

		self.invoice.reload().update({"due_date": "2026-12-31"}).save()

		self.assertEqual(file_doc.reload().svan_paperless_needs_update, 0)

	def test_foreign_custom_field_values_survive_the_push(self):
		file_doc = self.synced_file(self.invoice)
		foreign_id = self.paperless.create("custom_fields", self.name("Set in Paperless"), data_type="string")
		document = self.document_of(file_doc)
		self.paperless.patch_document(
			document["id"],
			custom_fields=[*custom_field_values(document), {"field": foreign_id, "value": "keep me"}],
		)

		self._set_reference("RE-3")
		sync_metadata_updates(enforce_limits=False)

		values = custom_field_values(self.document_of(file_doc))
		self.assertIn({"field": foreign_id, "value": "keep me"}, values)
		self.assertIn(self._reference("RE-3"), values)

	def test_cleared_source_field_clears_custom_field(self):
		file_doc = self.synced_file(self.invoice)
		field_id = self._reference(None)["field"]

		self._set_reference("")
		sync_metadata_updates(enforce_limits=False)

		fields = [entry["field"] for entry in self.document_of(file_doc)["custom_fields"]]
		self.assertNotIn(field_id, fields)

	def test_cleared_party_clears_correspondent(self):
		file_doc = self.synced_file(self.invoice)

		self.invoice.reload().update({"party": ""}).save()
		sync_metadata_updates(enforce_limits=False)

		self.assertIsNone(self.document_of(file_doc)["correspondent"])

	def test_added_tag_is_pushed(self):
		file_doc = self.synced_file(self.invoice)

		self.invoice.add_tag(self.name("Reviewed"))
		sync_metadata_updates(enforce_limits=False)

		tag_names = [self.entity_name("tags", t) for t in self.document_of(file_doc)["tags"]]
		self.assertIn(self.name("Reviewed"), tag_names)

	def test_change_while_syncing_is_pushed_after_finalize(self):
		file_doc = self.attach_file(self.invoice)
		self.assertEqual(file_doc.reload().svan_paperless_sync_status, "syncing")

		self._set_reference("RE-late")
		self.assertEqual(self.wait_for_sync(file_doc), "synced")
		sync_metadata_updates(enforce_limits=False)

		self.assertIn(self._reference("RE-late"), custom_field_values(self.document_of(file_doc)))

	def test_renamed_party_renames_correspondent(self):
		file_doc = self.synced_file(self.invoice)

		self.party.reload().update({"party_title": self.name("ACME Group")}).save()

		correspondent = self.document_of(file_doc)["correspondent"]
		self.assertEqual(self.entity_name("correspondents", correspondent), self.name("ACME Group"))

	def test_document_deleted_in_paperless_breaks_file(self):
		file_doc = self.synced_file(self.invoice)
		self.paperless.delete_document(file_doc.svan_paperless_document_id)

		self._set_reference("RE-gone")
		sync_metadata_updates(enforce_limits=False)

		self.assertEqual(file_doc.reload().svan_paperless_sync_status, "broken")

	def _set_reference(self, value):
		self.invoice.reload().update({"reference_no": value}).save()

	def _reference(self, value):
		field_id = self.paperless.find("custom_fields", self.name("Reference"))["id"]
		return {"field": field_id, "value": value}
