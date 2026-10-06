# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
import requests

from svan_paperless_bridge.utils.sync_log import log_sync_event

DEFAULT_PAGE_SIZE = 100


def fetch_paperless_document_types(settings, name=None):
	"""Fetch document types from Paperless NGX (GET /document_types/).

	Follows the "next" link of DRF's standard pagination until all pages have been
	retrieved - analogous to tag_sync.fetch_paperless_tags() and
	correspondent_sync.fetch_paperless_correspondents(). Raises on a connection,
	HTTP or JSON error - the caller (create_missing_document_type) is responsible
	for logging and error handling.

	name (optional): a server-side name filter (?name=), analogous to
	SVANPaperlessSettings.test_paperless_connection() - for
	create_missing_document_type() it reduces the number of pages fetched from "all
	document types" to "those matching this name". As Paperless NGX presumably
	implements the filter as "icontains" rather than "iexact", the caller still has
	to compare exactly itself.

	Returns a list of dicts {"id": int, "name": str}. Entries without an "id" or
	without a (non-empty) "name" are skipped.
	"""
	# get_password() also sees a token that is not saved yet.
	token = settings.get_password("api_token", raise_exception=False)
	headers = {"Authorization": f"Token {token}"}
	timeout = settings.api_call_timeout or 30

	url = settings.api_url.rstrip("/") + "/document_types/"
	params = {"page_size": DEFAULT_PAGE_SIZE}
	if name:
		params["name"] = name

	document_types = []
	while url:
		response = requests.get(url, headers=headers, params=params, timeout=timeout, verify=True)
		response.raise_for_status()
		payload = response.json()

		for entry in payload.get("results", []):
			if entry.get("id") is not None and entry.get("name"):
				document_types.append({"id": entry["id"], "name": entry["name"]})

		# "next" is a complete URL including all query parameters.
		url = payload.get("next")
		params = None

	return document_types


def create_missing_document_type(name, settings):
	"""Create a document type with the given name in Paperless NGX.

	Only called when get_paperless_document_type_id_for_document() found no
	paperless_document_type_id stored on a whitelist row yet.

	Checks case-insensitively beforehand via fetch_paperless_document_types()
	whether a document type of that name already exists (analogous to
	tag_sync.create_missing_tag() and
	correspondent_sync.create_missing_correspondent()) - so no duplicate document
	type is created. Otherwise it is created via POST /document_types/ ("name" is
	the only mandatory field according to DocumentTypeSerializer).

	Returns the (found or newly created) Paperless NGX document type ID. Raises on
	a connection, HTTP or JSON error - the caller is responsible for logging and
	the fallback.
	"""
	for existing in fetch_paperless_document_types(settings, name=name):
		if existing["name"].strip().lower() == name.strip().lower():
			return existing["id"]

	token = settings.get_password("api_token", raise_exception=False)
	headers = {"Authorization": f"Token {token}"}
	url = settings.api_url.rstrip("/") + "/document_types/"

	response = requests.post(
		url, headers=headers, data={"name": name}, timeout=settings.api_call_timeout or 30, verify=True
	)
	response.raise_for_status()
	return response.json()["id"]


def get_paperless_document_type_id_for_document(file_name, attached_to_doctype, settings):
	"""Determine the Paperless NGX document type ID for a business document.

	attached_to_doctype refers to the document the file is attached to (e.g.
	Purchase Invoice) - NOT to the File document itself, see query_uuid() in
	paperless_sync.py.

	Unlike for tags/correspondents/custom fields there is NO separate mapping table
	for this: the mapping lives directly as paperless_document_type_name/-id on the
	respective row of the DocType whitelist ("SVAN Paperless Whitelisted DocType") -
	every synced DocType has exactly one mandatory row there anyway, so an
	additional mapping DocType would be redundant configuration.

	If a paperless_document_type_id is already stored on the row, it is returned
	directly. Otherwise - lazily, only on the first actual file upload for this
	DocType, not already when the settings are saved - a document type of the same
	name is looked up or created in Paperless NGX through
	create_missing_document_type(), and the new ID is stored on the whitelist row.

	Returns None if the DocType is no longer whitelisted, the row has no
	paperless_document_type_name (yet), or creating it fails - a missing document
	type must never prevent a file upload, analogous to
	tag_sync.get_paperless_tag_ids_for_document().
	"""
	if not attached_to_doctype:
		return None

	row = frappe.db.get_value(
		"SVAN Paperless Whitelisted DocType",
		{"document_type": attached_to_doctype},
		["name", "paperless_document_type_id", "paperless_document_type_name"],
		as_dict=True,
	)
	if not row:
		return None

	if row.paperless_document_type_id:
		return row.paperless_document_type_id

	if not row.paperless_document_type_name:
		return None

	try:
		document_type_id = create_missing_document_type(row.paperless_document_type_name, settings)
		frappe.db.set_value(
			"SVAN Paperless Whitelisted DocType",
			row.name,
			"paperless_document_type_id",
			document_type_id,
			update_modified=False,
		)
	except Exception:
		log_sync_event(
			file_name,
			settings,
			"Error",
			"get_paperless_document_type_id_for_document",
			title="Automatic creation of the document type failed",
			details=(
				f"DocType: {attached_to_doctype} (Paperless name: {row.paperless_document_type_name})\n\n"
				f"{frappe.get_traceback()}"
			),
		)
		return None

	log_sync_event(
		file_name,
		settings,
		"Info",
		"get_paperless_document_type_id_for_document",
		title="Document type created automatically",
		details=(
			f"DocType: {attached_to_doctype}\n"
			f"Paperless NGX document type: {row.paperless_document_type_name} (ID {document_type_id})"
		),
	)

	return document_type_id


def repair_stale_document_type_id(file_name, invalid_paperless_document_type_id, settings):
	"""Repair the whitelist row of a document type deleted directly in Paperless
	NGX - analogous to tag_sync.repair_stale_tag_id() and
	correspondent_sync.repair_stale_correspondent_id().

	Called from paperless_sync.query_uuid() when Paperless NGX rejects the upload
	with "Invalid pk ... - object does not exist" for the "document_type" field.
	Unlike for tags/correspondents there is no separate mapping table here (see
	get_paperless_document_type_id_for_document()) - the affected whitelist row is
	found directly by its previous paperless_document_type_id and updated in place.

	Returns True if a row was found and repaired (the caller should then retry the
	upload with a freshly resolved document type ID), otherwise False.
	"""
	row = frappe.db.get_value(
		"SVAN Paperless Whitelisted DocType",
		{"paperless_document_type_id": invalid_paperless_document_type_id},
		["name", "paperless_document_type_name"],
		as_dict=True,
	)
	if not row or not row.paperless_document_type_name:
		return False

	try:
		document_type_id = create_missing_document_type(row.paperless_document_type_name, settings)
	except Exception:
		log_sync_event(
			file_name,
			settings,
			"Error",
			"repair_stale_document_type_id",
			title="Repairing the document type failed",
			details=(
				f"DocType whitelist row: {row.name} (Paperless name: {row.paperless_document_type_name})\n\n"
				f"{frappe.get_traceback()}"
			),
		)
		return False

	frappe.db.set_value(
		"SVAN Paperless Whitelisted DocType",
		row.name,
		"paperless_document_type_id",
		document_type_id,
		update_modified=False,
	)
	return True
