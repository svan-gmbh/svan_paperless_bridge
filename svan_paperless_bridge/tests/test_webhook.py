# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

from unittest.mock import patch

import frappe

from svan_paperless_bridge.tests.paperless_test_case import (
	INVOICE_DOCTYPE,
	WEBHOOK_SECRET,
	PaperlessTestCase,
)
from svan_paperless_bridge.webhook_sync import receive_update


class TestWebhook(PaperlessTestCase):
	"""Changes are made in Paperless NGX, then the webhook call is simulated.

	Paperless NGX's own "Document Updated" workflow may additionally notify the
	site it is set up for; that site finds no File for these documents.
	"""

	def setUp(self):
		super().setUp()
		self.party = self.make_party("ACME Ltd")
		self.invoice = self.make_invoice(party=self.party.name, reference_no="RE-1", due_date="2026-03-17")
		self.invoice.add_tag(self.name("Urgent"))
		self.file_doc = self.synced_file(self.invoice)
		self.document_id = int(self.file_doc.svan_paperless_document_id)

	def test_missing_secret_is_rejected(self):
		with patch("frappe.get_request_header", return_value=None):
			self.assertRaises(frappe.PermissionError, receive_update, document_id=self.document_id)

	def test_wrong_secret_is_rejected(self):
		with patch("frappe.get_request_header", return_value="guessed"):
			self.assertRaises(frappe.PermissionError, receive_update, document_id=self.document_id)

	def test_tag_added_in_paperless_is_added_to_business_document(self):
		paid_id = self.paperless.create("tags", self.name("Paid"))
		self.paperless.patch_document(self.document_id, tags=[*self._document()["tags"], paid_id])

		self._receive()

		self.assertIn(self.name("Paid"), self._tags())
		self.assertTrue(frappe.db.exists("SVAN Paperless Tag Mapping", {"frappe_tag": self.name("Paid")}))

	def test_tag_removed_in_paperless_is_removed(self):
		self.paperless.patch_document(self.document_id, tags=[])

		self._receive()

		self.assertNotIn(self.name("Urgent"), self._tags())

	def test_forward_only_tag_is_kept(self):
		frappe.db.set_value(
			"SVAN Paperless Tag Mapping", {"frappe_tag": self.name("Urgent")}, "bidirectional", 0
		)
		self.paperless.patch_document(self.document_id, tags=[])

		self._receive()

		self.assertIn(self.name("Urgent"), self._tags())

	def test_custom_field_value_is_applied_without_ping_pong(self):
		field_id = self._map("reference_no", "Reference", "string")
		self._set_paperless_value(field_id, "RE-from-paperless")

		self._receive()

		self.assertEqual(self.invoice.reload().reference_no, "RE-from-paperless")
		self.assertEqual(self.file_doc.reload().svan_paperless_needs_update, 0)

	def test_removed_custom_field_value_clears_field(self):
		field_id = self._map("reference_no", "Reference", "string")
		self._set_paperless_value(field_id, "RE-1")
		self._receive()
		self.paperless.patch_document(self.document_id, custom_fields=[])

		self._receive()

		self.assertFalse(self.invoice.reload().reference_no)

	def test_unchanged_date_does_not_rewrite_document(self):
		field_id = self._map("due_date", "Due", "date")
		self._set_paperless_value(field_id, "2026-03-17")
		modified = self.invoice.reload().modified

		self._receive()

		self.assertEqual(self.invoice.reload().modified, modified)

	def test_correspondent_of_other_party_relinks(self):
		other = self.make_party("Globex")
		correspondent_id = self.paperless.create("correspondents", self.name("Globex"))
		self.paperless.patch_document(self.document_id, correspondent=correspondent_id)

		self._receive()

		self.assertEqual(self.invoice.reload().party, other.name)

	def test_removed_correspondent_clears_party(self):
		self.paperless.patch_document(self.document_id, correspondent=None)

		self._receive()

		self.assertFalse(self.invoice.reload().party)

	def test_ambiguous_correspondent_leaves_party(self):
		self.make_party("Twin")
		self.make_party("Twin")
		correspondent_id = self.paperless.create("correspondents", self.name("Twin"))
		self.paperless.patch_document(self.document_id, correspondent=correspondent_id)

		self._receive()

		self.assertEqual(self.invoice.reload().party, self.party.name)

	def _receive(self):
		with patch("frappe.get_request_header", return_value=WEBHOOK_SECRET):
			receive_update(document_id=self.document_id)

	def _document(self):
		return self.paperless.document(self.document_id)

	def _tags(self):
		return frappe.get_all(
			"Tag Link",
			filters={"document_type": INVOICE_DOCTYPE, "document_name": self.invoice.name},
			pluck="tag",
		)

	def _map(self, fieldname, label, data_type):
		field_id = self.paperless.create("custom_fields", self.name(label), data_type=data_type)
		frappe.get_doc(
			{
				"doctype": "SVAN Paperless Custom Field Mapping",
				"paperless_custom_field_id": field_id,
				"paperless_custom_field_name": self.name(label),
				"paperless_custom_field_type": data_type,
				"field_sources": [{"source_doctype": INVOICE_DOCTYPE, "source_fieldname": fieldname}],
			}
		).insert()
		return field_id

	def _set_paperless_value(self, field_id, value):
		self.paperless.patch_document(self.document_id, custom_fields=[{"field": field_id, "value": value}])
