# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
from frappe.model.document import Document


class SVANPaperlessFileType(Document):
	def before_naming(self):
		# Must run before the name is assigned (autoname: field:file_extension),
		# because set_new_name() builds the document name from the field value as
		# it was BEFORE validate() - see set_new_name() in
		# frappe/model/naming.py. Otherwise ".pdf" would end up unnormalized as
		# the document name even though the field already shows "PDF".
		self.normalize_file_extension()

	def validate(self):
		self.normalize_file_extension()

	def normalize_file_extension(self):
		"""Bring file_extension into the same format as Frappe's File.file_type
		(upper case, without a leading dot) - see set_file_type() in
		frappe/core/doctype/file/file.py. Only then is an exact comparison in
		paperless_sync.reconcile_file_classification() possible.
		"""
		if not self.file_extension:
			frappe.throw(frappe._("Please enter a file extension."))

		self.file_extension = self.file_extension.strip().lstrip(".").upper()
