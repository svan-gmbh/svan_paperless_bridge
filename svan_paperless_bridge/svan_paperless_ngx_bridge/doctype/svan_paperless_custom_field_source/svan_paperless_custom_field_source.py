# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
from frappe import _
from frappe.model import no_value_fields
from frappe.model.document import Document

from svan_paperless_bridge.utils.field_options import get_value_field_options


class SVANPaperlessCustomFieldSource(Document):
	pass


def validate_source_doctype(row):
	"""Reject "File" as a source DocType.

	Since the switch to business objects only, metadata sync no longer considers
	custom field sources on the File document itself - such a row would simply be
	ignored silently by the sync logic (a source_doctype of "File" is no longer
	provided for there). So that this does not happen unnoticed, "File" is
	rejected here on save already, in addition to the client-side filter on the
	link field (see svan_paperless_custom_field_mapping.js).
	"""
	if row.source_doctype == "File":
		frappe.throw(_('"File" cannot be used as a source DocType.'))


def validate_source_fieldname(row):
	"""Make sure row.source_fieldname is a real value field on
	row.source_doctype (not a structural/layout field without content of its own,
	such as Section Break or Table).

	NOT called automatically on save - Frappe does not call the validate() method
	of a child table row, only that of the parent document. It therefore has to be
	called explicitly per row from SVANPaperlessCustomFieldMapping.validate(),
	analogous to svan_paperless_whitelisted_doctype.validate_party_fieldname().

	custom_field_sync.get_paperless_custom_field_values_for_document() builds a raw
	SQL column query from source_fieldname (frappe.db.get_value(source_doctype,
	..., fieldnames)) - an invalid value (a typo, or the field label instead of the
	technical fieldname) would otherwise cause a database error ("Unknown column")
	instead of a clean "field not found" and would permanently block syncing for
	ALL files of the affected source DocType. An empty source_fieldname is already
	prevented by reqd=1.
	"""
	if not row.source_doctype or not row.source_fieldname:
		return

	if not frappe.db.exists("DocType", row.source_doctype):
		return

	field = frappe.get_meta(row.source_doctype).get_field(row.source_fieldname)
	if not field or field.fieldtype in no_value_fields:
		frappe.throw(
			_(
				"'{0}' is not a valid field on '{1}'. For Source Fieldname, use the technical "
				"fieldname (not the field label)."
			).format(row.source_fieldname, row.source_doctype)
		)


@frappe.whitelist()
def get_source_field_options(doctype=None, txt=None):
	"""Return suggestions for the "Source Fieldname" autocomplete field in
	"SVAN Paperless Custom Field Mapping" (see
	svan_paperless_custom_field_mapping.js) - analogous to
	svan_paperless_whitelisted_doctype.get_party_field_options().

	Delegates to the generic
	svan_paperless_bridge.utils.field_options.get_value_field_options() (see there
	for the return format and filter behaviour).
	"""
	return get_value_field_options(doctype, txt)
