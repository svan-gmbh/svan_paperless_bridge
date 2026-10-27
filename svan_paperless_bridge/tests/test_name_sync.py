# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

from urllib.parse import quote

import frappe

from svan_paperless_bridge.paperless_compare import compare_with_paperless
from svan_paperless_bridge.paperless_import import import_from_paperless
from svan_paperless_bridge.paperless_sync import sync_metadata_updates
from svan_paperless_bridge.preview import stream
from svan_paperless_bridge.tests.paperless_test_case import PaperlessTestCase, make_pdf
from svan_paperless_bridge.tests.reference_test_case import ReferenceTestCase

SYNC_LOG = "SVAN Paperless Sync Log"
SYNCED_FIELDS = ("svan_paperless_synced_name", "svan_paperless_synced_title", "svan_paperless_synced_label")


class NameSyncMixin:
	def title(self, file_doc):
		return self.document_of(file_doc)["title"]

	def label(self, file_doc):
		file_doc.reload()
		return self.paperless.version_label(
			file_doc.svan_paperless_document_id, file_doc.svan_paperless_version_id
		)

	def rename(self, file_doc, file_name):
		file_doc.reload()
		file_doc.file_name = file_name
		file_doc.save()

	def logged(self, file_doc, title):
		return frappe.db.exists(SYNC_LOG, {"reference_file": file_doc.name, "title": title})


