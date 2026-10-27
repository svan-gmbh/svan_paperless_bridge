# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
from frappe import _
from frappe.model import no_value_fields

# The ID of a document is no field in its meta, but a valid source (e.g. the order number).
ID_FIELDNAME = "name"


def get_value_field_options(doctype, txt=None, fieldtypes=None, include_id=False):
	"""Return suggestions for an autocomplete field that expects a technical
	fieldname on a DocType the user picked beforehand - e.g. "Party Fieldname"
	(svan_paperless_whitelisted_doctype.py) or "Source Fieldname"
	(svan_paperless_custom_field_source.py). Generic for any DocType, with no
	per-DocType field list hardcoded.

	Without fieldtypes, every value field qualifies - structural/layout fields
	without a value of their own (Frappe's standard constant
	frappe.model.no_value_fields, among them Section Break, Table, Button) are
	always excluded. With fieldtypes (e.g. Link and Dynamic Link for "Party
	Fieldname") only fields of one of those types are suggested.

	Returns a list of {"value": fieldname, "label": "Label (fieldname)"},
	optionally filtered by txt (fieldname or label contains txt, case
	insensitive - matching the Awesomplete behaviour of the autocomplete input).
	Without a doctype (the row has no DocType selected yet) or for an unknown
	DocType, an empty list is returned.
	"""
	if not doctype or not frappe.db.exists("DocType", doctype):
		return []

	txt = (txt or "").strip().lower()
	options = []
	id_label = _("ID")
	if include_id and (not txt or txt in ID_FIELDNAME or txt in id_label.lower()):
		options.append({"value": ID_FIELDNAME, "label": f"{id_label} ({ID_FIELDNAME})"})
	for field in frappe.get_meta(doctype).fields:
		if field.fieldtype in no_value_fields:
			continue
		if fieldtypes and field.fieldtype not in fieldtypes:
			continue
		if txt and txt not in field.fieldname.lower() and txt not in (field.label or "").lower():
			continue
		options.append(
			{"value": field.fieldname, "label": f"{field.label or field.fieldname} ({field.fieldname})"}
		)

	return options


def is_value_field(meta, fieldname):
	"""True for the ID and for fields holding a value of their own."""
	if fieldname == ID_FIELDNAME:
		return True
	field = meta.get_field(fieldname)
	return bool(field) and field.fieldtype not in no_value_fields
