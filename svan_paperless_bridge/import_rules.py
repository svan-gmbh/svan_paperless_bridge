# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

"""Which business document a Paperless NGX document belongs to, according to the
"Reference For Import" of the Settings (a custom field mapping).

Shared by the import of new files (paperless_import.py) and the moving/detaching
of linked files (document_relink.py).
"""

import frappe

from svan_paperless_bridge.custom_field_sync import ensure_paperless_custom_field_mapping
from svan_paperless_bridge.document_type_sync import get_paperless_document_type_id_for_document
from svan_paperless_bridge.tag_sync import ensure_paperless_tag_mapping


class ImportRules:
	"""One rule per whitelisted DocType the reference has a source on, resolved to
	Paperless NGX IDs. Missing document types, custom fields and tags are created
	there like on upload. Empty (falsy) while no reference is configured.
	"""

	def __init__(self, settings):
		self.by_document_type = {}
		self.import_tag_ids = set()
		mapping = _reference_mapping(settings)
		tag_id = _paperless_tag_id(settings.import_tag, settings) if settings.import_tag else None
		if not mapping or (settings.import_tag and not tag_id):
			return

		self.import_tag_ids = {tag_id} if tag_id else set()
		for row in settings.whitelisted_doctypes:
			rule = ImportRule.resolve(row.document_type, mapping, tag_id, settings)
			if rule:
				self.by_document_type.setdefault(rule.document_type_id, []).append(rule)

	def __bool__(self):
		return bool(self.by_document_type)

	def matches(self, document, require_tag=True):
		"""(rule, reference value) for every rule the document fulfils. The import tag
		is only needed to link a document, not to move a linked one.
		"""
		values = {
			entry.get("field"): reference_value(entry.get("value"))
			for entry in document.get("custom_fields") or []
		}
		tags = document.get("tags") or []
		return [
			(rule, values[rule.field_id])
			for rule in self.by_document_type.get(document.get("document_type"), [])
			if values.get(rule.field_id) not in (None, "")
			and (not require_tag or not rule.tag_id or rule.tag_id in tags)
		]

	def covers(self, doctype):
		"""True if files of this DocType take part in the import."""
		return any(rule.doctype == doctype for rules in self.by_document_type.values() for rule in rules)


class ImportRule:
	def __init__(self, doctype, source_fieldname, document_type_id, field_id, tag_id):
		self.doctype = doctype
		self.source_fieldname = source_fieldname
		self.document_type_id = document_type_id
		self.field_id = field_id
		self.tag_id = tag_id

	@classmethod
	def resolve(cls, doctype, mapping, tag_id, settings):
		source_fieldname = frappe.db.get_value(
			"SVAN Paperless Custom Field Source",
			{"parent": mapping.name, "source_doctype": doctype},
			"source_fieldname",
		)
		if not source_fieldname:
			return None

		document_type_id = get_paperless_document_type_id_for_document(None, doctype, settings)
		if not document_type_id:
			return None
		return cls(doctype, source_fieldname, int(document_type_id), int(mapping.field_id), tag_id)


def find_business_documents(matches):
	"""(doctype, name) pairs the references point at; at most two per rule are
	looked up, as more than one means "ambiguous" anyway.
	"""
	found = []
	for rule, value in matches:
		for name in frappe.get_all(
			rule.doctype, filters={rule.source_fieldname: value}, pluck="name", limit=2
		):
			if (rule.doctype, name) not in found:
				found.append((rule.doctype, name))
	return found


def reference_value(value):
	"""Surrounding whitespace and line breaks are no part of a reference. A blank one
	must not count as given: MariaDB ignores trailing spaces, so "   " would equal
	every empty source field.
	"""
	return value.strip() if isinstance(value, str) else value


def _reference_mapping(settings):
	"""The reference mapping with its Paperless NGX custom field ID, or None."""
	if not settings.import_reference_mapping:
		return None
	mapping = frappe.db.get_value(
		"SVAN Paperless Custom Field Mapping",
		settings.import_reference_mapping,
		["name", "paperless_custom_field_id", "paperless_custom_field_name", "paperless_custom_field_type"],
		as_dict=True,
	)
	if not mapping:
		return None
	mapping.field_id = mapping.paperless_custom_field_id or ensure_paperless_custom_field_mapping(
		None, mapping, settings
	)
	return mapping if mapping.field_id else None


def _paperless_tag_id(tag, settings):
	return frappe.db.get_value(
		"SVAN Paperless Tag Mapping", {"frappe_tag": tag}, "paperless_tag_id"
	) or ensure_paperless_tag_mapping(None, tag, settings)
