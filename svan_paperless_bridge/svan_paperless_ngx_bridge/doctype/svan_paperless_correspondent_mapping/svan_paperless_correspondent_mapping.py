# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
from frappe import _
from frappe.model.document import Document


class SVANPaperlessCorrespondentMapping(Document):
	def validate(self):
		self.validate_unique_frappe_party()
		self.disable_bidirectional_if_rename_unsupported()

	def on_update(self):
		"""A new party type may be a target of a Dynamic Link party field (see
		correspondent_sync.get_possible_party_types()), so the watched-DocType map is
		rebuilt when a row is created or its party type changes.
		"""
		if not self.has_value_changed("frappe_party_type"):
			return

		from svan_paperless_bridge.change_watch import invalidate_watched_doctype_map_cache

		invalidate_watched_doctype_map_cache()

	def disable_bidirectional_if_rename_unsupported(self):
		"""Set "Bidirectional" to 0 if renaming the linked party master record cannot
		possibly take effect for frappe_party_type (see
		correspondent_sync.can_rename_via_title_field()) - on every save, whether edited
		manually or created automatically. Deliberately no error: an automatic creation
		must not fail, it only overrides the default. The user is told about the override
		unless the caller sets flags.silent_bidirectional_override (automatic creation).

		Example "Contact": its title field full_name is always recalculated by
		Contact.validate() itself from first_name/middle_name/last_name/company_name -
		a display name set by rename_frappe_party_from_paperless() would be discarded
		again within the same doc.save(). Reproduced live: a rename in Paperless NGX
		("asad" -> "asad2") was logged as "applied successfully" without anything
		actually changing on the Contact.
		"""
		if not self.bidirectional or not self.frappe_party_type:
			return

		from svan_paperless_bridge.correspondent_sync import get_party_rename_supported

		if get_party_rename_supported(self.frappe_party_type):
			return

		self.bidirectional = 0
		if not self.flags.silent_bidirectional_override:
			frappe.msgprint(
				_(
					"'Bidirectional' was switched off for {0} - the display name of this party type is "
					"recalculated from several fields on save and therefore cannot be written back."
				).format(self.frappe_party_type),
				indicator="orange",
			)

	def validate_unique_frappe_party(self):
		"""Check the uniqueness of (frappe_party_type, frappe_party) manually.

		A database unique index is not possible here (a composite key over two fields,
		unlike tags - SVAN Paperless Tag Mapping - with only one mapping field) -
		analogous to
		SVANPaperlessCustomFieldMapping.validate_unique_paperless_custom_field_id().

		Raised as UniqueValidationError - exactly like a database unique constraint
		would - so that correspondent_sync._save_mapping_doc() and its callers treat a
		row created concurrently between their lookup and this check as the same race.
		"""
		if not self.frappe_party_type or not self.frappe_party:
			return

		duplicate = frappe.db.get_value(
			"SVAN Paperless Correspondent Mapping",
			{
				"frappe_party_type": self.frappe_party_type,
				"frappe_party": self.frappe_party,
				"name": ["!=", self.name],
			},
		)
		if duplicate:
			frappe.throw(
				_("A mapping for {0} {1} already exists ({2}).").format(
					self.frappe_party_type, self.frappe_party, duplicate
				),
				exc=frappe.UniqueValidationError,
			)
