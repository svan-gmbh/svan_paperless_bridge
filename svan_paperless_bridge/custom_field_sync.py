# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
import requests

from svan_paperless_bridge.utils.sync_log import log_sync_event

DEFAULT_PAGE_SIZE = 100

# "select" and "documentlink" need extra configuration (choices via "extra_data") that
# this mapping does not cover. The form no longer offers them; this guards values set via
# API or import.
UNSUPPORTED_AUTO_CREATE_DATA_TYPES = {"select", "documentlink"}


def fetch_paperless_custom_fields(settings, name=None):
	"""Fetch custom fields from Paperless NGX (GET /custom_fields/).

	Follows the "next" link of DRF's standard pagination until all pages have been
	retrieved - analogous to tag_sync.fetch_paperless_tags(). Raises on a
	connection, HTTP or JSON error - the caller (create_missing_custom_field) is
	responsible for logging and error handling.

	name (optional): a server-side name filter (?name=), analogous to
	SVANPaperlessSettings.test_paperless_connection() - for
	create_missing_custom_field() it reduces the number of pages fetched from "all
	custom fields" to "those matching this name", instead of paginating the full
	list for every custom field to be created. As Paperless NGX presumably
	implements the filter as "icontains" rather than "iexact", the caller still has
	to compare exactly itself.

	Returns a list of dicts {"id": int, "name": str, "data_type": str}. Entries
	without an "id" or without a (non-empty) "name" are skipped.
	"""
	# get_password() also sees a token that is not saved yet.
	token = settings.get_password("api_token", raise_exception=False)
	headers = {"Authorization": f"Token {token}"}
	timeout = settings.api_call_timeout or 30

	url = settings.api_url.rstrip("/") + "/custom_fields/"
	params = {"page_size": DEFAULT_PAGE_SIZE}
	if name:
		params["name"] = name

	custom_fields = []
	while url:
		response = requests.get(url, headers=headers, params=params, timeout=timeout, verify=True)
		response.raise_for_status()
		payload = response.json()

		for entry in payload.get("results", []):
			if entry.get("id") is not None and entry.get("name"):
				custom_fields.append(
					{"id": entry["id"], "name": entry["name"], "data_type": entry.get("data_type") or ""}
				)

		url = payload.get("next")
		params = None

	return custom_fields


def create_missing_custom_field(name, data_type, settings):
	"""Create a custom field with the given name/data type in Paperless NGX.

	Only called when ensure_paperless_custom_field_mapping() has a value to
	transfer for a mapping row without a paperless_custom_field_id. Raises
	frappe.ValidationError if data_type is one of the unsupported types (see
	UNSUPPORTED_AUTO_CREATE_DATA_TYPES).

	Checks case-insensitively beforehand via fetch_paperless_custom_fields()
	whether a custom field of that name already exists (analogous to
	tag_sync.create_missing_tag()) - so no duplicate custom field is created.
	Otherwise it is created via POST /custom_fields/ ("name" and "data_type" are
	the mandatory fields according to CustomFieldSerializer).

	Returns the (found or newly created) Paperless NGX custom field ID. Raises on a
	connection, HTTP or JSON error - the caller is responsible for logging and the
	fallback.
	"""
	if data_type in UNSUPPORTED_AUTO_CREATE_DATA_TYPES:
		frappe.throw(
			frappe._(
				'Automatic creation is not supported for data type "{0}" (it requires additional '
				"configuration in Paperless NGX)."
			).format(data_type)
		)

	for existing in fetch_paperless_custom_fields(settings, name=name):
		if existing["name"].strip().lower() == name.strip().lower():
			return existing["id"]

	token = settings.get_password("api_token", raise_exception=False)
	headers = {"Authorization": f"Token {token}"}
	url = settings.api_url.rstrip("/") + "/custom_fields/"

	response = requests.post(
		url,
		headers=headers,
		data={"name": name, "data_type": data_type},
		timeout=settings.api_call_timeout or 30,
		verify=True,
	)
	response.raise_for_status()
	return response.json()["id"]