class TestNameSync(NameSyncMixin, PaperlessTestCase):
	def setUp(self):
		super().setUp()
		self.settings.delete_physical_file = 1
		self.settings.save()
		self.invoice = self.make_invoice()
		self.original = self.synced_file(self.invoice)
		self.document_id = self.original.svan_paperless_document_id

	def version(self):
		version = self.synced_file(self.invoice)
		self.assertEqual(version.svan_paperless_document_id, self.document_id)
		return version

	def test_upload_sets_title_and_label_of_the_original(self):
		self.assertEqual(self.title(self.original), self.original.file_name)
		self.assertEqual(self.label(self.original), self.original.file_name)
		self.assertEqual(self.original.svan_paperless_synced_name, self.original.file_name)

	def test_label_changed_in_paperless_during_the_upload_is_taken_over(self):
		version = self.attach_file(self.invoice).reload()
		version_id = self.paperless.wait_for_task(version.svan_paperless_uuid)
		new_name = f"{self.paperless.prefix}-renamed during upload.pdf"
		self.paperless.set_version_label(self.document_id, version_id, new_name)

		self.assertEqual(self.wait_for_sync(version), "synced")

		self.assertEqual(version.reload().file_name, new_name)
		self.assertEqual(self.label(version), new_name)

	def test_title_changed_in_paperless_during_the_upload_is_taken_over(self):
		file_doc = self.attach_file(self.make_invoice(), file_name="other.pdf").reload()
		document_id = self.paperless.wait_for_task(file_doc.svan_paperless_uuid)
		new_name = f"{self.paperless.prefix}-renamed during upload.pdf"
		self.paperless.patch_document(document_id, title=new_name)

		self.assertEqual(self.wait_for_sync(file_doc), "synced")

		self.assertEqual(file_doc.reload().file_name, new_name)
		self.assertEqual(self.label(file_doc), new_name)

	def test_rename_of_the_original_in_erpnext_is_transferred(self):
		new_name = self.name("renamed.pdf")
		self.rename(self.original, new_name)
		self.assertEqual(self.original.reload().svan_paperless_needs_update, 1)

		sync_metadata_updates(enforce_limits=False)

		self.assertEqual(self.title(self.original), new_name)
		self.assertEqual(self.label(self.original), new_name)
		self.original.reload()
		self.assertEqual(
			(self.original.svan_paperless_synced_name, self.original.svan_paperless_needs_update),
			(new_name, 0),
		)
		self.assertTrue(self.logged(self.original, "File name transferred to Paperless NGX"))
		# The webhook fired by the transfer changes nothing back.
		self.webhook(self.document_id)
		self.assertEqual(self.original.reload().file_name, new_name)

	def test_rename_of_a_version_changes_only_its_label(self):
		version = self.version()
		self.assertEqual(self.label(version), version.file_name)
		new_name = self.name("second.pdf")

		self.rename(version, new_name)
		sync_metadata_updates(enforce_limits=False)

		self.assertEqual(self.label(version), new_name)
		self.assertEqual(self.title(self.original), self.original.file_name)
		self.assertEqual(self.label(self.original), self.original.file_name)

	def test_title_changed_in_paperless_renames_the_original(self):
		content = self.original.get_content()
		self.paperless.patch_document(self.document_id, title=self.name("Invoice March"))

		self.webhook(self.document_id)

		self.original.reload()
		new_name = self.name("Invoice March.pdf")
		self.assertEqual(self.original.file_name, new_name)
		self.assertIn(quote(new_name), self.original.file_url)
		stream(self.original.name)
		self.assertEqual(frappe.response["filecontent"], content)
		self.assertEqual(frappe.response["filename"], new_name)
		# The title keeps its form without extension, the label follows.
		self.assertEqual(self.title(self.original), self.name("Invoice March"))
		self.assertEqual(self.label(self.original), new_name)
		self.assertTrue(
			self.logged(self.original, "File renamed after a change of the title in Paperless NGX")
		)
		self.webhook(self.document_id)
		self.assertEqual(self.original.reload().file_name, new_name)

	def test_label_of_the_original_changed_in_paperless_renames_it(self):
		new_name = self.name("by label.pdf")
		self.paperless.set_version_label(self.document_id, self.document_id, new_name)

		self.webhook(self.document_id)

		self.assertEqual(self.original.reload().file_name, new_name)
		self.assertEqual(self.title(self.original), new_name)

	def test_label_of_a_version_changed_in_paperless_renames_only_it(self):
		version = self.version()
		self.paperless.set_version_label(self.document_id, version.svan_paperless_version_id, self.name("v2"))

		self.webhook(self.document_id)

		self.assertEqual(version.reload().file_name, self.name("v2.pdf"))
		self.assertEqual(self.original.reload().file_name, self.original.svan_paperless_synced_name)
		self.assertEqual(self.title(self.original), self.original.file_name)

	def test_comparison_takes_a_change_without_webhook(self):
		self.paperless.patch_document(self.document_id, title=self.name("from comparison"))

		compare_with_paperless(enforce_limits=False)

		self.assertEqual(self.original.reload().file_name, self.name("from comparison.pdf"))

	def test_pending_rename_in_erpnext_wins(self):
		new_name = self.name("erpnext.pdf")
		self.rename(self.original, new_name)
		self.paperless.patch_document(self.document_id, title=self.name("paperless"))

		self.webhook(self.document_id)
		self.assertEqual(self.original.reload().file_name, new_name)

		sync_metadata_updates(enforce_limits=False)
		self.assertEqual(self.title(self.original), new_name)
		compare_with_paperless(enforce_limits=False)
		self.assertEqual(self.original.reload().file_name, new_name)

	def test_files_linked_before_keep_their_names(self):
		file_name = self.original.file_name
		frappe.db.set_value("File", self.original.name, dict.fromkeys(SYNCED_FIELDS, ""))
		self.paperless.patch_document(self.document_id, title=self.name("older title"))

		compare_with_paperless(enforce_limits=False)

		self.original.reload()
		self.assertEqual(self.original.file_name, file_name)
		self.assertEqual(self.original.svan_paperless_synced_title, self.name("older title"))
		self.assertEqual(self.title(self.original), self.name("older title"))

		self.paperless.patch_document(self.document_id, title=self.name("newer title"))
		compare_with_paperless(enforce_limits=False)
		self.assertEqual(self.original.reload().file_name, self.name("newer title.pdf"))

	def test_rename_past_the_hooks_is_transferred(self):
		new_name = self.name("set value.pdf")
		frappe.db.set_value("File", self.original.name, "file_name", new_name)

		compare_with_paperless(enforce_limits=False)
		sync_metadata_updates(enforce_limits=False)

		self.assertEqual(self.title(self.original), new_name)

	def test_long_name_is_shortened_before_the_extension(self):
		new_name = self.name("x" * 100 + ".pdf")
		self.rename(self.original, new_name)

		sync_metadata_updates(enforce_limits=False)

		label = self.label(self.original)
		self.assertEqual(len(label), 64)
		self.assertEqual(label, new_name[:60] + ".pdf")
		self.assertEqual(self.title(self.original), new_name)
		self.webhook(self.document_id)
		self.assertEqual(self.original.reload().file_name, new_name)

	def test_emptied_names_are_not_taken(self):
		version = self.version()
		self.paperless.set_version_label(self.document_id, version.svan_paperless_version_id, "")
		self.paperless.patch_document(self.document_id, title="")

		self.webhook(self.document_id)

		self.assertEqual(version.reload().file_name, version.svan_paperless_synced_name)
		self.assertEqual(self.original.reload().file_name, self.original.svan_paperless_synced_name)

	def test_slash_is_dropped(self):
		self.paperless.patch_document(self.document_id, title=self.name("03/2026"))

		self.webhook(self.document_id)

		self.assertEqual(self.original.reload().file_name, self.name("032026.pdf"))


