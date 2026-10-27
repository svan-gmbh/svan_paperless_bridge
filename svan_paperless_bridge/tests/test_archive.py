# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from svan_paperless_bridge.archive import archived_name, is_archived
from svan_paperless_bridge.paperless_compare import compare_with_paperless
from svan_paperless_bridge.paperless_delete import cleanup_deleted_files, keeping_paperless_documents
from svan_paperless_bridge.paperless_import import import_from_paperless
from svan_paperless_bridge.paperless_sync import (
	poll_syncing_files,
	reconcile_metadata_changes,
	retroactively_delete_physical_files,
	sync_metadata_updates,
	sync_pending_files,
)
from svan_paperless_bridge.tests.paperless_test_case import PARTY_DOCTYPE, PaperlessTestCase, make_pdf
from svan_paperless_bridge.tests.reference_test_case import ReferenceTestCase

SYNC_LOG = "SVAN Paperless Sync Log"


class TestArchivedName(UnitTestCase):
	def test_marker_goes_before_the_extension(self):
		self.assertEqual(archived_name("Invoice.pdf", "Invoice.pdf", 64), "Invoice (archived).pdf")

	def test_title_without_extension_gets_the_marker_at_the_end(self):
		self.assertEqual(archived_name("Invoice", "Invoice.pdf", 128), "Invoice (archived)")

	def test_marker_is_not_added_twice(self):
		self.assertEqual(archived_name("Invoice (Archived).pdf", "Invoice.pdf", 64), "Invoice (Archived).pdf")

	def test_long_name_is_shortened_before_the_marker(self):
		name = archived_name("x" * 70 + ".pdf", "x" * 70 + ".pdf", 64)
		self.assertEqual(len(name), 64)
		self.assertTrue(name.endswith("x (archived).pdf"))

	def test_empty_label_takes_the_file_name(self):
		self.assertEqual(archived_name(None, "Invoice.pdf", 64), "Invoice (archived).pdf")


class ArchiveTestCase(ReferenceTestCase):
	"""The archive on, a reference on the invoice ID: every uploaded document would
	qualify for the import once it has no file.
	"""

	def setUp(self):
		super().setUp()
		self.settings.archive_deleted_files = 1
		self.settings.save()
		self.invoice = self.make_invoice()

	def label(self, file_doc):
		return self.paperless.version_label(
			file_doc.svan_paperless_document_id, file_doc.svan_paperless_version_id
		)

	def title(self, document_id):
		return self.paperless.document(document_id)["title"]

	def attachments(self, invoice=None):
		invoice = invoice or self.invoice
		return frappe.get_all(
			"File",
			filters={"attached_to_doctype": invoice.doctype, "attached_to_name": invoice.name},
			pluck="name",
			order_by="creation asc",
		)