def ensure_paperless_custom_field_mapping(file_name, mapping, settings):
	"""Create a custom field in Paperless NGX automatically for a mapping row
	without a paperless_custom_field_id and fill in the new ID.

	Called from get_paperless_custom_field_values_for_document() as soon as a
	relevant mapping row has no paperless_custom_field_id yet - analogous to
	tag_sync.ensure_paperless_tag_mapping() and
	correspondent_sync.auto_create_correspondent_for_party(). If the name or type
	is missing on the mapping row, or creating it fails (network/API error, or an
	unsupported field type - see UNSUPPORTED_AUTO_CREATE_DATA_TYPES), that is
	logged and None is returned - a missing custom field mapping must never prevent
	a file upload.

	Updates the ID through a full doc.save() rather than frappe.db.set_value():
	only that way does
	SVANPaperlessCustomFieldMapping.validate_unique_paperless_custom_field_id() run
	as well - without it, two mapping rows could unnoticeably point at the same
	Paperless custom field ID (e.g. after a rename in Paperless NGX), which would
	make get_paperless_custom_field_values_for_document() silently overwrite one of
	the two values on upload (the ID is the dict key there). A ValidationError
	triggered by this is handled like any other failure: logged, returning None.

	Returns the new/found paperless_custom_field_id, or None.
	"""
	if not mapping.paperless_custom_field_name or not mapping.paperless_custom_field_type:
		log_sync_event(
			file_name,
			settings,
			"Warn",
			"ensure_paperless_custom_field_mapping",
			title="Custom field without name/type cannot be created automatically",
			details=f"SVAN Paperless Custom Field Mapping: {mapping.name}",
		)
		return None

	try:
		paperless_custom_field_id = create_missing_custom_field(
			mapping.paperless_custom_field_name, mapping.paperless_custom_field_type, settings
		)
		mapping_doc = frappe.get_doc("SVAN Paperless Custom Field Mapping", mapping.name)
		mapping_doc.paperless_custom_field_id = paperless_custom_field_id
		mapping_doc.last_synced = frappe.utils.now_datetime()
		# A legacy bidirectional+link row is switched off by this save (see
		# SVANPaperlessCustomFieldMapping.disable_bidirectional_for_link_targets()) -
		# silently, because the user did not edit it.
		mapping_doc.flags.silent_bidirectional_override = True
		mapping_doc.save(ignore_permissions=True)
	except Exception:
		log_sync_event(
			file_name,
			settings,
			"Error",
			"ensure_paperless_custom_field_mapping",
			title="Automatic creation of the custom field failed",
			details=(
				f"SVAN Paperless Custom Field Mapping: {mapping.name} "
				f"({mapping.paperless_custom_field_name}, {mapping.paperless_custom_field_type})\n\n"
				f"{frappe.get_traceback()}"
			),
		)
		return None

	log_sync_event(
		file_name,
		settings,
		"Info",
		"ensure_paperless_custom_field_mapping",
		title="Custom field created automatically",
		details=(
			f"SVAN Paperless Custom Field Mapping: {mapping.name} "
			f"({mapping.paperless_custom_field_name}, {mapping.paperless_custom_field_type})\n"
			f"New Paperless NGX custom field ID: {paperless_custom_field_id}"
		),
	)

	return paperless_custom_field_id


