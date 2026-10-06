# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
from frappe.model import no_value_fields


def get_value_field_options(doctype, txt=None, fieldtypes=None):
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
