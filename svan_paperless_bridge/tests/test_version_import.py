# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import os

import frappe

from svan_paperless_bridge.paperless_compare import CRON_SOURCE, compare_with_paperless
from svan_paperless_bridge.paperless_delete import delete_keeping_paperless_original
from svan_paperless_bridge.paperless_import import import_from_paperless
from svan_paperless_bridge.preview import stream
from svan_paperless_bridge.tests.paperless_test_case import PARTY_DOCTYPE, PaperlessTestCase, make_pdf
from svan_paperless_bridge.tests.reference_test_case import ReferenceTestCase
from svan_paperless_bridge.tests.test_import import _png

SYNC_LOG = "SVAN Paperless Sync Log"
IMPORTED = "Version imported from Paperless NGX"


class VersionImportMixin:
	def attachments(self, invoice):
		return frappe.get_all(
			"File",
			filters={"attached_to_doctype": invoice.doctype, "attached_to_name": invoice.name},
			fields=["name", "svan_paperless_document_id", "svan_paperless_version_id"],
			order_by="creation asc",
		)

	def file_of_version(self, version_id):
		name = frappe.db.get_value("File", {"svan_paperless_version_id": str(version_id)})
		return frappe.get_doc("File", name) if name else None

	def preview(self, file_doc):
		stream(file_doc.name)
		return frappe.response["filecontent"]

	def problems_logged_since(self, started):
		return frappe.get_all(
			SYNC_LOG,
			filters={"log_type": ["in", ["Warn", "Error"]], "log_datetime": [">=", started]},
			pluck="title",
		)


class TestVersionImport(VersionImportMixin, PaperlessTestCase):
	"""Documents uploaded from ERPNext."""

	def setUp(self):
		super().setUp()
		self.settings.delete_physical_file = 1
		self.settings.save()
		self.invoice = self.make_invoice()
		self.original = self.synced_file(self.invoice)
		self.document_id = self.original.svan_paperless_document_id

	def test_comparison_imports_a_version_added_in_paperless(self):
		content = make_pdf()
		version_id = self.paperless.add_version(self.document_id, self.name("second.pdf"), content)

		compare_with_paperless(enforce_limits=False)

		file_doc = self.file_of_version(version_id)
		self.assertEqual(
			(file_doc.attached_to_doctype, file_doc.attached_to_name),
			(self.invoice.doctype, self.invoice.name),
		)
		self.assertEqual(file_doc.svan_paperless_document_id, self.document_id)
		self.assertEqual(file_doc.svan_paperless_sync_status, "synced")
		self.assertEqual(file_doc.svan_source, "paperless")
		self.assertEqual(file_doc.file_name, self.name("second.pdf"))
		self.assertTrue(file_doc.file_url.startswith("/api/method/"))
		self.assertEqual(self.preview(file_doc), content)
		self.assertTrue(
			frappe.db.exists(
				SYNC_LOG, {"reference_file": file_doc.name, "title": IMPORTED, "source": CRON_SOURCE}
			)
		)

	def test_imported_version_is_named_after_its_label(self):
		version_id = self.paperless.add_version(self.document_id, self.name("second.pdf"), make_pdf())
		label = f"{self.paperless.prefix}-labelled"
		self.paperless.set_version_label(self.document_id, version_id, label)

		compare_with_paperless(enforce_limits=False)

		self.assertEqual(self.file_of_version(version_id).file_name, f"{label}.pdf")

	def test_label_stays_the_name_when_it_exists_on_disk(self):
		version_id = self.paperless.add_version(self.document_id, self.name("second.pdf"), make_pdf())
		label = f"{self.paperless.prefix}-on disk.pdf"
		self.paperless.set_version_label(self.document_id, version_id, label)
		path = frappe.get_site_path("private", "files", label)
		with open(path, "wb") as f:
			f.write(b"another file")
		self.addCleanup(os.remove, path)

		compare_with_paperless(enforce_limits=False)

		self.assertEqual(self.file_of_version(version_id).file_name, label)

	def test_versions_are_imported_in_their_order(self):
		first = self.paperless.add_version(self.document_id, self.name("second.pdf"), make_pdf())
		second = self.paperless.add_version(self.document_id, self.name("third.pdf"), make_pdf())

		compare_with_paperless(enforce_limits=False)

		self.assertEqual(
			[f.svan_paperless_version_id for f in self.attachments(self.invoice)],
			[self.document_id, str(first), str(second)],
		)

	def test_webhook_and_comparison_import_a_version_once(self):
		version_id = self.paperless.add_version(self.document_id, self.name("second.pdf"), make_pdf())

		self.webhook(self.document_id)
		compare_with_paperless(enforce_limits=False)
		self.webhook(self.document_id)

		self.assertEqual(
			[f.svan_paperless_version_id for f in self.attachments(self.invoice)],
			[self.document_id, str(version_id)],
		)

	def test_own_version_upload_is_not_imported_again(self):
		version = self.attach_file(self.invoice)
		version.reload()
		self.assertEqual(version.svan_paperless_document_id, self.document_id)
		# Consumed by Paperless NGX, but the poll has not stored the version ID yet.
		self.paperless.wait_for_task(version.svan_paperless_uuid)

		self.webhook(self.document_id)
		self.assertEqual(self.wait_for_sync(version), "synced")
		self.webhook(self.document_id)
		compare_with_paperless(enforce_limits=False)

		self.assertEqual([f.name for f in self.attachments(self.invoice)], [self.original.name, version.name])

	def test_original_deleted_in_erpnext_is_not_imported_again(self):
		version = self.synced_file(self.invoice)
		delete_keeping_paperless_original(self.original.name)
		self.assertIn("(archived)", self.paperless.version_label(self.document_id, self.document_id))

		self.webhook(self.document_id)
		compare_with_paperless(enforce_limits=False)

		self.assertEqual([f.name for f in self.attachments(self.invoice)], [version.name])

	def test_deleted_imported_version_is_not_imported_again(self):
		version_id = self.paperless.add_version(self.document_id, self.name("second.pdf"), make_pdf())
		self.webhook(self.document_id)

		frappe.delete_doc("File", self.file_of_version(version_id).name)

		self.assertEqual(
			[v["id"] for v in self.document_of(self.original)["versions"]], [int(self.document_id)]
		)
		self.webhook(self.document_id)
		compare_with_paperless(enforce_limits=False)
		self.assertEqual([f.name for f in self.attachments(self.invoice)], [self.original.name])

	def test_version_restored_from_the_trash_is_imported_again(self):
		content = make_pdf()
		version = self.synced_file(self.invoice, content=content)
		version_id = version.svan_paperless_version_id
		frappe.delete_doc("File", version.name)

		self.paperless.restore(version_id)
		compare_with_paperless(enforce_limits=False)

		file_doc = self.file_of_version(version_id)
		self.assertEqual(file_doc.attached_to_name, self.invoice.name)
		self.assertEqual(self.preview(file_doc), content)

	def test_doctype_no_longer_whitelisted_gets_no_versions(self):
		self.settings.set(
			"whitelisted_doctypes",
			[{"document_type": PARTY_DOCTYPE, "paperless_document_type_name": self.name("Party")}],
		)
		self.settings.save()
		self.paperless.add_version(self.document_id, self.name("second.pdf"), make_pdf())

		self.webhook(self.document_id)
		compare_with_paperless(enforce_limits=False)

		self.assertEqual([f.name for f in self.attachments(self.invoice)], [self.original.name])

	def test_same_content_uploaded_in_erpnext_takes_the_version_added_in_paperless(self):
		content = make_pdf()
		version_id = self.paperless.add_version(self.document_id, self.name("second.pdf"), content)

		file_doc = self.synced_file(self.invoice, content=content)

		self.assertEqual(file_doc.svan_paperless_document_id, self.document_id)
		self.assertEqual(file_doc.svan_paperless_version_id, str(version_id))
		self.assertEqual(len(self.document_of(self.original)["versions"]), 2)
		self.assertEqual(self.preview(file_doc), content)
		compare_with_paperless(enforce_limits=False)
		self.assertEqual(len(self.attachments(self.invoice)), 2)


