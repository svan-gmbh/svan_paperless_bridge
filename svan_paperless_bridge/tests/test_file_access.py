# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
from frappe.permissions import add_permission, update_permission_property

from svan_paperless_bridge.preview import stream
from svan_paperless_bridge.tests.paperless_test_case import INVOICE_DOCTYPE, PaperlessTestCase, make_pdf

ROLE = "Svan Test Invoice User"


class TestFileAccessOfOrdinaryUser(PaperlessTestCase):
	"""A user who may only read and write invoices (no System Manager)."""

	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.clear_cache, doctype=INVOICE_DOCTYPE)
		frappe.get_doc({"doctype": "Role", "role_name": ROLE, "desk_access": 1}).insert(
			ignore_if_duplicate=True
		)
		add_permission(INVOICE_DOCTYPE, ROLE)
		update_permission_property(INVOICE_DOCTYPE, ROLE, 0, "write", 1)
		self.user = self.make_user()
		frappe.get_doc("User", self.user).add_roles(ROLE)
		self.invoice = self.make_invoice()

	def test_attached_file_is_uploaded_and_visible(self):
		frappe.set_user(self.user)
		file_doc = self.attach_file(self.invoice)

		self.assertEqual(self.wait_for_sync(file_doc), "synced")
		visible = frappe.get_list(
			"File", filters={"attached_to_doctype": INVOICE_DOCTYPE, "attached_to_name": self.invoice.name}
		)
		self.assertEqual([f.name for f in visible], [file_doc.name])

	def test_offloaded_file_is_previewed(self):
		self.settings.delete_physical_file = 1
		self.settings.save()
		content = make_pdf()
		frappe.set_user(self.user)
		file_doc = self.synced_file(self.invoice, content=content)

		stream(file_doc.name)

		self.assertEqual(frappe.response["filecontent"], content)

	def test_attaching_to_an_invoice_without_write_permission_is_refused(self):
		update_permission_property(INVOICE_DOCTYPE, ROLE, 0, "write", 0)
		frappe.set_user(self.user)

		self.assertRaises(frappe.PermissionError, self.attach_file, self.invoice)
