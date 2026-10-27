# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe

from svan_paperless_bridge.document_type_sync import get_paperless_document_type_id_for_document
from svan_paperless_bridge.paperless_import import import_from_paperless
from svan_paperless_bridge.paperless_sync import sync_metadata_updates
from svan_paperless_bridge.tests.paperless_test_case import (
	INVOICE_DOCTYPE,
	PARTY_DOCTYPE,
	custom_field_values,
)
from svan_paperless_bridge.tests.reference_test_case import MAPPING, ReferenceTestCase


class TestRelink(ReferenceTestCase):
	def setUp(self):
		super().setUp()
		self.invoice = self.make_invoice()
		self.other = self.make_invoice()

	def test_new_reference_moves_the_file_with_the_metadata_of_its_new_business_document(self):
		self.invoice.update({"party": self.make_party("Old Party").name}).save()
		self.invoice.add_tag(self.name("Old Tag"))
		self.other.update({"party": self.make_party("New Party").name}).save()
		self.other.add_tag(self.name("New Tag"))
		file_doc = self.synced_file(self.invoice)
		document_id = file_doc.svan_paperless_document_id

		self.set_paperless_reference(document_id, self.other.name)
		self.webhook(document_id)

		self.assertEqual(file_doc.reload().attached_to_name, self.other.name)
		document = self.paperless.document(document_id)
		self.assertEqual(
			self.entity_name("correspondents", document["correspondent"]), self.name("New Party")
		)
		self.assertEqual([self.entity_name("tags", t) for t in document["tags"]], [self.name("New Tag")])
		self.assertIn({"field": self.field_id(), "value": self.other.name}, custom_field_values(document))
		self.assertEqual(self.tags_of(self.other), [self.name("New Tag")])
		self.assertEqual(self.tags_of(self.invoice), [self.name("Old Tag")])
		self.assertTrue(self._comment(self.invoice, "Attachment Removed"))
		self.assertTrue(self._comment(self.other, "Attachment"))
		self.assertTrue(self._logged(file_doc, "File moved to another business document"))

	def test_move_to_another_doctype_leaves_no_values_of_the_previous_one(self):
		self.whitelist_party()
		self.add_source(PARTY_DOCTYPE, "name")
		reference_no = self._custom_field_mapping({INVOICE_DOCTYPE: "reference_no"}, "Reference No")
		self.invoice.update({"reference_no": "RE-7"}).save()
		party = self.make_party("Target Party")
		file_doc = self.synced_file(self.invoice)
		document_id = file_doc.svan_paperless_document_id
		reference_no_id = int(frappe.db.get_value(MAPPING, reference_no, "paperless_custom_field_id"))
		self.assertIn(
			{"field": reference_no_id, "value": "RE-7"},
			custom_field_values(self.paperless.document(document_id)),
		)

		# Paperless NGX replaces the whole list; the reference number stays like a user would leave it.
		self.paperless.patch_document(
			document_id,
			document_type=self._document_type_id(PARTY_DOCTYPE),
			custom_fields=[
				{"field": self.field_id(), "value": party.name},
				{"field": reference_no_id, "value": "RE-7"},
			],
		)
		self.webhook(document_id)

		file_doc.reload()
		self.assertEqual(
			(file_doc.attached_to_doctype, file_doc.attached_to_name), (PARTY_DOCTYPE, party.name)
		)
		document = self.paperless.document(document_id)
		fields = [entry["field"] for entry in document["custom_fields"]]
		self.assertNotIn(reference_no_id, fields)
		self.assertIn({"field": self.field_id(), "value": party.name}, custom_field_values(document))
		self.assertEqual(
			self.entity_name("correspondents", document["correspondent"]), self.name("Default Correspondent")
		)

	def test_unknown_reference_detaches_and_keeps_the_document(self):
		file_doc = self.synced_file(self.invoice)
		document_id = file_doc.svan_paperless_document_id

		self.set_paperless_reference(document_id, "no such invoice")
		self.webhook(document_id)

		self.assertFalse(frappe.db.exists("File", file_doc.name))
		self.assertIsNotNone(self.paperless.document(document_id))
		self.assertTrue(self._comment(self.invoice, "Attachment Removed"))
		self.assertTrue(self._logged(file_doc, "File detached from its business document"))

	def test_empty_reference_detaches(self):
		file_doc = self.synced_file(self.invoice)

		self.set_paperless_reference(file_doc.svan_paperless_document_id, "")
		self.webhook(file_doc.svan_paperless_document_id)

		self.assertFalse(frappe.db.exists("File", file_doc.name))

	def test_removed_reference_field_detaches(self):
		file_doc = self.synced_file(self.invoice)

		self.paperless.patch_document(file_doc.svan_paperless_document_id, custom_fields=[])
		self.webhook(file_doc.svan_paperless_document_id)

		self.assertFalse(frappe.db.exists("File", file_doc.name))

	def test_ambiguous_reference_detaches(self):
		self.reference_mapping({INVOICE_DOCTYPE: "reference_no"}, label="Reference No")
		self.invoice.update({"reference_no": "RE-1"}).save()
		file_doc = self.synced_file(self.invoice)

		self.other.update({"reference_no": "RE-1"}).save()
		self.paperless.patch_document(file_doc.svan_paperless_document_id, title=self.name("changed"))
		self.webhook(file_doc.svan_paperless_document_id)

		self.assertFalse(frappe.db.exists("File", file_doc.name))

	def test_document_type_of_a_doctype_without_source_detaches(self):
		file_doc = self.synced_file(self.invoice)
		self.whitelist_party()

		self.set_paperless_reference(file_doc.svan_paperless_document_id, self.invoice.name, PARTY_DOCTYPE)
		self.webhook(file_doc.svan_paperless_document_id)

		self.assertFalse(frappe.db.exists("File", file_doc.name))

	def test_removed_document_type_detaches(self):
		file_doc = self.synced_file(self.invoice)

		self.paperless.patch_document(file_doc.svan_paperless_document_id, document_type=None)
		self.webhook(file_doc.svan_paperless_document_id)

		self.assertFalse(frappe.db.exists("File", file_doc.name))

	def test_unchanged_reference_keeps_the_file_and_syncs_back(self):
		file_doc = self.synced_file(self.invoice)
		tag = self.name("From Paperless")
		tag_id = self.tag_id(tag)

		self.paperless.patch_document(file_doc.svan_paperless_document_id, tags=[tag_id])
		self.webhook(file_doc.svan_paperless_document_id)

		self.assertEqual(file_doc.reload().attached_to_name, self.invoice.name)
		self.assertIn(tag, self.tags_of(self.invoice))

	def test_move_needs_no_import_tag(self):
		import_tag = self.name("ERPNext Relevant")
		self.tag_id(import_tag)
		self.set_reference(self.mapping, import_tag=import_tag)
		file_doc = self.synced_file(self.invoice)

		self.set_paperless_reference(file_doc.svan_paperless_document_id, self.other.name)
		self.webhook(file_doc.svan_paperless_document_id)

		self.assertEqual(file_doc.reload().attached_to_name, self.other.name)

	def test_detached_document_is_linked_again_only_with_the_import_tag(self):
		import_tag = self.name("ERPNext Relevant")
		import_tag_id = self.tag_id(import_tag)
		self.set_reference(self.mapping, import_tag=import_tag)
		document_id = self.paperless_document(self.invoice.name, tags=[import_tag_id])
		import_from_paperless(enforce_limits=False)
		self.assertIsNotNone(self.imported_file(document_id))

		self.set_paperless_reference(document_id, "no such invoice")
		self.webhook(document_id)
		self.assertIsNone(self.imported_file(document_id))
		self.assertIsNone(self.queue_status(document_id))

		self.set_paperless_reference(document_id, self.other.name, tags=[])
		self.webhook(document_id)
		self.assertIsNone(self.imported_file(document_id))

		self.set_paperless_reference(document_id, self.other.name, tags=[import_tag_id])
		self.webhook(document_id)
		self.assertEqual(self.imported_file(document_id).attached_to_name, self.other.name)

	def test_files_of_a_doctype_without_source_are_left_alone(self):
		self.set_reference(None)
		self.whitelist_party()
		party = self.make_party("Party With File")
		file_doc = self.synced_file(party)
		self.set_reference(self.mapping)

		self.set_paperless_reference(file_doc.svan_paperless_document_id, self.invoice.name)
		self.webhook(file_doc.svan_paperless_document_id)
		self.set_paperless_reference(file_doc.svan_paperless_document_id, "")
		self.webhook(file_doc.svan_paperless_document_id)

		self.assertEqual(file_doc.reload().attached_to_name, party.name)

	def test_pending_metadata_update_wins(self):
		file_doc = self.synced_file(self.invoice)
		document_id = file_doc.svan_paperless_document_id
		frappe.db.set_value("File", file_doc.name, "svan_paperless_needs_update", 1)

		self.set_paperless_reference(document_id, "no such invoice")
		self.webhook(document_id)
		self.assertEqual(file_doc.reload().attached_to_name, self.invoice.name)

		sync_metadata_updates(enforce_limits=False)
		self.assertIn(
			{"field": self.field_id(), "value": self.invoice.name},
			custom_field_values(self.paperless.document(document_id)),
		)

	def test_all_versions_move_together(self):
		original = self.synced_file(self.invoice)
		version = self.synced_file(self.invoice)
		self.assertEqual(version.svan_paperless_document_id, original.svan_paperless_document_id)

		self.set_paperless_reference(original.svan_paperless_document_id, self.other.name)
		self.webhook(original.svan_paperless_document_id)

		self.assertEqual(original.reload().attached_to_name, self.other.name)
		self.assertEqual(version.reload().attached_to_name, self.other.name)

	def test_all_versions_are_detached_together(self):
		original = self.synced_file(self.invoice)
		version = self.synced_file(self.invoice)
		document_id = original.svan_paperless_document_id

		self.set_paperless_reference(document_id, "no such invoice")
		self.webhook(document_id)

		self.assertFalse(frappe.db.exists("File", original.name))
		self.assertFalse(frappe.db.exists("File", version.name))
		self.assertEqual(len(self.paperless.document(document_id)["versions"]), 2)

	def test_import_run_moves_and_detaches_too(self):
		moved = self.synced_file(self.invoice, file_name="moved.pdf")
		detached = self.synced_file(self.invoice, file_name="detached.pdf")
		self.set_paperless_reference(moved.svan_paperless_document_id, self.other.name)
		self.set_paperless_reference(detached.svan_paperless_document_id, "no such invoice")

		import_from_paperless(enforce_limits=False)

		self.assertEqual(moved.reload().attached_to_name, self.other.name)
		self.assertFalse(frappe.db.exists("File", detached.name))

	def test_detaching_an_offloaded_file_keeps_its_content_in_paperless(self):
		self.settings.delete_physical_file = 1
		self.settings.save()
		file_doc = self.synced_file(self.invoice)
		document_id = file_doc.svan_paperless_document_id
		content = self.paperless.original(document_id)

		self.set_paperless_reference(document_id, "no such invoice")
		self.webhook(document_id)

		self.assertFalse(frappe.db.exists("File", file_doc.name))
		self.assertEqual(self.paperless.original(document_id), content)

	def test_renamed_business_document_pushes_its_new_reference(self):
		file_doc = self.synced_file(self.invoice)
		new_name = frappe.generate_hash(length=10)

		frappe.rename_doc(INVOICE_DOCTYPE, self.invoice.name, new_name, force=True)

		file_doc.reload()
		self.assertEqual(file_doc.attached_to_name, new_name)
		self.assertEqual(file_doc.svan_paperless_needs_update, 1)
		sync_metadata_updates(enforce_limits=False)
		self.assertIn(
			{"field": self.field_id(), "value": new_name},
			custom_field_values(self.paperless.document(file_doc.svan_paperless_document_id)),
		)
		self.webhook(file_doc.svan_paperless_document_id)
		self.assertEqual(file_doc.reload().attached_to_name, new_name)

	def test_without_reference_nothing_is_moved(self):
		file_doc = self.synced_file(self.invoice)
		self.set_reference(None)

		self.paperless.patch_document(file_doc.svan_paperless_document_id, custom_fields=[])
		self.webhook(file_doc.svan_paperless_document_id)

		self.assertEqual(file_doc.reload().attached_to_name, self.invoice.name)

	def _custom_field_mapping(self, sources, label):
		return (
			frappe.get_doc(
				{
					"doctype": MAPPING,
					"paperless_custom_field_name": self.name(label),
					"paperless_custom_field_type": "string",
					"field_sources": [
						{"source_doctype": doctype, "source_fieldname": fieldname}
						for doctype, fieldname in sources.items()
					],
				}
			)
			.insert()
			.name
		)

	def _document_type_id(self, doctype):
		return get_paperless_document_type_id_for_document(None, doctype, self.settings)

	def _comment(self, doc, comment_type):
		return frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": doc.doctype,
				"reference_name": doc.name,
				"comment_type": comment_type,
			},
		)

	def _logged(self, file_doc, title):
		return frappe.db.exists("SVAN Paperless Sync Log", {"reference_file": file_doc.name, "title": title})
