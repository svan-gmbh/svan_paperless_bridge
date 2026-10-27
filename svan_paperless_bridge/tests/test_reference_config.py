# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe

from svan_paperless_bridge.paperless_sync import sync_metadata_updates, sync_pending_files
from svan_paperless_bridge.reference_config import check_reference_change
from svan_paperless_bridge.tests.paperless_test_case import (
	INVOICE_DOCTYPE,
	PARTY_DOCTYPE,
	custom_field_values,
)
from svan_paperless_bridge.tests.reference_test_case import MAPPING, ReferenceTestCase

GAP_WARNING = "Not transferred - the reference for the import has no source for this DocType"


class TestReferenceGap(ReferenceTestCase):
	def setUp(self):
		super().setUp()
		self.whitelist_party()
		self.party = self.make_party("Gap Party")

	def test_gap_keeps_new_files_pending_and_warns_on_every_run(self):
		file_doc = self.attach_file(self.party)

		sync_pending_files(enforce_limits=False)
		sync_pending_files(enforce_limits=False)

		file_doc.reload()
		self.assertEqual(file_doc.svan_paperless_sync_status, "pending")
		self.assertFalse(file_doc.svan_paperless_uuid)
		# The attempt on insert and both runs.
		self.assertEqual(
			frappe.db.count(
				"SVAN Paperless Sync Log", {"reference_file": file_doc.name, "title": GAP_WARNING}
			),
			3,
		)

	def test_closed_gap_transfers_the_waiting_files(self):
		file_doc = self.attach_file(self.party)

		self.add_source(PARTY_DOCTYPE, "name")

		self.assertEqual(self.run_sync(file_doc), "synced")

	def test_no_gap_without_reference(self):
		self.set_reference(None)

		self.assertEqual(self.wait_for_sync(self.attach_file(self.party)), "synced")

	def test_doctypes_with_source_are_not_blocked(self):
		self.assertEqual(self.wait_for_sync(self.attach_file(self.make_invoice())), "synced")


class TestReferenceBackfill(ReferenceTestCase):
	def setUp(self):
		super().setUp()
		self.mapping = self.reference_mapping({INVOICE_DOCTYPE: "reference_no"}, label="Reference No")
		self.invoice = self.make_invoice(reference_no="RE-1")
		self.file_doc = self.synced_file(self.invoice)

	def test_changed_sources_flag_linked_files_and_push_the_new_values(self):
		self._set_sources({INVOICE_DOCTYPE: "name"})

		self.assertEqual(self.file_doc.reload().svan_paperless_needs_update, 1)
		sync_metadata_updates(enforce_limits=False)
		self.assertIn(
			{"field": self.field_id(), "value": self.invoice.name},
			custom_field_values(self.document_of(self.file_doc)),
		)

	def test_flagged_file_is_not_detached_by_the_old_value(self):
		self._set_sources({INVOICE_DOCTYPE: "name"})

		# Paperless NGX still has "RE-1", which no invoice ID matches.
		self.webhook(self.file_doc.svan_paperless_document_id)

		self.assertTrue(frappe.db.exists("File", self.file_doc.name))

	def test_new_reference_flags_linked_files(self):
		self.set_reference(None)

		self.set_reference(self.mapping)

		self.assertEqual(self.file_doc.reload().svan_paperless_needs_update, 1)

	def test_saving_with_the_same_reference_flags_nothing(self):
		self.settings.save()

		self.assertEqual(self.file_doc.reload().svan_paperless_needs_update, 0)

	def test_renaming_the_custom_field_flags_nothing(self):
		mapping = frappe.get_doc(MAPPING, self.mapping)
		mapping.paperless_custom_field_name = self.name("Renamed Reference")
		mapping.save()

		self.assertEqual(self.file_doc.reload().svan_paperless_needs_update, 0)

	def test_files_of_doctypes_without_source_are_not_flagged(self):
		self.set_reference(None)
		self.whitelist_party()
		party_file = self.synced_file(self.make_party("Unflagged Party"))

		self.set_reference(self.mapping)

		self.assertEqual(party_file.reload().svan_paperless_needs_update, 0)

	def _set_sources(self, sources):
		mapping = frappe.get_doc(MAPPING, self.mapping)
		mapping.set(
			"field_sources",
			[
				{"source_doctype": doctype, "source_fieldname": fieldname}
				for doctype, fieldname in sources.items()
			],
		)
		mapping.save()


class TestCheckReferenceChange(ReferenceTestCase):
	def setUp(self):
		super().setUp()
		self.invoice = self.make_invoice()
		self.synced_file(self.invoice)

	def test_settings_with_a_new_reference_report_the_backfill(self):
		other = self.reference_mapping({INVOICE_DOCTYPE: "reference_no"}, label="Other")
		self.set_reference(self.mapping)
		settings = frappe.get_single("SVAN Paperless Settings").as_dict()
		settings["import_reference_mapping"] = other

		self.assertEqual(check_reference_change(frappe.as_json(settings)), {"gaps": [], "backfill": 1})

	def test_settings_with_unchanged_reference_report_no_backfill(self):
		settings = frappe.get_single("SVAN Paperless Settings").as_dict()

		self.assertEqual(check_reference_change(settings), {"gaps": [], "backfill": None})

	def test_settings_report_a_whitelisted_doctype_without_source(self):
		settings = frappe.get_single("SVAN Paperless Settings").as_dict()
		settings["whitelisted_doctypes"].append({"document_type": PARTY_DOCTYPE})

		self.assertEqual(check_reference_change(settings)["gaps"], [PARTY_DOCTYPE])

	def test_settings_without_reference_report_nothing(self):
		settings = frappe.get_single("SVAN Paperless Settings").as_dict()
		settings["import_reference_mapping"] = None
		settings["whitelisted_doctypes"].append({"document_type": PARTY_DOCTYPE})

		self.assertEqual(check_reference_change(settings), {"gaps": [], "backfill": None})

	def test_reference_mapping_with_changed_sources_reports_backfill_and_gaps(self):
		self.whitelist_party()
		mapping = frappe.get_doc(MAPPING, self.mapping).as_dict()
		mapping["field_sources"][0]["source_fieldname"] = "reference_no"

		self.assertEqual(check_reference_change(mapping), {"gaps": [PARTY_DOCTYPE], "backfill": 1})

	def test_other_mapping_reports_nothing(self):
		other = frappe.get_doc(
			{
				"doctype": MAPPING,
				"paperless_custom_field_name": self.name("Unrelated"),
				"paperless_custom_field_type": "string",
				"field_sources": [{"source_doctype": INVOICE_DOCTYPE, "source_fieldname": "reference_no"}],
			}
		).insert()

		self.assertEqual(check_reference_change(other.as_dict()), {"gaps": [], "backfill": None})