class TestVersionImportOfImportedDocuments(VersionImportMixin, ReferenceTestCase):
	"""Documents created in Paperless NGX and imported."""

	def setUp(self):
		super().setUp()
		self.settings.delete_physical_file = 1
		self.settings.save()
		self.invoice = self.make_invoice()

	def test_webhook_imports_a_version_of_an_imported_document(self):
		document_id = self.paperless_document(self.invoice.name)
		import_from_paperless(enforce_limits=False)
		started = frappe.utils.now_datetime()
		png = _png()

		version_id = self.paperless.add_version(document_id, self.name("second.png"), png)
		self.webhook(document_id)

		file_doc = self.file_of_version(version_id)
		self.assertEqual(file_doc.attached_to_name, self.invoice.name)
		self.assertEqual(file_doc.file_name, self.name("second.png"))
		self.assertTrue(file_doc.file_url.startswith("/api/method/"))
		self.assertEqual(self.preview(file_doc), png)
		self.assertTrue(frappe.db.exists(SYNC_LOG, {"reference_file": file_doc.name, "title": IMPORTED}))
		self.assertEqual(self.problems_logged_since(started), [])

	def test_first_import_takes_the_newest_version_and_the_others_follow(self):
		document_id = self.paperless.upload(make_pdf(), self.name("scan"))
		second = self.paperless.add_version(document_id, self.name("second.pdf"), make_pdf())
		self.set_paperless_reference(document_id, self.invoice.name)
		import_from_paperless(enforce_limits=False)
		self.assertEqual([f.svan_paperless_version_id for f in self.attachments(self.invoice)], [str(second)])

		third = self.paperless.add_version(document_id, self.name("third.pdf"), make_pdf())
		self.webhook(document_id)

		self.assertEqual(
			[f.svan_paperless_version_id for f in self.attachments(self.invoice)],
			[str(second), str(document_id), str(third)],
		)

	def test_version_follows_a_reference_change_in_the_same_update(self):
		document_id = self.paperless_document(self.invoice.name)
		import_from_paperless(enforce_limits=False)
		other = self.make_invoice()

		version_id = self.paperless.add_version(document_id, self.name("second.pdf"), make_pdf())
		self.set_paperless_reference(document_id, other.name)
		self.webhook(document_id)

		self.assertEqual(self.attachments(self.invoice), [])
		self.assertEqual(
			[f.svan_paperless_version_id for f in self.attachments(other)],
			[str(document_id), str(version_id)],
		)
