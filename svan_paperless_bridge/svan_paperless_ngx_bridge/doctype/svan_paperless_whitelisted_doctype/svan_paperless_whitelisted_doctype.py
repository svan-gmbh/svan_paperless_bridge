# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
from frappe import _
from frappe.model import no_value_fields
from frappe.model.document import Document

from svan_paperless_bridge.utils.field_options import get_value_field_options


class SVANPaperlessWhitelistedDocType(Document):
	pass


def get_default_document_type_label(document_type):
	"""Determine a sensible suggestion for paperless_document_type_name.

	Translates the DocType name into the language resolved for the current request
	(User.language, otherwise System Settings.language) using Frappe's own
	translations - Frappe's core and standard app translations are stored there
	under the (English) DocType name as the source string (e.g. _("Customer")
	already yields "Kunde" in German - no detour via a field label needed, DocTypes
	have no "label" field of their own). If no translation exists (e.g. for a
	project-specific custom DocType), the DocType name itself is returned, so
	paperless_document_type_name is never left empty.

	Only a suggestion: the user can freely overwrite the value afterwards, see the
	field description of paperless_document_type_name in the DocType JSON.
	"""
	if not document_type or not frappe.db.exists("DocType", document_type):
		return document_type or ""

	return _(document_type)


def set_default_document_type_label(row):
	"""Fill row.paperless_document_type_name if it is (still) empty.

	Called from SVANPaperlessSettings.validate_whitelisted_doctypes() for every
	whitelist row - a server-side safety net for rows created without the client
	script (svan_paperless_settings.js, field event "document_type"), e.g. via the
	API or a data import. NEVER overwrites a value that is already present (either
	prefilled automatically or adjusted manually).
	"""
	if row.paperless_document_type_name or not row.document_type:
		return

	row.paperless_document_type_name = get_default_document_type_label(row.document_type)


def resolve_value_field(document_type, fieldname):
	"""Return the DocField object for fieldname on document_type if it is a real
	value field (not a structural/layout field without content of its own, such as
	Section Break or Table) - otherwise None.

	A central check, shared by validate_party_fieldname() (party_fieldname is free
	text, see there) and change_watch._build_watched_doctype_map() (which has to
	apply the same field type check to decide whether party_fieldname is watched on
	the DocType directly or - for a link field - the linked target DocType
	instead).
	"""
	if not fieldname or not document_type:
		return None

	field = frappe.get_meta(document_type).get_field(fieldname)
	if not field or field.fieldtype in no_value_fields:
		return None

	return field


def get_party_type_fieldname(document_type, field):
	"""Return the fieldname that holds the party DocType of a Dynamic Link party
	field (e.g. "quotation_to" for Quotation.party_name), or None if field is no
	usable Dynamic Link. The type field must be a Link to DocType or a Select.
	"""
	if not field or field.fieldtype != "Dynamic Link":
		return None

	type_field = resolve_value_field(document_type, field.options)
	if not type_field:
		return None
	if type_field.fieldtype == "Select" or (
		type_field.fieldtype == "Link" and type_field.options == "DocType"
	):
		return type_field.fieldname
	return None


def validate_party_fieldname(row):
	"""Make sure row.party_fieldname (if set) is a Link or Dynamic Link field on
	row.document_type.

	Not called automatically on save - Frappe does not call validate() of a child
	table row, so SVANPaperlessSettings.validate_whitelisted_doctypes() calls it per
	row. The party is always a master record of its own (Customer/Supplier/Lead/...),
	never the business document itself. A Link field names the party DocType in its
	options; a Dynamic Link (e.g. Quotation.party_name) reads it per document from
	the type field named in its options (Quotation.quotation_to), so one configured
	field covers both cases. correspondent_sync builds column queries from
	party_fieldname, so an invalid value (e.g. a label instead of the fieldname) is
	rejected here instead of causing a database error later. An empty
	party_fieldname is valid and means: no document-specific correspondent.
	"""
	if not row.party_fieldname or not row.document_type:
		return

	if not frappe.db.exists("DocType", row.document_type):
		return

	field = resolve_value_field(row.document_type, row.party_fieldname)
	if not field:
		frappe.throw(
			_(
				"'{0}' is not a valid field on '{1}'. For Party Fieldname, pick the technical "
				"fieldname from the suggestions (not the field label)."
			).format(row.party_fieldname, row.document_type)
		)

	if field.fieldtype == "Link":
		return

	if field.fieldtype != "Dynamic Link":
		frappe.throw(
			_(
				"'{0}' on '{1}' is not a link field. Party Fieldname must link to the party master "
				"record (e.g. Customer/Supplier)."
			).format(row.party_fieldname, row.document_type)
		)

	if not get_party_type_fieldname(row.document_type, field):
		frappe.throw(
			_(
				"The party type of the dynamic link '{0}' on '{1}' is defined by '{2}', which is "
				"neither a link to DocType nor a selection field."
			).format(row.party_fieldname, row.document_type, field.options)
		)


def update_party_rename_supported(row):
	"""Update row.party_rename_supported (see
	correspondent_sync.can_rename_via_title_field()) - recalculated on every save,
	because the in-memory probe is cheap and also picks up validate() logic of the
	party type that changed later.

	Only determinable for a Link field: a Dynamic Link can point at several party
	types, so the flag stays 0 and correspondent_sync.get_party_rename_supported()
	probes the party type of each mapping row itself.

	Like validate_party_fieldname(), it has to be called explicitly from
	SVANPaperlessSettings.validate_whitelisted_doctypes().
	"""
	field = resolve_value_field(row.document_type, row.party_fieldname) if row.party_fieldname else None
	if not field or field.fieldtype != "Link":
		row.party_rename_supported = 0
		return

	from svan_paperless_bridge.correspondent_sync import can_rename_via_title_field

	row.party_rename_supported = 1 if can_rename_via_title_field(field.options) else 0


@frappe.whitelist()
def get_party_field_options(doctype=None, txt=None):
	"""Return suggestions for the "Party Fieldname" autocomplete field in the SVAN
	Paperless Settings (see svan_paperless_settings.js): Link and Dynamic Link
	fields only (see validate_party_fieldname()).
	"""
	return get_value_field_options(doctype, txt, fieldtypes=["Link", "Dynamic Link"])


@frappe.whitelist()
def get_document_type_label_suggestion(document_type=None):
	"""Whitelisted wrapper around get_default_document_type_label() for the client
	script (svan_paperless_settings.js, field event "document_type").

	Returns the suggestion for paperless_document_type_name as soon as the user
	picks a DocType in a whitelist row - the prefilled value can be freely
	overwritten afterwards.
	"""
	return get_default_document_type_label(document_type)
