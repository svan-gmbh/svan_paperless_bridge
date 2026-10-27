# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import io
from unittest.mock import patch

import frappe
from frappe.core.doctype.file.utils import get_content_hash
from frappe.custom.doctype.property_setter.property_setter import make_property_setter
from PIL import Image, ImageDraw

from svan_paperless_bridge.paperless_import import import_from_paperless, reset_import_queue
from svan_paperless_bridge.paperless_sync import sync_metadata_updates
from svan_paperless_bridge.preview import stream
from svan_paperless_bridge.tests.paperless_test_case import (
	INVOICE_DOCTYPE,
	PARTY_DOCTYPE,
	custom_field_values,
	make_pdf,
)
from svan_paperless_bridge.tests.reference_test_case import IMPORT_DOCTYPE, MAPPING, ReferenceTestCase


class TestImport(ReferenceTestCase):
	def setUp(self):
		super().setUp()
		self.invoice = self.make_invoice(reference_no="RE-1")

	def test_document_with_reference_is_imported(self):
		content = make_pdf()
		document_id = self.paperless_document(self.invoice.name, content=content)

		import_from_paperless(enforce_limits=False)

		file_doc = self.imported_file(document_id)
		self.assertEqual(file_doc.attached_to_name, self.invoice.name)
		self.assertEqual(file_doc.svan_paperless_sync_status, "synced")
		self.assertEqual(file_doc.svan_source, "paperless")
		self.assertEqual(file_doc.svan_paperless_version_id, str(document_id))
		self.assertEqual(file_doc.get_content(encodings=()), content)
		self.assertEqual(self.queue_status(document_id), "imported")

	def test_import_sets_the_party_as_correspondent(self):
		party = self.make_party("ACME Ltd")
		invoice = self.make_invoice(party=party.name)
		document_id = self.paperless_document(invoice.name)

		import_from_paperless(enforce_limits=False)

		self.assertIsNotNone(self.imported_file(document_id))
		correspondent_id = self.paperless.document(document_id)["correspondent"]
		self.assertEqual(self.entity_name("correspondents", correspondent_id), party.party_title)

	def test_import_without_party_field_sets_the_default_correspondent(self):
		self.settings.whitelisted_doctypes[0].party_fieldname = None
		self.settings.save()
		document_id = self.paperless_document(self.invoice.name)

		import_from_paperless(enforce_limits=False)

		self.assertIsNotNone(self.imported_file(document_id))
		correspondent_id = self.paperless.document(document_id)["correspondent"]
		self.assertEqual(
			self.entity_name("correspondents", correspondent_id), self.name("Default Correspondent")
		)

	def test_offloaded_import_keeps_its_content_hash(self):
		self.settings.delete_physical_file = 1
		self.settings.save()
		content = make_pdf()
		document_id = self.paperless_document(self.invoice.name, content=content)

		import_from_paperless(enforce_limits=False)

		file_doc = self.imported_file(document_id)
		self.assertTrue(file_doc.file_url.startswith("/api/method/"))
		self.assertEqual(file_doc.content_hash, get_content_hash(content))

	def test_newest_version_is_imported_with_its_own_name(self):
		document_id = self.paperless.upload(make_pdf(), self.name("scan"))
		png = _png()
		version_id = self.paperless.add_version(document_id, self.name("second.png"), png)
		self.set_paperless_reference(document_id, self.invoice.name)

		import_from_paperless(enforce_limits=False)

		file_doc = self.imported_file(document_id)
		self.assertEqual(file_doc.svan_paperless_version_id, str(version_id))
		self.assertEqual(file_doc.file_name, self.name("second.png"))
		self.assertEqual(file_doc.get_content(encodings=()), png)
		stream(file_doc.name)
		self.assertEqual(frappe.response["filecontent"], png)

	def test_exported_file_carries_the_reference(self):
		file_doc = self.synced_file(self.invoice)

		values = custom_field_values(self.document_of(file_doc))

		self.assertIn({"field": self.field_id(), "value": self.invoice.name}, values)

	def test_document_type_without_reference_is_ignored(self):
		document_id = self.paperless_document(self.invoice.name)
		self.set_reference(None)

		import_from_paperless(enforce_limits=False)

		self.assertFalse(frappe.db.exists(IMPORT_DOCTYPE, str(document_id)))

	def test_reference_ignores_case_and_surrounding_whitespace(self):
		document_id = self.paperless_document(f" \n{self.invoice.name.upper()}\t\n ")

		import_from_paperless(enforce_limits=False)

		self.assertEqual(self.imported_file(document_id).attached_to_name, self.invoice.name)

	def test_blank_reference_is_ignored(self):
		self.reference_mapping({INVOICE_DOCTYPE: "reference_no"}, label="Reference No")
		self.make_invoice(reference_no="")
		document_id = self.paperless_document(" \n ")

		import_from_paperless(enforce_limits=False)

		self.assertFalse(frappe.db.exists(IMPORT_DOCTYPE, str(document_id)))
		self.assertIsNone(self.imported_file(document_id))

	def test_import_tag_is_required_and_not_kept(self):
		import_tag, other_tag = self.name("ERPNext Relevant"), self.name("Contract")
		import_tag_id = self.tag_id(import_tag)
		self.set_reference(self.mapping, import_tag=import_tag)
		without_tag = self.paperless_document(self.invoice.name)
		with_tags = self.paperless_document(self.invoice.name, tags=[import_tag_id, self.tag_id(other_tag)])

		import_from_paperless(enforce_limits=False)
		sync_metadata_updates(enforce_limits=False)

		self.assertFalse(frappe.db.exists(IMPORT_DOCTYPE, str(without_tag)))
		self.assertIsNotNone(self.imported_file(with_tags))
		self.assertEqual(self.tags_of(self.invoice), [other_tag])
		self.assertNotIn(import_tag_id, self.paperless.document(with_tags)["tags"])

	def test_removed_import_tag_imports_documents_scanned_before(self):
		import_tag = self.name("ERPNext Relevant")
		self.tag_id(import_tag)
		self.set_reference(self.mapping, import_tag=import_tag)
		document_id = self.paperless_document(self.invoice.name)
		import_from_paperless(enforce_limits=False)
		self.assertIsNone(self.imported_file(document_id))

		# Like a form opened after the scan: saving the loaded watermark is no restart.
		self.settings.reload()
		self.set_reference(self.mapping)
		import_from_paperless(enforce_limits=False)

		self.assertIsNotNone(self.imported_file(document_id))

	def test_unknown_reference_stops_after_the_attempts_and_restarts_on_change(self):
		self.settings.max_import_attempts = 2
		self.settings.save()
		document_id = self.paperless_document("no such invoice")

		import_from_paperless(enforce_limits=False)
		self.assertEqual(self.queue_status(document_id), "pending")
		import_from_paperless(enforce_limits=False)
		self.assertEqual(self.queue_status(document_id), "not_found")

		self.paperless.patch_document(document_id, title=self.name("changed"))
		import_from_paperless(enforce_limits=False)
		self.assertEqual(frappe.db.get_value(IMPORT_DOCTYPE, str(document_id), "attempts"), 1)

	def test_ambiguous_reference_stops_until_reset(self):
		self.reference_mapping({INVOICE_DOCTYPE: "reference_no"}, label="Reference No")
		self.make_invoice(reference_no="RE-1")
		document_id = self.paperless_document("RE-1")

		import_from_paperless(enforce_limits=False)

		self.assertEqual(self.queue_status(document_id), "ambiguous")
		self.assertEqual(reset_import_queue(), 1)
		self.assertEqual(self.queue_status(document_id), "pending")

	def test_webhook_imports_unlinked_document(self):
		document_id = self.paperless_document(self.invoice.name)

		self.webhook(document_id)

		self.assertIsNotNone(self.imported_file(document_id))

	def test_webhook_logs_nothing_for_a_document_without_file(self):
		document_id = self.paperless.upload(make_pdf(), self.name("unrelated"))

		self.webhook(document_id)

		self.assertFalse(
			frappe.db.exists(
				"SVAN Paperless Sync Log", {"details": ["like", f"%document ID: {document_id}%"]}
			)
		)

	def test_reference_is_never_written_back(self):
		mapping = self.reference_mapping({INVOICE_DOCTYPE: "reference_no"}, bidirectional=1, label="No")
		self.assertEqual(frappe.db.get_value(MAPPING, mapping, "bidirectional"), 0)
		frappe.get_doc(MAPPING, mapping).update({"bidirectional": 1}).save()
		self.assertEqual(frappe.db.get_value(MAPPING, mapping, "bidirectional"), 0)
		file_doc = self.synced_file(self.invoice)
		document_id = int(file_doc.svan_paperless_document_id)
		# Another invoice with that number, so the file is moved rather than detached.
		self.make_invoice(reference_no="RE-99")
		self.paperless.patch_document(
			document_id, custom_fields=[{"field": self.field_id(), "value": "RE-99"}]
		)

		self.webhook(document_id)

		self.assertEqual(self.invoice.reload().reference_no, "RE-1")

	def test_version_added_in_paperless_keeps_a_pending_update(self):
		"""The push right after the import failed, so Paperless NGX still lacks the
		correspondent and has the import tag when a version is added there. The party
		is mandatory like Sales Order.customer.
		"""
		make_property_setter(INVOICE_DOCTYPE, "party", "reqd", 1, "Check")
		self.addCleanup(frappe.clear_cache, doctype=INVOICE_DOCTYPE)
		import_tag = self.name("ERPNext Relevant")
		import_tag_id = self.tag_id(import_tag)
		self.set_reference(self.mapping, import_tag=import_tag)
		party = self.make_party("ACME Ltd")
		# Known in Paperless NGX from documents uploaded earlier.
		frappe.get_doc(
			{
				"doctype": "SVAN Paperless Correspondent Mapping",
				"frappe_party_type": PARTY_DOCTYPE,
				"frappe_party": party.name,
				"paperless_correspondent_id": self.paperless.create("correspondents", party.party_title),
			}
		).insert()
		invoice = self.make_invoice(party=party.name)
		document_id = self.paperless_document(invoice.name, tags=[import_tag_id])
		with patch("svan_paperless_bridge.paperless_import.push_metadata_update", return_value=None):
			import_from_paperless(enforce_limits=False)
		started = frappe.utils.now_datetime()

		self.paperless.add_version(document_id, self.name("second.png"), _png())
		# What Paperless NGX's "Document Updated" workflow sends for the new version.
		self.webhook(document_id)
		import_from_paperless(enforce_limits=False)

		self.assertEqual(self._problems_logged_since(started), [])
		self.assertEqual(invoice.reload().party, invoice.party)
		self.assertNotIn(import_tag, self.tags_of(invoice))
		sync_metadata_updates(enforce_limits=False)
		document = self.paperless.document(document_id)
		self.assertIsNotNone(document["correspondent"])
		self.assertNotIn(import_tag_id, document["tags"])

	def _problems_logged_since(self, started):
		return frappe.get_all(
			"SVAN Paperless Sync Log",
			filters={"log_type": ["in", ["Warn", "Error"]], "log_datetime": [">=", started]},
			pluck="title",
		)


def _png():
	"""A PNG that Paperless NGX can OCR (a tiny one fails); random text keeps the
	checksum unique.
	"""
	image = Image.new("RGB", (400, 200), "white")
	ImageDraw.Draw(image).text((10, 80), f"Version {frappe.generate_hash()}", fill="black")
	buffer = io.BytesIO()
	image.save(buffer, "PNG")
	return buffer.getvalue()