class TestArchive(ArchiveTestCase):
	def test_deleting_the_only_file_archives_the_document(self):
		file_doc = self.synced_file(self.invoice)
		document_id = file_doc.svan_paperless_document_id

		frappe.delete_doc("File", file_doc.name)

		self.assertFalse(frappe.db.exists("File", file_doc.name))
		self.assertEqual(self.label(file_doc), f"{self.paperless.prefix}-scan (archived).pdf")
		self.assertTrue(is_archived(self.title(document_id)))
		self.assertTrue(
			frappe.db.exists(
				SYNC_LOG, {"reference_file": file_doc.name, "title": "Document archived in Paperless NGX"}
			)
		)

	def test_archived_document_is_not_imported_again(self):
		file_doc = self.synced_file(self.invoice)
		document_id = file_doc.svan_paperless_document_id
		frappe.delete_doc("File", file_doc.name)

		self.webhook(document_id)
		import_from_paperless(enforce_limits=False)

		self.assertEqual(self.attachments(), [])
		self.assertIsNone(self.queue_status(document_id))

	def test_imported_document_comes_back_once_the_mark_is_removed(self):
		document_id = self.paperless_document(self.invoice.name)
		import_from_paperless(enforce_limits=False)
		frappe.delete_doc("File", self.imported_file(document_id).name)
		self.assertIsNone(self.queue_status(document_id))

		self.paperless.patch_document(document_id, title=self.name("scan"))
		self.webhook(document_id)

		self.assertIsNotNone(self.imported_file(document_id))

	def test_webhook_skips_a_file_deleted_meanwhile(self):
		original = self.synced_file(self.invoice)
		version = self.synced_file(self.invoice)

		def delete_version(*args):
			with keeping_paperless_documents():
				frappe.delete_doc("File", version.name)

		with patch("svan_paperless_bridge.webhook_sync.apply_paperless_names", side_effect=delete_version):
			self.webhook(original.svan_paperless_document_id)

		self.assertEqual(self.attachments(), [original.name])

	def test_deleting_a_version_archives_only_that_version(self):
		original = self.synced_file(self.invoice)
		version = self.synced_file(self.invoice)
		document_id = original.svan_paperless_document_id
		title = self.title(document_id)

		frappe.delete_doc("File", version.name)
		self.webhook(document_id)
		compare_with_paperless(enforce_limits=False)

		self.assertTrue(is_archived(self.label(version)))
		self.assertFalse(is_archived(self.label(original)))
		self.assertEqual(self.title(document_id), title)
		self.assertEqual(self.attachments(), [original.name])

	def test_older_version_comes_back_once_the_mark_is_removed(self):
		original = self.synced_file(self.invoice)
		version = self.synced_file(self.invoice)
		document_id = original.svan_paperless_document_id
		frappe.delete_doc("File", original.name)

		self.paperless.set_version_label(document_id, document_id, original.file_name)
		self.webhook(document_id)

		self.assertEqual(
			frappe.get_all(
				"File",
				filters={"attached_to_name": self.invoice.name},
				pluck="svan_paperless_version_id",
				order_by="creation asc",
			),
			[version.svan_paperless_version_id, document_id],
		)

	def test_archived_version_is_taken_over_by_the_same_content_attached_again(self):
		original = self.synced_file(self.invoice)
		content = make_pdf()
		version = self.synced_file(self.invoice, content=content)
		frappe.delete_doc("File", version.name)

		again = self.synced_file(self.invoice, file_name="again.pdf", content=content)

		self.assertEqual(
			(again.svan_paperless_document_id, again.svan_paperless_version_id),
			(original.svan_paperless_document_id, version.svan_paperless_version_id),
		)
		self.assertEqual(self.label(again), again.file_name)

	def test_deleting_the_original_keeps_the_versions_without_asking(self):
		original = self.synced_file(self.invoice)
		version = self.synced_file(self.invoice)
		document_id = original.svan_paperless_document_id

		frappe.delete_doc("File", original.name)

		self.assertTrue(is_archived(self.label(original)))
		self.assertFalse(is_archived(self.title(document_id)))
		self.assertFalse(is_archived(self.label(version)))
		self.assertEqual(self.attachments(), [version.name])

	def test_deleting_the_business_document_archives_all_its_files(self):
		original = self.synced_file(self.invoice)
		version = self.synced_file(self.invoice)
		document_id = original.svan_paperless_document_id

		frappe.delete_doc(self.invoice.doctype, self.invoice.name)

		self.assertTrue(is_archived(self.title(document_id)))
		self.assertTrue(is_archived(self.label(original)))
		self.assertTrue(is_archived(self.label(version)))

	def test_unreachable_paperless_blocks_the_deletion(self):
		file_doc = self.synced_file(self.invoice)

		with self.paperless_unreachable():
			self.assertRaises(frappe.ValidationError, frappe.delete_doc, "File", file_doc.name)

		self.assertTrue(frappe.db.exists("File", file_doc.name))
		self.assertFalse(is_archived(self.label(file_doc)))

	def test_rolled_back_deletion_takes_the_marks_back(self):
		file_doc = self.synced_file(self.invoice)
		document_id = file_doc.svan_paperless_document_id
		title, label = self.title(document_id), self.label(file_doc)
		frappe.db.savepoint("before_delete")

		frappe.delete_doc("File", file_doc.name)
		self.assertTrue(is_archived(self.label(file_doc)))
		# What a full rollback does, without losing the test's own data.
		frappe.db.rollback(save_point="before_delete")
		frappe.db.after_rollback.run()

		self.assertEqual((self.title(document_id), self.label(file_doc)), (title, label))

	def test_marker_set_in_paperless_renames_a_linked_file_once(self):
		file_doc = self.synced_file(self.invoice)
		document_id = file_doc.svan_paperless_document_id
		archived = f"{self.paperless.prefix}-scan (archived).pdf"
		self.paperless.set_version_label(document_id, document_id, archived)

		self.webhook(document_id)

		self.assertEqual(file_doc.reload().file_name, archived)
		self.assertEqual(file_doc.svan_paperless_sync_status, "synced")
		frappe.delete_doc("File", file_doc.name)
		self.assertEqual(self.label(file_doc), archived)

	def test_import_takes_the_newest_version_not_archived(self):
		document_id = self.paperless_document(self.invoice.name)
		archived = self.paperless.add_version(document_id, self.name("second (archived).pdf"), make_pdf())
		self.paperless.set_version_label(document_id, archived, self.name("second (archived).pdf"))

		import_from_paperless(enforce_limits=False)

		self.assertEqual(self.imported_file(document_id).svan_paperless_version_id, str(document_id))


