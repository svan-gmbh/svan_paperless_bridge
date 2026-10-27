# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe

from svan_paperless_bridge.correspondent_sync import get_party_display_name, get_party_field
from svan_paperless_bridge.utils.field_options import is_value_field

SETTINGS_FIELDS = (
	"sync_config_version",
	"svan_default_correspondent_id",
	"delete_physical_file",
	"enforce_document_permissions",
)
# Personal data (user IDs) and Frappe's own bookkeeping columns.
EXCLUDED_FILE_FIELDS = {"owner", "modified_by", "_user_tags", "_comments", "_assign", "_liked_by"}
WHITELIST_ROW_FIELDS = (
	"paperless_document_type_name",
	"paperless_document_type_id",
	"party_fieldname",
	"party_rename_supported",
)


def collect_log_context(file_name, settings):
	"""Snapshot for a sync log entry: settings, the file and the metadata of its
	business document, each with the mapping state of that moment.

	Read-only on purpose: the resolvers used for the upload create missing tags,
	correspondents and custom fields in Paperless NGX and log themselves.
	"""
	settings = settings or frappe.get_cached_doc("SVAN Paperless Settings")
	context = {"settings": {field: settings.get(field) for field in SETTINGS_FIELDS}}
	if file_name:
		context.update(FileContext(file_name).collect())
	return context


class FileContext:
	def __init__(self, file_name):
		self.file_name = file_name

	def collect(self):
		file_row = frappe.db.get_value("File", self.file_name, "*", as_dict=True)
		if not file_row:
			return {"file": {"name": self.file_name, "exists": False}}

		context = {"file": {k: v for k, v in file_row.items() if k not in EXCLUDED_FILE_FIELDS}}
		if file_row.attached_to_doctype and file_row.attached_to_name:
			context["business_document"] = BusinessDocumentContext(
				file_row.attached_to_doctype, file_row.attached_to_name
			).collect()
		return context


class BusinessDocumentContext:
	def __init__(self, doctype, name):
		self.doctype = doctype
		self.name = name

	def collect(self):
		context = {"doctype": self.doctype, "name": self.name}
		if not frappe.db.exists(self.doctype, self.name):
			return {**context, "exists": False}

		return {
			**context,
			"exists": True,
			"whitelist_row": self._whitelist_row(),
			"tags": self._tags(),
			"party": self._party(),
			"custom_fields": self._custom_fields(),
		}

	def _whitelist_row(self):
		return frappe.db.get_value(
			"SVAN Paperless Whitelisted DocType",
			{"document_type": self.doctype},
			WHITELIST_ROW_FIELDS,
			as_dict=True,
		)

	def _tags(self):
		tags = frappe.get_all(
			"Tag Link",
			filters={"document_type": self.doctype, "document_name": self.name},
			pluck="tag",
		)
		mappings = {
			row.frappe_tag: row
			for row in frappe.get_all(
				"SVAN Paperless Tag Mapping",
				filters={"frappe_tag": ["in", tags or [""]]},
				fields=["frappe_tag", "paperless_tag_id", "bidirectional"],
			)
		}
		return [
			{
				"tag": tag,
				"paperless_tag_id": mappings[tag].paperless_tag_id if tag in mappings else None,
				"bidirectional": mappings[tag].bidirectional if tag in mappings else None,
			}
			for tag in tags
		]

	def _party(self):
		fieldname, type_fieldname, party_type = get_party_field(self.doctype)
		if not fieldname:
			return None

		if type_fieldname:
			party_type, party = frappe.db.get_value(self.doctype, self.name, [type_fieldname, fieldname])
		else:
			party = frappe.db.get_value(self.doctype, self.name, fieldname)

		party_context = {"fieldname": fieldname, "party_type": party_type, "party": party}
		if not (party_type and party):
			return party_context

		mapping = frappe.db.get_value(
			"SVAN Paperless Correspondent Mapping",
			{"frappe_party_type": party_type, "frappe_party": party},
			["paperless_correspondent_id", "bidirectional"],
			as_dict=True,
		)
		return {
			**party_context,
			"display_name": get_party_display_name(party_type, party),
			"paperless_correspondent_id": mapping.paperless_correspondent_id if mapping else None,
			"bidirectional": mapping.bidirectional if mapping else None,
		}

	def _custom_fields(self):
		sources = frappe.get_all(
			"SVAN Paperless Custom Field Source",
			filters={"source_doctype": self.doctype, "parenttype": "SVAN Paperless Custom Field Mapping"},
			fields=["parent", "source_fieldname"],
		)
		if not sources:
			return []

		mappings = {
			row.name: row
			for row in frappe.get_all(
				"SVAN Paperless Custom Field Mapping",
				filters={"name": ["in", list({source.parent for source in sources})]},
				fields=[
					"name",
					"paperless_custom_field_id",
					"paperless_custom_field_name",
					"paperless_custom_field_type",
					"bidirectional",
				],
			)
		}
		values = self._field_values({source.source_fieldname for source in sources})
		return [
			{
				"mapping": source.parent,
				**{k: v for k, v in mappings.get(source.parent, {}).items() if k != "name"},
				"source_fieldname": source.source_fieldname,
				"value": values.get(source.source_fieldname),
			}
			for source in sources
		]

	def _field_values(self, fieldnames):
		# A source field dropped from the DocType must not fail the query.
		meta = frappe.get_meta(self.doctype)
		existing = [fieldname for fieldname in fieldnames if is_value_field(meta, fieldname)]
		if not existing:
			return {}
		return frappe.db.get_value(self.doctype, self.name, existing, as_dict=True) or {}
