# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import json

import frappe

from svan_paperless_bridge.svan_paperless_ngx_bridge.doctype.svan_paperless_sync_log.svan_paperless_sync_log import (
	SVANPaperlessSyncLog,
	export_sync_logs,
)
from svan_paperless_bridge.tests.paperless_test_case import INVOICE_DOCTYPE, PaperlessTestCase
from svan_paperless_bridge.utils.sync_log import log_sync_event

SYNC_LOG = "SVAN Paperless Sync Log"


class TestSyncLog(PaperlessTestCase):
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
		self.tag = self.name("urgent")
		self.invoice.add_tag(self.tag)
		# Stays pending: the snapshot needs no Paperless NGX.
		with self.paperless_unreachable():
			self.file_doc = self.attach_file(self.invoice)

	def test_context_snapshots_file_and_business_document(self):
		context = self._context(self._log(self.file_doc.name))

		self.assertEqual(context["settings"]["sync_config_version"], self.settings.sync_config_version)
		self.assertEqual(context["file"]["content_hash"], self.file_doc.content_hash)
		self.assertNotIn("owner", context["file"])
		self.assertNotIn("content", context["file"])

		business_document = context["business_document"]
		self.assertEqual(business_document["name"], self.invoice.name)
		self.assertEqual(business_document["whitelist_row"]["party_fieldname"], "party")
		self.assertEqual(
			business_document["tags"], [{"tag": self.tag, "paperless_tag_id": None, "bidirectional": None}]
		)
		self.assertEqual(business_document["party"]["display_name"], self.party.party_title)
		[custom_field] = business_document["custom_fields"]
		self.assertEqual(custom_field["source_fieldname"], "reference_no")
		self.assertEqual(custom_field["value"], "RE-1")

	def test_context_without_file_only_has_settings(self):
		self.assertEqual(list(self._context(self._log(None))), ["settings"])

	def test_context_of_deleted_business_document(self):
		frappe.db.delete(INVOICE_DOCTYPE, {"name": self.invoice.name})

		business_document = self._context(self._log(self.file_doc.name))["business_document"]

		self.assertFalse(business_document["exists"])

	def test_export_of_selected_entries(self):
		selected = self._log(self.file_doc.name)
		self._log(None)

		[entry] = self._export(names=[selected])

		self.assertEqual(entry["name"], selected)
		self.assertEqual(entry["context"]["file"]["name"], self.file_doc.name)
		self.assertNotIn("owner", entry)

	def test_export_is_utf8(self):
		party_title = self.name("Müller & Söhne")
		self.party.update({"party_title": party_title}).save()

		[entry] = self._export(names=[self._log(self.file_doc.name)])

		self.assertIn(party_title.encode("utf-8"), frappe.response.filecontent)
		self.assertEqual(entry["context"]["business_document"]["party"]["display_name"], party_title)
		self.assertIn("charset=utf-8", frappe.response.content_type)

	def test_export_without_selection_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._export(names=[])

	def test_clear_old_logs(self):
		old, recent = self._log(None), self._log(None)
		frappe.db.set_value(SYNC_LOG, old, "creation", frappe.utils.add_days(None, -91))

		SVANPaperlessSyncLog.clear_old_logs(days=90)

		self.assertFalse(frappe.db.exists(SYNC_LOG, old))
		self.assertTrue(frappe.db.exists(SYNC_LOG, recent))

	def _log(self, file_name, title=None):
		title = title or self.name("test entry")
		log_sync_event(file_name, self.settings, "Info", "test", title=title)
		return frappe.get_all(SYNC_LOG, filters={"title": title}, order_by="creation desc", pluck="name")[0]

	def _context(self, log_name):
		return json.loads(frappe.db.get_value(SYNC_LOG, log_name, "context"))

	def _export(self, **kwargs):
		export_sync_logs(**{key: json.dumps(value) for key, value in kwargs.items()})
		return json.loads(frappe.response.filecontent.decode("utf-8"))
