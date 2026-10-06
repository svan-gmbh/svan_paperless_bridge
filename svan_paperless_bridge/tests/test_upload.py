# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe

from svan_paperless_bridge.paperless_sync import poll_syncing_files
from svan_paperless_bridge.preview import is_preview_url
from svan_paperless_bridge.tests.paperless_test_case import (
	INVOICE_DOCTYPE,
	PaperlessTestCase,
	custom_field_values,
	make_pdf,
)


class TestUpload(PaperlessTestCase):
	def test_attachment_is_uploaded_with_business_metadata(self):
		party = self.make_party("ACME Ltd")
		invoice = self.make_invoice(party=party.name, reference_no="RE-1001")
		invoice.add_tag(self.name("Urgent"))
		self._map_reference_no()

		file_doc = self.synced_file(invoice)

		document = self.document_of(file_doc)
		self.assertEqual(document["title"], file_doc.file_name)
		self.assertEqual(self.entity_name("correspondents", document["correspondent"]), self.name("ACME Ltd"))
		self.assertEqual(self.entity_name("document_types", document["document_type"]), self.name("Invoice"))
		self.assertIn(self.name("Urgent"), [self.entity_name("tags", t) for t in document["tags"]])
		field_id = self.paperless.find("custom_fields", self.name("Reference"))["id"]
		self.assertIn({"field": field_id, "value": "RE-1001"}, custom_field_values(document))

	def test_file_is_uploaded_byte_for_byte(self):
		# A small PDF is also readable as Windows-1250 text; it must not arrive re-encoded.
		content = make_pdf()

		file_doc = self.synced_file(self.make_invoice(), content=content)

		self.assertEqual(self.paperless.original(file_doc.svan_paperless_document_id), content)

	def test_empty_party_field_sends_no_correspondent(self):
		file_doc = self.synced_file(self.make_invoice())

		self.assertIsNone(self.document_of(file_doc)["correspondent"])

	def test_doctype_without_party_field_uses_default_correspondent(self):
		self.settings.whitelisted_doctypes[0].party_fieldname = ""
		self.settings.save()

		file_doc = self.synced_file(self.make_invoice())

		correspondent = self.document_of(file_doc)["correspondent"]
		self.assertEqual(
			self.entity_name("correspondents", correspondent), self.name("Default Correspondent")
		)

	def test_dynamic_link_party_resolves_correspondent(self):
		self.settings.whitelisted_doctypes[0].party_fieldname = "party_name"
		self.settings.save()
		party = self.make_party("Dynamic Party")

		file_doc = self.synced_file(self.make_invoice(party_type=party.doctype, party_name=party.name))

		correspondent = self.document_of(file_doc)["correspondent"]
		self.assertEqual(self.entity_name("correspondents", correspondent), self.name("Dynamic Party"))

	def test_identical_content_in_two_files_gives_two_documents(self):
		content = make_pdf()
		first = self.synced_file(self.make_invoice(), content=content)
		second = self.synced_file(self.make_invoice(), content=content)

		self.assertNotEqual(first.svan_paperless_document_id, second.svan_paperless_document_id)

	def test_orphaned_document_with_same_checksum_is_adopted(self):
		content = make_pdf()
		orphan_id = self.paperless.upload(content, self.name("orphan"))
		party = self.make_party("Adopting Party")

		file_doc = self.synced_file(self.make_invoice(party=party.name), content=content)

		self.assertEqual(int(file_doc.svan_paperless_document_id), orphan_id)
		correspondent = self.paperless.document(orphan_id)["correspondent"]
		self.assertEqual(self.entity_name("correspondents", correspondent), self.name("Adopting Party"))

	def test_unreachable_paperless_keeps_file_pending(self):
		with self.paperless_unreachable():
			file_doc = self.attach_file(self.make_invoice())
			self.assertEqual(self.run_sync(file_doc), "pending")

		self.assertEqual(self.run_sync(file_doc), "synced")

	def test_unsupported_file_type_breaks_file_and_leaves_whitelist(self):
		with self.paperless_unreachable():
			# Paperless NGX detects the type from the content: application/octet-stream.
			file_doc = self.attach_file(self.make_invoice(), file_name="notes.txt", content=b"\x00\x01\x02")

		self.assertEqual(self.run_sync(file_doc), "broken")
		whitelist = [
			row.file_type for row in frappe.get_single("SVAN Paperless Settings").whitelisted_file_types
		]
		self.assertNotIn("TXT", whitelist)

	def test_document_owned_by_other_file_is_not_taken_over(self):
		first = self.synced_file(self.make_invoice())
		with self.paperless_unreachable():
			second = self.attach_file(self.make_invoice())
		# As if the second upload's task had returned the first file's document.
		second.db_set(
			{"svan_paperless_sync_status": "syncing", "svan_paperless_uuid": first.svan_paperless_uuid},
			update_modified=False,
		)

		poll_syncing_files(enforce_limits=False)

		second.reload()
		self.assertEqual(second.svan_paperless_sync_status, "pending")
		self.assertFalse(second.svan_paperless_uuid)

	def test_stale_tag_id_is_repaired_and_upload_retried(self):
		invoice = self.make_invoice()
		invoice.add_tag(self.name("Archive"))
		self.synced_file(invoice)
		self.paperless.delete("tags", self.paperless.find("tags", self.name("Archive"))["id"])

		file_doc = self.synced_file(invoice)

		tag_names = [self.entity_name("tags", t) for t in self.document_of(file_doc)["tags"]]
		self.assertIn(self.name("Archive"), tag_names)

	def test_offloading_replaces_file_url_with_preview_route(self):
		self.settings.delete_physical_file = 1
		self.settings.save()

		file_doc = self.synced_file(self.make_invoice())

		self.assertTrue(is_preview_url(file_doc.file_url))

	def _map_reference_no(self):
		frappe.get_doc(
			{
				"doctype": "SVAN Paperless Custom Field Mapping",
				"paperless_custom_field_name": self.name("Reference"),
				"paperless_custom_field_type": "string",
				"field_sources": [{"source_doctype": INVOICE_DOCTYPE, "source_fieldname": "reference_no"}],
			}
		).insert()
