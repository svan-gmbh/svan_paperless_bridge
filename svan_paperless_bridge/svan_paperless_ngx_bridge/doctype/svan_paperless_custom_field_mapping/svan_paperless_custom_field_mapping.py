# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
from frappe import _
from frappe.model.document import Document

from svan_paperless_bridge.svan_paperless_ngx_bridge.doctype.svan_paperless_custom_field_source.svan_paperless_custom_field_source import (
	validate_source_doctype,
	validate_source_fieldname,
)
from svan_paperless_bridge.svan_paperless_ngx_bridge.doctype.svan_paperless_settings.svan_paperless_settings import (
	is_import_reference,
	show_reference_not_bidirectional_alert,
)


class SVANPaperlessCustomFieldMapping(Document):
	def validate(self):
		self.validate_unique_paperless_custom_field_id()
		self.validate_field_sources()
		self.disable_bidirectional_for_link_targets()
		self.disable_bidirectional_for_import_reference()
		self.update_field_sources_count()

	def on_update(self):
		self.invalidate_watched_doctype_map_cache()
		self.apply_reference_source_change()

	def on_trash(self):
		self.invalidate_watched_doctype_map_cache()

	def invalidate_watched_doctype_map_cache(self):
		"""Discard the cached watched-DocType map of change_watch.py.

		A new, changed or deleted field_sources row (source DocType + source fieldname)
		changes which DocTypes/fields change_watch.py has to watch for custom field
		changes. Imported lazily to avoid a circular import.
		"""
		from svan_paperless_bridge.change_watch import invalidate_watched_doctype_map_cache

		invalidate_watched_doctype_map_cache()

	def apply_reference_source_change(self):
		"""Changed sources of the import reference mean other reference values in
		Paperless NGX and other DocTypes to import into (see reference_config.py). A
		rename of the custom field alone only renames it there.
		"""
		from svan_paperless_bridge.reference_config import (
			flag_files_for_reference_backfill,
			restart_import_scan,
			sources_changed_on_mapping,
		)

		if sources_changed_on_mapping(self):
			flag_files_for_reference_backfill(self.name)
			restart_import_scan()

	def validate_field_sources(self):
		"""Validate source_fieldname of every source field row and allow each source
		DocType only once - with two fields of one DocType it would be ambiguous which
		one provides the value (upload) or receives it (webhook).

		Frappe does not call validate() of a child table row automatically (only that
		of the parent document) - so it is called explicitly per row here. See
		svan_paperless_custom_field_source.validate_source_fieldname() for the actual
		check and its rationale.
		"""
		source_doctypes = set()
		for row in self.field_sources:
			validate_source_doctype(row)
			validate_source_fieldname(row)
			if row.source_doctype in source_doctypes:
				frappe.throw(
					_(
						"Source DocType {0} is listed more than once - only one source field per DocType is allowed."
					).format(row.source_doctype)
				)
			source_doctypes.add(row.source_doctype)

	def disable_bidirectional_for_link_targets(self):
		"""Set "Bidirectional" to 0 if ANY source row targets a link field - on every
		save, whether edited manually or created automatically. Deliberately no error:
		the field defaults to 1, so a refusal would fail the common case. The user is
		told about the override unless the caller sets flags.silent_bidirectional_override
		(automatic save).

		A link field points at a master record of its own that is potentially shared by
		several other documents - syncing back from Paperless NGX would have to either
		rename that linked record or relink the reference, both too invasive an
		intervention on the strength of an external change alone. Plain value fields
		(Data/Date/Currency/...) are unaffected and can still be synced bidirectionally.
		"""
		if not self.bidirectional:
			return

		for row in self.field_sources:
			if not row.source_doctype or not row.source_fieldname:
				continue
			field = frappe.get_meta(row.source_doctype).get_field(row.source_fieldname)
			if field and field.fieldtype == "Link":
				self.bidirectional = 0
				if not self.flags.silent_bidirectional_override:
					frappe.msgprint(
						_(
							'\'Bidirectional\' was switched off - source field "{0}" on "{1}" is a link field '
							"and syncing back would otherwise have to rename or relink a foreign master record."
						).format(row.source_fieldname, row.source_doctype),
						indicator="orange",
					)
				return

	def disable_bidirectional_for_import_reference(self):
		"""The reference of the file import identifies the business document and is
		never synced back.
		"""
		if self.bidirectional and is_import_reference(self.name):
			self.bidirectional = 0
			show_reference_not_bidirectional_alert()

	def update_field_sources_count(self):
		"""Keep field_sources_count in sync with the number of rows in
		"field_sources" - a pure display/list view field (see its description in the
		DocType JSON), which is why it is calculated here rather than in field_sources
		itself.
		"""
		self.field_sources_count = len(self.field_sources)

	def validate_unique_paperless_custom_field_id(self):
		"""Check the uniqueness of paperless_custom_field_id manually.

		A database unique index is NOT possible here: unlike for tags (SVAN Paperless
		Tag Mapping), a row may deliberately exist without an ID (the user creates a
		custom field that does not exist in Paperless NGX yet, see custom_field_sync.py)
		- several rows with an empty/0 ID therefore have to be possible at the same
		time.
		"""
		if not self.paperless_custom_field_id:
			return

		duplicate = frappe.db.get_value(
			"SVAN Paperless Custom Field Mapping",
			{"paperless_custom_field_id": self.paperless_custom_field_id, "name": ["!=", self.name]},
		)
		if duplicate:
			frappe.throw(
				_("A mapping for Paperless custom field ID {0} already exists ({1}).").format(
					self.paperless_custom_field_id, duplicate
				)
			)