class TestArchiveSwitchedOff(ArchiveTestCase):
	def test_archived_document_comes_back_once_changed_in_paperless(self):
		file_doc = self.synced_file(self.invoice)
		document_id = file_doc.svan_paperless_document_id
		frappe.delete_doc("File", file_doc.name)
		self.settings.archive_deleted_files = 0
		self.settings.save()

		self.webhook(document_id)

		self.assertEqual(len(self.attachments()), 1)

	def test_archived_version_never_comes_back_on_its_own(self):
		original = self.synced_file(self.invoice)
		version = self.synced_file(self.invoice)
		frappe.delete_doc("File", version.name)
		self.settings.archive_deleted_files = 0
		self.settings.save()

		compare_with_paperless(enforce_limits=False)
		self.webhook(original.svan_paperless_document_id)

		self.assertEqual(self.attachments(), [original.name])

	def test_deleting_deletes_in_paperless_again(self):
		file_doc = self.synced_file(self.invoice)
		self.settings.archive_deleted_files = 0
		self.settings.save()

		frappe.delete_doc("File", file_doc.name)

		self.assertIsNone(self.paperless.document(file_doc.svan_paperless_document_id))


class TestArchiveKeepsEveryDocument(ArchiveTestCase):
	def test_no_document_or_version_is_lost(self):
		"""Every sync function and every kind of deletion with the archive on; all
		documents of the instance are compared, not only the test's own.
		"""
		self.settings.delete_physical_file = 1
		self.settings.save()
		original = self.synced_file(self.invoice)
		self.synced_file(self.invoice)
		self.synced_file(self.invoice, file_name="other.pdf")
		other_invoice = self.make_invoice()
		imported_id = self.paperless_document(other_invoice.name)
		import_from_paperless(enforce_limits=False)
		before = self.paperless.inventory()

		for file_name in self.attachments():
			frappe.delete_doc("File", file_name)
		frappe.delete_doc(other_invoice.doctype, other_invoice.name)
		self.webhook(original.svan_paperless_document_id)
		self.webhook(imported_id)
		sync_pending_files(enforce_limits=False)
		poll_syncing_files(enforce_limits=False)
		sync_metadata_updates(enforce_limits=False)
		reconcile_metadata_changes(enforce_limits=False)
		compare_with_paperless(enforce_limits=False)
		import_from_paperless(enforce_limits=False)
		retroactively_delete_physical_files()
		cleanup_deleted_files()

		after = self.paperless.inventory()
		lost = {
			document_id: document
			for document_id, document in before.items()
			if document_id not in after
			or {v["id"] for v in document["versions"]} - {v["id"] for v in after[document_id]["versions"]}
		}
		self.assertEqual(lost, {})
		self.assertTrue(is_archived(after[imported_id]["title"]))


class TestWhitelistRemoved(PaperlessTestCase):
	"""Files of a DocType taken off the whitelist stay as they are on both sides."""

	def setUp(self):
		super().setUp()
		self.invoice = self.make_invoice()
		self.file_doc = self.synced_file(self.invoice)
		self.document_id = self.file_doc.svan_paperless_document_id
		self.whitelist_only_the_party()

	def whitelist_only_the_party(self):
		self.settings.set(
			"whitelisted_doctypes",
			[{"document_type": PARTY_DOCTYPE, "paperless_document_type_name": self.name("Party")}],
		)
		self.settings.save()

	def test_deleting_the_file_leaves_paperless_alone(self):
		title = self.paperless.document(self.document_id)["title"]

		frappe.delete_doc("File", self.file_doc.name)

		self.assertEqual((self.paperless.document(self.document_id) or {}).get("title"), title)

	def test_document_deleted_in_paperless_keeps_the_file(self):
		self.paperless.delete_document(self.document_id)

		compare_with_paperless(enforce_limits=False)

		self.assertTrue(frappe.db.exists("File", self.file_doc.name))

	def test_changes_in_paperless_are_not_applied(self):
		tag_id = self.paperless.create("tags", self.name("Contract"))
		self.paperless.patch_document(self.document_id, tags=[tag_id])

		self.webhook(self.document_id)

		self.assertFalse(frappe.db.exists("Tag Link", {"document_name": self.invoice.name}))

	def test_changes_in_erpnext_wait_until_the_doctype_is_back(self):
		new_name = f"{self.paperless.prefix}-renamed.pdf"
		self.file_doc.reload().file_name = new_name
		self.file_doc.save()

		sync_metadata_updates(enforce_limits=False)
		self.assertNotEqual(self.paperless.version_label(self.document_id, self.document_id), new_name)

		self.settings = self.configure_settings()
		sync_metadata_updates(enforce_limits=False)
		self.assertEqual(self.paperless.version_label(self.document_id, self.document_id), new_name)

	def test_doctype_back_on_the_whitelist_pushes_its_files(self):
		self.configure_settings()

		self.assertEqual(frappe.db.get_value("File", self.file_doc.name, "svan_paperless_needs_update"), 1)