def get_paperless_custom_field_values_for_document(
	file_name, attached_to_doctype, attached_to_name, settings
):
	"""Resolve the configured source fields of ONE business document into
	Paperless NGX custom field values - generic for any
	attached_to_doctype/attached_to_name pair (called from
	paperless_sync._resolve_upload_metadata()).

	Reads all "SVAN Paperless Custom Field Mapping" rows whose "field_sources"
	contain a row with source_doctype == attached_to_doctype, loads the business
	document once and reads the configured field values from it. Empty/None values
	are skipped and only logged as a warning - a missing or empty field must never
	prevent a file upload, analogous to
	tag_sync.get_paperless_tag_ids_for_document().

	If a relevant mapping row has no paperless_custom_field_id yet (the user
	created it manually with the desired name/type, but it does not exist in
	Paperless NGX yet), the custom field is created there automatically through
	ensure_paperless_custom_field_mapping() and the new ID is filled in on the
	mapping row - so, as with tags and correspondents, it is checked/created
	lazily during the file upload instead of periodically in advance. If creating
	it fails, the field is skipped and logged as before.

	Returns a (possibly empty) dict {paperless_custom_field_id: value}.
	"""
	if not attached_to_doctype or not attached_to_name:
		return {}

	source_rows = frappe.get_all(
		"SVAN Paperless Custom Field Source",
		filters={"source_doctype": attached_to_doctype, "parenttype": "SVAN Paperless Custom Field Mapping"},
		fields=["parent", "source_fieldname"],
	)
	if not source_rows:
		return {}

	parent_names = list({row.parent for row in source_rows})
	mapping_rows = frappe.get_all(
		"SVAN Paperless Custom Field Mapping",
		filters={"name": ["in", parent_names]},
		fields=[
			"name",
			"paperless_custom_field_id",
			"paperless_custom_field_name",
			"paperless_custom_field_type",
		],
	)
	mapping_by_parent = {row.name: row for row in mapping_rows}

	# A source field removed or renamed after the mapping was saved must not fail the query
	# ("Unknown column") and block the upload of every file of this DocType.
	meta = frappe.get_meta(attached_to_doctype)
	all_fieldnames = list({row.source_fieldname for row in source_rows})
	fieldnames = [f for f in all_fieldnames if meta.get_field(f)]

	# A dropped field counts as an empty value below.
	business_doc_values = (
		frappe.db.get_value(attached_to_doctype, attached_to_name, fieldnames, as_dict=True)
		if fieldnames
		else {}
	) or {}

	custom_field_values = {}
	empty_fields = []
	for row in source_rows:
		mapping = mapping_by_parent.get(row.parent)
		value = business_doc_values.get(row.source_fieldname)
		if not mapping or value in (None, ""):
			empty_fields.append(f"{attached_to_doctype}.{row.source_fieldname}")
			continue

		paperless_custom_field_id = mapping.paperless_custom_field_id
		if not paperless_custom_field_id:
			paperless_custom_field_id = ensure_paperless_custom_field_mapping(file_name, mapping, settings)
			if not paperless_custom_field_id:
				empty_fields.append(f"{attached_to_doctype}.{row.source_fieldname}")
				continue

		custom_field_values[paperless_custom_field_id] = value

	if empty_fields:
		log_sync_event(
			file_name,
			settings,
			"Warn",
			"get_paperless_custom_field_values_for_document",
			title="Source fields without a value skipped",
			details=(
				f"Document: {attached_to_doctype} {attached_to_name}\n"
				f"Empty/unmappable source fields: {', '.join(empty_fields)}"
			),
		)

	return custom_field_values


def get_owned_paperless_custom_field_ids_for_document(attached_to_doctype):
	"""Return the set of all Paperless NGX custom field IDs that are configured
	for attached_to_doctype through a "SVAN Paperless Custom Field Source" row AND
	whose mapping row already has an ID (i.e. the fields we "own" for this
	document).

	Needed by paperless_sync.push_metadata_update() in order to overwrite/clear
	ONLY those IDs in a `custom_fields` PATCH and leave every other custom field
	value of the Paperless NGX document that we do not manage untouched (see the
	docstring there) - without this distinction, simply replacing `custom_fields`
	would delete every value not sent along explicitly, reproduced live as a risk
	of data loss.
	"""
	source_rows = frappe.get_all(
		"SVAN Paperless Custom Field Source",
		filters={"source_doctype": attached_to_doctype, "parenttype": "SVAN Paperless Custom Field Mapping"},
		pluck="parent",
	)
	if not source_rows:
		return set()

	ids = frappe.get_all(
		"SVAN Paperless Custom Field Mapping",
		filters={"name": ["in", list(set(source_rows))], "paperless_custom_field_id": ["!=", 0]},
		pluck="paperless_custom_field_id",
	)
	return set(ids)


def repair_stale_custom_field_id(file_name, invalid_paperless_custom_field_id, settings):
	"""Repair the mapping row of a custom field deleted directly in Paperless NGX -
	analogous to tag_sync.repair_stale_tag_id(),
	correspondent_sync.repair_stale_correspondent_id() and
	document_type_sync.repair_stale_document_type_id().

	Called from paperless_sync.query_uuid() when Paperless NGX rejects the upload
	with "Custom field with id ... does not exist" for the "custom_fields" field.

	For the field types in UNSUPPORTED_AUTO_CREATE_DATA_TYPES (select,
	documentlink) the repair fails like any automatic creation (see
	create_missing_custom_field()) - those rows still depend on an ID entered
	manually in Paperless NGX.

	Returns True if a row was found and repaired (the caller should then retry the
	upload with a freshly resolved custom field ID), otherwise False.
	"""
	mapping = frappe.db.get_value(
		"SVAN Paperless Custom Field Mapping",
		{"paperless_custom_field_id": invalid_paperless_custom_field_id},
		["name", "paperless_custom_field_name", "paperless_custom_field_type"],
		as_dict=True,
	)
	if not mapping:
		return False

	new_id = ensure_paperless_custom_field_mapping(file_name, mapping, settings)
	return bool(new_id)