class TestNameSyncOfLocalFiles(NameSyncMixin, PaperlessTestCase):
	"""Content kept in ERPNext (delete_physical_file off)."""

	def setUp(self):
		super().setUp()
		self.settings.delete_physical_file = 0
		self.settings.save()
		self.invoice = self.make_invoice()

	def test_rename_never_touches_the_files_on_disk(self):
		content, other_content = make_pdf(), make_pdf()
		original = self.synced_file(self.invoice, content=content)
		other = self.synced_file(self.invoice, file_name="other.pdf", content=other_content)
		self.assertNotEqual(original.svan_paperless_document_id, other.svan_paperless_document_id)
		file_url, old_name = original.file_url, original.file_name

		# The same name as the other file: only the field may change, never a path.
		self.paperless.patch_document(
			original.svan_paperless_document_id, title=f"{self.paperless.prefix}-other"
		)
		self.webhook(original.svan_paperless_document_id)

		original.reload()
		self.assertEqual(original.file_name, other.file_name)
		self.assertEqual(original.file_url, file_url)
		self.assertEqual(original.get_content(encodings=()), content)
		self.assertEqual(other.reload().get_content(encodings=()), other_content)

		# A new upload under the old name gets a path of its own, as Frappe always does.
		new_content = make_pdf()
		new = self.attach_file(self.invoice, content=new_content)
		self.assertEqual(new.file_name, old_name.replace(".pdf", f"{new.content_hash[-6:]}.pdf"))
		self.assertNotEqual(new.file_url, file_url)
		self.assertEqual(original.reload().get_content(encodings=()), content)
		self.assertEqual(new.get_content(encodings=()), new_content)


class TestNameSyncOfImportedDocuments(NameSyncMixin, ReferenceTestCase):
	def setUp(self):
		super().setUp()
		self.settings.delete_physical_file = 1
		self.settings.save()
		self.invoice = self.make_invoice()

	def imported(self):
		document_id = self.paperless_document(self.invoice.name, content=make_pdf())
		self.paperless.patch_document(document_id, title=self.name("kept in Paperless"))
		import_from_paperless(enforce_limits=False)
		return document_id, self.imported_file(document_id)

	def test_imported_file_keeps_its_name_and_paperless_its_title(self):
		document_id, file_doc = self.imported()
		file_name = file_doc.file_name

		self.webhook(document_id)
		compare_with_paperless(enforce_limits=False)

		self.assertEqual(file_doc.reload().file_name, file_name)
		self.assertEqual(self.title(file_doc), self.name("kept in Paperless"))

	def test_title_changed_later_renames_the_imported_file(self):
		document_id, file_doc = self.imported()

		self.paperless.patch_document(document_id, title=self.name("changed"))
		self.webhook(document_id)

		self.assertEqual(file_doc.reload().file_name, self.name("changed.pdf"))
