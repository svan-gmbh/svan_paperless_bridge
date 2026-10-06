# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import time
import unittest
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from pypdf import PdfWriter

from svan_paperless_bridge.change_watch import invalidate_watched_doctype_map_cache
from svan_paperless_bridge.paperless_sync import poll_syncing_files, sync_pending_files
from svan_paperless_bridge.tests.live_paperless import NAME_PREFIX, LivePaperless

SETTINGS = "SVAN Paperless Settings"
PARTY_DOCTYPE = "Svan Test Party"
INVOICE_DOCTYPE = "Svan Test Invoice"
WEBHOOK_SECRET = "test-webhook-secret"
API_URL_CONFIG = "svan_paperless_test_api_url"
API_TOKEN_CONFIG = "svan_paperless_test_api_token"
# Discard port: refuses connections at once.
UNREACHABLE_API_URL = "http://127.0.0.1:9/api/"
SYNC_TIMEOUT_SECONDS = 60


class PaperlessTestCase(IntegrationTestCase):
	"""Runs against the real Paperless NGX named in site_config.json (superuser
	token required) and is skipped without it. The database is rolled back after
	every test; the app commits after every file on purpose, so those commits are
	disabled here. What the test created in Paperless NGX is deleted again.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.api_url = frappe.conf.get(API_URL_CONFIG)
		cls.api_token = frappe.conf.get(API_TOKEN_CONFIG)
		if not (cls.api_url and cls.api_token):
			raise unittest.SkipTest(f"{API_URL_CONFIG}/{API_TOKEN_CONFIG} missing in site_config.json")
		_ensure_test_doctypes()
		# Leftovers of an aborted run.
		LivePaperless(cls.api_url, cls.api_token).cleanup(NAME_PREFIX)

	def setUp(self):
		super().setUp()
		self.paperless = LivePaperless(self.api_url, self.api_token)
		self.addCleanup(self.paperless.cleanup)
		self.addCleanup(self._discard_changes)
		self._start(patch.object(frappe.local.db, "commit"))
		self.settings = self.configure_settings()

	def configure_settings(self):
		settings = frappe.get_single(SETTINGS)
		settings.update(
			{
				"api_url": self.api_url,
				"api_token": self.api_token,
				"default_correspondent": self.name("Default Correspondent"),
				"webhook_secret": WEBHOOK_SECRET,
				"delete_physical_file": 0,
				"enforce_document_permissions": 0,
			}
		)
		settings.set(
			"whitelisted_doctypes",
			[
				{
					"document_type": INVOICE_DOCTYPE,
					"party_fieldname": "party",
					"paperless_document_type_name": self.name("Invoice"),
				}
			],
		)
		settings.set("whitelisted_file_types", [{"file_type": "PDF"}, {"file_type": "TXT"}])
		settings.save()
		return settings

	def name(self, label):
		"""Every name that can reach Paperless NGX goes through here (see LivePaperless)."""
		return self.paperless.name(label)

	def make_party(self, label):
		return frappe.get_doc({"doctype": PARTY_DOCTYPE, "party_title": self.name(label)}).insert()

	def make_invoice(self, **values):
		return frappe.get_doc({"doctype": INVOICE_DOCTYPE, **values}).insert()

	def make_user(self):
		"""A user without any role, i.e. not exempt from Paperless permission checks."""
		return (
			frappe.get_doc(
				{
					"doctype": "User",
					"email": "paperless-test-user@example.com",
					"first_name": "Paperless Test",
					"send_welcome_email": 0,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def attach_file(self, invoice, file_name="scan.pdf", content=None, is_private=1):
		"""Insert a File; its after_insert hook already makes the first upload attempt."""
		return frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"{self.paperless.prefix}-{file_name}",
				"attached_to_doctype": invoice.doctype,
				"attached_to_name": invoice.name,
				"is_private": is_private,
				"content": content or make_pdf(),
			}
		).insert()

	def synced_file(self, invoice, **kwargs):
		file_doc = self.attach_file(invoice, **kwargs)
		self.assertEqual(self.wait_for_sync(file_doc), "synced")
		return file_doc

	def run_sync(self, file_doc):
		sync_pending_files(enforce_limits=False)
		return self.wait_for_sync(file_doc)

	def wait_for_sync(self, file_doc):
		"""Poll until Paperless NGX has consumed the upload; returns the final status."""
		deadline = time.monotonic() + SYNC_TIMEOUT_SECONDS
		while True:
			poll_syncing_files(enforce_limits=False)
			status = file_doc.reload().svan_paperless_sync_status
			if status != "syncing" or time.monotonic() > deadline:
				return status
			time.sleep(0.5)

	def document_of(self, file_doc):
		file_doc.reload()
		return self.paperless.document(file_doc.svan_paperless_document_id)

	def entity_name(self, endpoint, entity_id):
		return self.paperless.get(f"/{endpoint}/{entity_id}/")["name"] if entity_id else None

	@contextmanager
	def paperless_unreachable(self):
		frappe.db.set_single_value(SETTINGS, "api_url", UNREACHABLE_API_URL)
		try:
			yield
		finally:
			frappe.db.set_single_value(SETTINGS, "api_url", self.api_url)

	def _start(self, patcher):
		patcher.start()
		self.addCleanup(patcher.stop)

	def _discard_changes(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.flags.svan_paperless_applying_webhook_update = False
		# The rollback does not reach the Redis caches filled during the test.
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		invalidate_watched_doctype_map_cache()


def custom_field_values(document):
	"""The document's custom field entries without extra keys Paperless NGX may add."""
	return [{"field": entry["field"], "value": entry["value"]} for entry in document["custom_fields"]]


def make_pdf(marker=None):
	"""A valid one-page PDF (Frappe parses PDFs on insert); a random marker keeps
	the checksum unique.
	"""
	writer = PdfWriter()
	writer.add_blank_page(10, 10)
	writer.add_metadata({"/Subject": marker or frappe.generate_hash()})
	buffer = BytesIO()
	writer.write(buffer)
	return buffer.getvalue()


def _ensure_test_doctypes():
	"""Create the business DocTypes the tests attach files to. Not rolled back
	(DDL commits implicitly), so they are only created once per site.
	"""
	if not frappe.db.exists("DocType", PARTY_DOCTYPE):
		_new_doctype(
			PARTY_DOCTYPE,
			[{"fieldname": "party_title", "fieldtype": "Data", "label": "Title", "reqd": 1}],
			title_field="party_title",
		)
	if not frappe.db.exists("DocType", INVOICE_DOCTYPE):
		_new_doctype(
			INVOICE_DOCTYPE,
			[
				{"fieldname": "party", "fieldtype": "Link", "label": "Party", "options": PARTY_DOCTYPE},
				{
					"fieldname": "party_type",
					"fieldtype": "Select",
					"label": "Party Type",
					"options": f"\n{PARTY_DOCTYPE}",
				},
				{
					"fieldname": "party_name",
					"fieldtype": "Dynamic Link",
					"label": "Party Name",
					"options": "party_type",
				},
				{"fieldname": "reference_no", "fieldtype": "Data", "label": "Reference No"},
				{"fieldname": "due_date", "fieldtype": "Date", "label": "Due Date"},
			],
		)
	frappe.db.commit()


def _new_doctype(name, fields, **properties):
	frappe.get_doc(
		{
			"doctype": "DocType",
			"name": name,
			"module": "SVAN Paperless NGX Bridge",
			"custom": 1,
			"autoname": "hash",
			"fields": fields,
			"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
			**properties,
		}
	).insert()
