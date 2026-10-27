# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe

from svan_paperless_bridge.document_type_sync import get_paperless_document_type_id_for_document
from svan_paperless_bridge.tests.paperless_test_case import (
	INVOICE_DOCTYPE,
	PARTY_DOCTYPE,
	PaperlessTestCase,
	make_pdf,
)

IMPORT_DOCTYPE = "SVAN Paperless Import"
MAPPING = "SVAN Paperless Custom Field Mapping"


class ReferenceTestCase(PaperlessTestCase):
	"""A reference for the import on the invoice ID; documents created in
	Paperless NGX carry it.
	"""

	def setUp(self):
		super().setUp()
		self.mapping = self.reference_mapping({INVOICE_DOCTYPE: "name"})

	def reference_mapping(self, sources, bidirectional=0, label="Reference"):
		mapping = frappe.get_doc(
			{
				"doctype": MAPPING,
				"paperless_custom_field_name": self.name(label),
				"paperless_custom_field_type": "string",
				"bidirectional": bidirectional,
				"field_sources": [
					{"source_doctype": doctype, "source_fieldname": fieldname}
					for doctype, fieldname in sources.items()
				],
			}
		).insert()
		self.set_reference(mapping.name)
		return mapping.name

	def set_reference(self, mapping, import_tag=None):
		self.settings.import_reference_mapping, self.settings.import_tag = mapping, import_tag
		self.settings.save()

	def whitelist_party(self):
		self.settings.append(
			"whitelisted_doctypes",
			{"document_type": PARTY_DOCTYPE, "paperless_document_type_name": self.name("Party")},
		)
		self.settings.save()

	def add_source(self, doctype, fieldname):
		mapping = frappe.get_doc(MAPPING, self.settings.import_reference_mapping)
		mapping.append("field_sources", {"source_doctype": doctype, "source_fieldname": fieldname})
		mapping.save()

	def paperless_document(self, reference, doctype=INVOICE_DOCTYPE, content=None, tags=None):
		"""A document created in Paperless NGX with the document type of doctype and a reference."""
		document_id = self.paperless.upload(content or make_pdf(), self.name("scan"))
		self.set_paperless_reference(document_id, reference, doctype, tags=tags or [])
		return document_id

	def set_paperless_reference(self, document_id, reference, doctype=INVOICE_DOCTYPE, **fields):
		document_type_id = get_paperless_document_type_id_for_document(None, doctype, self.settings)
		self.paperless.patch_document(
			document_id,
			document_type=document_type_id,
			custom_fields=[{"field": self.field_id(), "value": reference}],
			**fields,
		)

	def field_id(self):
		mapping = frappe.get_doc(MAPPING, self.settings.import_reference_mapping)
		if not mapping.paperless_custom_field_id:
			mapping.paperless_custom_field_id = self.paperless.create(
				"custom_fields", mapping.paperless_custom_field_name, data_type="string"
			)
			mapping.save()
		return int(mapping.paperless_custom_field_id)

	def tag_id(self, tag):
		frappe.get_doc({"doctype": "Tag", "name": tag}).insert(ignore_if_duplicate=True)
		tag_id = self.paperless.create("tags", tag)
		frappe.get_doc(
			{"doctype": "SVAN Paperless Tag Mapping", "frappe_tag": tag, "paperless_tag_id": tag_id}
		).insert()
		return tag_id

	def files_of(self, document_id):
		return frappe.get_all(
			"File",
			filters={"svan_paperless_document_id": str(document_id)},
			fields=["name", "attached_to_doctype", "attached_to_name", "svan_paperless_version_id"],
		)

	def imported_file(self, document_id):
		name = frappe.db.get_value("File", {"svan_paperless_document_id": str(document_id)})
		return frappe.get_doc("File", name) if name else None

	def queue_status(self, document_id):
		return frappe.db.get_value(IMPORT_DOCTYPE, str(document_id), "status")

	def tags_of(self, doc):
		return frappe.get_all(
			"Tag Link", filters={"document_type": doc.doctype, "document_name": doc.name}, pluck="tag"
		)
