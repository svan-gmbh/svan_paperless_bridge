"""
Central, current definition of the custom fields svan_paperless_bridge creates on
the core DocTypes "File" and "User".

Called from install.after_install(). Changes to this definition only reach an
existing site via bench migrate through a patch that calls
create_svan_paperless_custom_fields(); without one, run it with bench execute.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.custom.doctype.property_setter.property_setter import delete_property_setter

USER_DOCTYPE = "User"
# The SVAN marker's tooltip names the app through it; Frappe also deletes the fields with the app.
MODULE = "SVAN Paperless NGX Bridge"


def get_custom_fields():
	return {
		"File": [
			{
				"fieldname": "svan_paperless_document_id",
				"label": "Document ID",
				"fieldtype": "Data",
				"insert_after": "file_type",
				"hidden": 1,
				"read_only": 1,
				"no_copy": 1,
				# Every webhook, comparison and version step looks files up by it.
				"search_index": 1,
				"description": "SVAN Paperless NGX Bridge: ",
			},
			{
				"fieldname": "svan_paperless_version_id",
				"label": "Version ID",
				"fieldtype": "Data",
				"insert_after": "svan_paperless_document_id",
				"hidden": 1,
				"read_only": 1,
				"no_copy": 1,
				"description": (
					"SVAN Paperless NGX Bridge: "
					"Version of the Paperless NGX document holding this file's content; equals the "
					"document ID for the original."
				),
			},
			{
				"fieldname": "svan_paperless_uuid",
				"label": "UUID",
				"fieldtype": "Data",
				"insert_after": "svan_paperless_version_id",
				"hidden": 1,
				"read_only": 1,
				"no_copy": 1,
				"description": (
					"SVAN Paperless NGX Bridge: "
					"UUID returned when the file is handed over to the Paperless NGX API."
				),
			},
			{
				"fieldname": "svan_paperless_sync_status",
				"label": "Sync Status",
				"fieldtype": "Select",
				"options": "\npending\nsyncing\nsynced\nbroken\ndeleted",
				"insert_after": "svan_paperless_uuid",
				"read_only": 1,
				"no_copy": 1,
				"default": "",
				"in_list_view": 1,
				"description": (
					"SVAN Paperless NGX Bridge: "
					'Sync status with Paperless NGX: "" (empty) = no sync intended '
					"(DocType not whitelisted, or already uploaded to Dropbox/Google "
					'Drive), "pending" = waiting (will be synced on the next scheduled '
					'run), "syncing" = sync in progress, "synced" = synced, "broken" = '
					'failed (sync unsuccessful), "deleted" = the Paperless NGX document '
					"was already removed while deleting this file (see "
					'paperless_delete.py) - terminal, and like "broken" never considered '
					"for another sync."
				),
			},
			{
				"fieldname": "svan_paperless_config_version_checked",
				"label": "Sync Config Version Checked",
				"fieldtype": "Int",
				"insert_after": "svan_paperless_sync_status",
				"hidden": 1,
				"read_only": 1,
				"no_copy": 1,
				"default": "0",
				"description": (
					"SVAN Paperless NGX Bridge: "
					"Configuration state (SVAN Paperless Settings.sync_config_version) "
					"this file was last evaluated against for sync relevance."
				),
			},
			{
				"fieldname": "svan_source",
				"label": "Source",
				"fieldtype": "Data",
				"insert_after": "svan_paperless_config_version_checked",
				"hidden": 1,
				"read_only": 1,
				"no_copy": 1,
				"default": "",
				"description": (
					"SVAN Paperless NGX Bridge: "
					'What triggered this file record, e.g. "erpnext" - set automatically '
					"on insert if still empty (see file_events.set_default_source)."
				),
			},
			{
				"fieldname": "svan_paperless_needs_update",
				"label": "Metadata Update Pending",
				"fieldtype": "Check",
				"insert_after": "svan_source",
				"hidden": 1,
				"read_only": 1,
				"no_copy": 1,
				"default": "0",
				"description": (
					"SVAN Paperless NGX Bridge: "
					"Set when tags, the correspondent or a mapped custom field of the "
					"business document, or the file name change after the upload (files in sync status "
					'"syncing" or "synced"); cleared once the scheduled job has pushed '
					"the metadata to Paperless NGX."
				),
			},
			{
				"fieldname": "svan_paperless_synced_name",
				"label": "Synced File Name",
				"fieldtype": "Data",
				"insert_after": "svan_paperless_needs_update",
				"hidden": 1,
				"read_only": 1,
				"no_copy": 1,
				"description": (
					"SVAN Paperless NGX Bridge: "
					"File name last matched with Paperless NGX; empty until the first match."
				),
			},
			{
				"fieldname": "svan_paperless_synced_title",
				"label": "Synced Title",
				"fieldtype": "Data",
				"insert_after": "svan_paperless_synced_name",
				"hidden": 1,
				"read_only": 1,
				"no_copy": 1,
				"description": (
					"SVAN Paperless NGX Bridge: "
					"Title of the Paperless NGX document at the last match (original only)."
				),
			},
			{
				"fieldname": "svan_paperless_synced_label",
				"label": "Synced Version Label",
				"fieldtype": "Data",
				"insert_after": "svan_paperless_synced_title",
				"hidden": 1,
				"read_only": 1,
				"no_copy": 1,
				"description": (
					"SVAN Paperless NGX Bridge: "
					"Label of this file's version in Paperless NGX at the last match."
				),
			},
		],
		USER_DOCTYPE: [
			{
				"fieldname": "svan_paperless_ngx_section",
				"label": "SVAN Paperless NGX",
				"fieldtype": "Section Break",
				"insert_after": "api_secret",
				"collapsible": 1,
			},
			{
				"fieldname": "svan_paperless_api_token",
				"label": "API Token",
				"fieldtype": "Password",
				"insert_after": "svan_paperless_ngx_section",
				"description": (
					"Required to check access against the permission system of "
					"Paperless NGX. In Paperless NGX it can be found - or created "
					'anew - under your user name (top right) → "My Profile" → '
					'"API Auth Token" (copy button).'
				),
			},
		],
	}


def create_svan_paperless_custom_fields():
	"""Create/update all svan_paperless_bridge custom fields.

	Idempotent (create_custom_fields(..., update=True)) - safe both on a fresh
	installation (after_install) and when run again on an existing site.
	"""
	custom_fields = get_custom_fields()
	for fields in custom_fields.values():
		for field in fields:
			field["module"] = MODULE
	create_custom_fields(custom_fields, update=True)
	fix_user_field_order()


def fix_user_field_order():
	"""Force the position of svan_paperless_ngx_section/-api_token directly after
	"api_secret" via an explicit field_order property setter - the same technique
	Frappe's "Customize Form" uses when fields are reordered by drag & drop (see
	CustomizeForm.set_property_setter_for_field_order() in
	frappe/custom/doctype/customize_form/customize_form.py).

	Needed because of a limitation in Frappe's own insert_after positioning for new
	custom fields (Meta.sort_fields() in frappe/model/meta.py): for a new Section
	Break field it searches forward from the insert_after target for the next field
	of the same fieldtype and only appends AFTER it - "Tab Break" is not recognized
	as a stop condition. "api_secret" is the last field of the "API Access" section,
	directly followed by the (hidden) "onboarding_status" and then "connections_tab"
	(a Tab Break) - the search therefore continues across the tab boundary, finds no
	further Section Break in the rest of the form and appends our new field at the
	very end of the form instead of directly after "API Access".
	"""
	meta = frappe.get_meta(USER_DOCTYPE, cached=False)
	order = [df.fieldname for df in meta.fields]

	new_fields = [f["fieldname"] for f in get_custom_fields()[USER_DOCTYPE]]
	for fieldname in new_fields:
		if fieldname in order:
			order.remove(fieldname)

	insert_at = order.index("api_secret") + 1
	order[insert_at:insert_at] = new_fields

	delete_property_setter(USER_DOCTYPE, "field_order")
	frappe.make_property_setter(
		{
			"doctype": USER_DOCTYPE,
			"doctype_or_field": "DocType",
			"property": "field_order",
			"value": frappe.as_json(order),
		},
		is_system_generated=False,
	)
	frappe.clear_cache(doctype=USER_DOCTYPE)
