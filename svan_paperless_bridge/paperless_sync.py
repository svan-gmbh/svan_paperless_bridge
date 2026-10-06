# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import hashlib
import mimetypes
import re
import time

import frappe
import requests

from svan_paperless_bridge.amend_copy import RETRY, prepare_amend_copy_for_upload
from svan_paperless_bridge.correspondent_sync import (
	fetch_paperless_correspondents,
	get_configured_party_fieldname,
	get_paperless_correspondent_id_for_document,
	get_party_display_name,
	get_party_field,
	rename_frappe_party_from_paperless,
	repair_stale_correspondent_id,
)
from svan_paperless_bridge.custom_field_sync import (
	fetch_paperless_custom_fields,
	get_owned_paperless_custom_field_ids_for_document,
	get_paperless_custom_field_values_for_document,
	repair_stale_custom_field_id,
)
from svan_paperless_bridge.document_type_sync import (
	get_paperless_document_type_id_for_document,
	repair_stale_document_type_id,
)
from svan_paperless_bridge.preview import PREVIEW_METHOD_PATH, get_preview_url, is_preview_url
from svan_paperless_bridge.svan_paperless_ngx_bridge.doctype.svan_paperless_settings.svan_paperless_settings import (
	validate_settings,
)
from svan_paperless_bridge.tag_sync import (
	fetch_paperless_tags,
	get_paperless_tag_ids_for_document,
	rename_frappe_tag_from_paperless,
	repair_stale_tag_id,
)
from svan_paperless_bridge.utils.document_claim import is_document_id_held_by_other_file
from svan_paperless_bridge.utils.sync_log import log_sync_event

MAX_SAVE_ATTEMPTS = 5

# 1 first attempt + 1 retry after repairing a stale tag/correspondent/document
# type/custom field ID (see INVALID_PK_RE below).
MAX_UPLOAD_REPAIR_ATTEMPTS = 2

# Rejection by post_document/ for an unsupported file type, e.g.
# "File type application/x-msdownload not supported".
FILE_TYPE_NOT_SUPPORTED_RE = re.compile(r"File type (\S+) not supported")

# Task failures a new upload cannot fix because the file itself is the problem
# (messages from the Paperless NGX consumer/parsers). Every other failure is
# presumably solvable on the Paperless NGX side (Tika, Gotenberg, OCR setup, ...)
# and retried.
UNSOLVABLE_TASK_ERROR_RE = re.compile(
	r"Unsupported mime type|img2pdf conversion failed|Image to PDF/A conversion failed"
)

# Rejection of a deleted tag/correspondent/document type ID by Django REST
# Framework (PrimaryKeyRelatedField).
INVALID_PK_RE = re.compile(r'Invalid pk "(\d+)" - object does not exist\.')

# A separate error format for custom_fields (a JSONField rather than a
# PrimaryKeyRelatedField, see INVALID_PK_RE) - it can report several IDs at once,
# hence findall.
CUSTOM_FIELD_ID_NOT_FOUND_RE = re.compile(r"Custom field with id (\d+) does not exist")

# Buffer (seconds) in sync_pending_files(): a file is only started when there is
# still time left until max_runtime_seconds after its full API timeout.
TIME_CHECK_SAFETY_MARGIN_SECONDS = 1

# Only for the immediate attempt after insert; the scheduled job uses
# settings.api_call_timeout.
IMMEDIATE_SYNC_TIMEOUT_SECONDS = 5

# Shows in the sync log whether an attempt came from the scheduled job or right after insert.
SYNC_ORIGIN_CRON = "Scheduled job"
SYNC_ORIGIN_IMMEDIATE = "Immediate attempt on creation"

# Outcomes of _patch_document_metadata()/_adopt_existing_document() besides RETRY
# (see amend_copy.py).
OUTCOME_OK = "ok"
OUTCOME_GONE = "gone"


def reserve_file_for_upload(file_name):
	"""Reserve a file for upload atomically (status "pending" -> "syncing").

	Prevents the scheduled job and the manual sync button from uploading the same
	file at the same time and thereby creating a duplicate in Paperless NGX: the
	UPDATE ... WHERE svan_paperless_sync_status = 'pending' is atomic, and only the
	process whose row was actually matched has reserved it. Committed immediately so
	that the reservation is visible to parallel processes right away.

	Returns True if THIS call made the reservation, otherwise False.
	"""
	frappe.db.sql(
		"""
		UPDATE `tabFile`
		SET svan_paperless_sync_status = 'syncing'
		WHERE name = %s AND svan_paperless_sync_status = 'pending'
		""",
		file_name,
	)
	reserved = frappe.db._cursor.rowcount == 1
	frappe.db.commit()
	return reserved


def save_uuid(file_name, settings, new_uuid, origin=None):
	"""Save a new Paperless NGX UUID on the file (up to MAX_SAVE_ATTEMPTS
	attempts). If all of them fail, the file is still NOT set back to "pending":
	new_uuid already exists in Paperless NGX, and another upload would only create a
	duplicate (see the final except branch).

	origin (see query_uuid()) is appended to every log entry as an additional line
	in details - the sync log behaviour itself (which events are logged when) stays
	IDENTICAL to a scheduled run; the detail line merely makes visible whether the
	call came from the scheduled job or from the immediate attempt on creation (see
	paperless_sync.attempt_immediate_sync()).
	"""
	origin = origin or SYNC_ORIGIN_CRON

	def _log(log_type, title, details=None):
		suffix = f"Triggered by: {origin}"
		details = f"{details}\n\n{suffix}" if details else suffix
		log_sync_event(file_name, settings, log_type, "save_uuid", title=title, details=details)

	for attempt in range(1, MAX_SAVE_ATTEMPTS + 1):
		file_doc = frappe.get_doc("File", file_name)

		if file_doc.svan_paperless_uuid:
			_log(
				"Info",
				title="UUID already present - save skipped",
				details=(f"New UUID: {new_uuid}\nExisting UUID: {file_doc.svan_paperless_uuid}"),
			)
			return

		try:
			file_doc.svan_paperless_sync_status = "syncing"
			file_doc.svan_paperless_uuid = new_uuid
			file_doc.save(ignore_permissions=True)
		except Exception:
			if attempt == MAX_SAVE_ATTEMPTS:
				# db_set/set_value instead of doc.save(): the UUID has to be
				# persisted despite repeated save errors, as the document already
				# exists in Paperless NGX.
				frappe.db.set_value(
					"File",
					file_name,
					{"svan_paperless_sync_status": "broken", "svan_paperless_uuid": new_uuid},
					update_modified=False,
				)
				_log(
					"Error",
					title=f"Saving the UUID failed after {MAX_SAVE_ATTEMPTS} attempts - marked as 'broken'",
					details=(
						f"New UUID: {new_uuid}\n"
						f"Existing UUID: {file_doc.svan_paperless_uuid}\n\n"
						f"{frappe.get_traceback()}"
					),
				)
			continue
		else:
			_log(
				"Info",
				title="UUID saved successfully",
				details=f"UUID {new_uuid} saved (status: syncing, attempt {attempt}/{MAX_SAVE_ATTEMPTS}).",
			)
			return


def find_unclaimed_document_by_checksum(checksum, settings):
	"""Return the ID of a Paperless NGX document with this checksum that no File
	references yet (e.g. left behind by an upload whose task UUID was never saved),
	otherwise None. Documents already owned by a File are skipped: every File needs
	a document of its own.
	"""
	for document_id in find_paperless_document_ids_by_checksum(checksum, settings):
		if not is_document_id_held_by_other_file(document_id):
			return document_id
	return None


def find_paperless_document_ids_by_checksum(checksum, settings):
	"""Look for documents already present in Paperless NGX by checksum (SHA-256 of
	the original content) (GET /api/documents/?checksum__iexact=<hash>).
	checksum__iexact is the only equality lookup Paperless NGX supports for this.
	Paperless NGX 3+ stores duplicates, so there can be several hits.

	Returns a list of document IDs, empty on no match or on any error - a failure
	here must never prevent the upload, hence only "Warn" rather than "Error".
	"""
	token = settings.get_password("api_token", raise_exception=False)
	url = settings.api_url.rstrip("/") + "/documents/"
	headers = {"Authorization": f"Token {token}"}

	try:
		response = requests.get(
			url,
			headers=headers,
			params={"checksum__iexact": checksum, "page_size": 100},
			timeout=settings.api_call_timeout or 30,
			verify=True,
		)
	except Exception:
		log_sync_event(
			None,
			settings,
			"Warn",
			"find_paperless_document_ids_by_checksum",
			title="Request failed - the upload is attempted normally instead",
			details=frappe.get_traceback(),
		)
		return []

	if response.status_code != 200:
		log_sync_event(
			None,
			settings,
			"Warn",
			"find_paperless_document_ids_by_checksum",
			title=f"Response: HTTP {response.status_code} - the upload is attempted normally instead",
			details=response.text,
		)
		return []

	try:
		results = response.json().get("results", [])
	except Exception:
		log_sync_event(
			None,
			settings,
			"Warn",
			"find_paperless_document_ids_by_checksum",
			title="Response was not valid JSON - the upload is attempted normally instead",
			details=f"HTTP {response.status_code}\n\n{frappe.get_traceback()}",
		)
		return []

	return [result["id"] for result in results]


def _resolve_upload_metadata(file_name, file_doc, settings):
	"""Determine tags/correspondent/document type/custom field values for the
	upload or metadata update of a file - shared by query_uuid() (the initial
	upload) and push_metadata_update() (a subsequent update), as both need the same
	resolution logic.

	Considers ONLY the values of the business document the file is attached to
	(file_doc.attached_to_doctype/-name) - metadata DIRECTLY ON THE FILE DOCUMENT
	ITSELF no longer plays any role in the sync:
	  - Tags: all tags of the business document.
	  - Correspondent: that of the business document, if it can be determined. If a
	    party_fieldname is configured for it but currently empty, the correspondent
	    in Paperless NGX is explicitly CLEARED (None) instead of falling back to the
	    global default - if, on the other hand, no party_fieldname is configured for
	    this DocType at all, the global default correspondent
	    (settings.svan_default_correspondent_id) remains the default as before.
	  - Custom field values: only those of the business document.
	  - Document type: as before, only through the business document (see
	    document_type_sync.py).
	"""
	business_doctype = file_doc.attached_to_doctype
	business_name = file_doc.attached_to_name

	tag_ids = get_paperless_tag_ids_for_document(file_name, business_doctype, business_name, settings)

	correspondent_id = get_paperless_correspondent_id_for_document(
		file_name, business_doctype, business_name, settings
	)
	if not correspondent_id:
		if get_configured_party_fieldname(business_doctype):
			correspondent_id = None
		else:
			correspondent_id = settings.svan_default_correspondent_id

	document_type_id = get_paperless_document_type_id_for_document(file_name, business_doctype, settings)

	custom_field_values = get_paperless_custom_field_values_for_document(
		file_name, business_doctype, business_name, settings
	)

	return tag_ids, correspondent_id, document_type_id, custom_field_values


def query_uuid(file_name, settings, origin=None):
	"""Request a Paperless NGX UUID for a file: reserves it atomically (see
	reserve_file_for_upload(); a failure means another run is already processing it,
	and it is skipped silently) and uploads it. On HTTP 200 the UUID is stored
	through save_uuid(). If the file content cannot be read (e.g. it is hosted
	externally), only a warning is logged and the status set to "broken".

	origin denotes the calling context for the sync log (SYNC_ORIGIN_CRON from the
	regular scheduled run sync_pending_files(), SYNC_ORIGIN_IMMEDIATE from the
	immediate attempt on creation paperless_sync.attempt_immediate_sync()) - it is
	appended to EVERY log entry of this function as an additional detail line (see
	_log() below), WITHOUT anything else about the flow or the events logged
	differing: an immediate attempt should behave in the sync log exactly like a
	scheduled run, only with this additional origin note. It applies only to the log
	entries of THIS function (including save_uuid() called from it) - the underlying
	tag/correspondent/document type/custom field subsystems log without an origin
	note as before.
	"""
	origin = origin or SYNC_ORIGIN_CRON

	def _log(log_type, title, details=None):
		suffix = f"Triggered by: {origin}"
		details = f"{details}\n\n{suffix}" if details else suffix
		log_sync_event(file_name, settings, log_type, "query_uuid", title=title, details=details)

	def _release_for_retry():
		# Transient failure after the reservation: without this the file would stay
		# "syncing" with neither UUID nor document ID, which poll_syncing_files()
		# can never complete. Back to "pending" so that the next scheduled run
		# retries it (the checksum lookup adopts a document the failed attempt may
		# already have created).
		file_doc.db_set("svan_paperless_sync_status", "pending", update_modified=False)

	file_doc = frappe.get_doc("File", file_name)

	if file_doc.svan_paperless_uuid:
		return

	if not reserve_file_for_upload(file_name):
		return

	_log(
		"Info",
		title="File transfer starting...",
		details=f"File '{file_doc.file_name}' is being uploaded.",
	)

	if is_preview_url(file_doc.file_url):
		if prepare_amend_copy_for_upload(file_doc, settings, _log) == RETRY:
			_release_for_retry()
		return

	try:
		# No encodings: get_content() would otherwise decode anything readable as text
		# (Windows-1250/-1252, UTF-8 with BOM) and the upload would send it re-encoded as UTF-8.
		content = file_doc.get_content(encodings=())
	except Exception:
		file_doc.db_set("svan_paperless_sync_status", "broken", update_modified=False)
		_log(
			"Warn",
			title="File content could not be read - skipped",
			details=(
				f"The content of file '{file_doc.file_name}' could not be read "
				"(e.g. hosted externally). Sync status set to 'broken'.\n\n"
				f"{frappe.get_traceback()}"
			),
		)
		return

	# A document with the same content that no File owns yet (e.g. from an upload whose UUID
	# was never saved) is adopted instead of uploading again.
	checksum = hashlib.sha256(content if isinstance(content, bytes) else content.encode()).hexdigest()
	document_id = find_unclaimed_document_by_checksum(checksum, settings)
	if document_id:
		outcome = _adopt_existing_document(file_doc, document_id, settings, _log)
		if outcome == OUTCOME_OK:
			return
		if outcome == RETRY:
			_release_for_retry()
			return
		# OUTCOME_GONE: it disappeared in the meantime - upload normally.

	# A cached ID deleted in Paperless NGX only shows up as a 400 here; it is repaired and
	# the upload retried once.
	for attempt in range(1, MAX_UPLOAD_REPAIR_ATTEMPTS + 1):
		tag_ids, correspondent_id, document_type_id, custom_field_values = _resolve_upload_metadata(
			file_name, file_doc, settings
		)

		try:
			# get_password() also sees a token that is not saved yet.
			token = settings.get_password("api_token", raise_exception=False)
			url = settings.api_url.rstrip("/") + "/documents/post_document/"
			headers = {"Authorization": f"Token {token}"}
			data = {
				"title": file_doc.file_name,
				"created": file_doc.creation,
			}
			if correspondent_id:
				# requests would send None in a multipart form as the string "None".
				data["correspondent"] = correspondent_id
			if tag_ids:
				# requests sends a list as a repeated form field, as post_document/ expects.
				data["tags"] = tag_ids
			if custom_field_values:
				# custom_fields is a JSONField; in a multipart request it must be a JSON string.
				data["custom_fields"] = frappe.as_json(custom_field_values)
			if document_type_id:
				data["document_type"] = document_type_id
			files = {"document": (file_doc.file_name, content)}

			response = requests.post(
				url,
				headers=headers,
				data=data,
				files=files,
				timeout=settings.api_call_timeout or 30,
				verify=True,
			)
		except Exception:
			_log(
				"Error",
				title="Request failed - queued for the next scheduled run",
				details=frappe.get_traceback(),
			)
			_release_for_retry()
			return

		_log(
			"Info",
			title=f"Response: HTTP {response.status_code}",
			details=response.text,
		)

		if response.status_code == 400 and attempt < MAX_UPLOAD_REPAIR_ATTEMPTS:
			repaired = False

			invalid_tag_id = _find_invalid_tag_id(response)
			if invalid_tag_id and repair_stale_tag_id(file_name, invalid_tag_id, settings):
				repaired = True
				_log(
					"Info",
					title="Stale Paperless tag ID repaired - retrying the upload",
					details=(
						f"Paperless NGX tag ID {invalid_tag_id} no longer exists there "
						"(presumably deleted directly in Paperless NGX). The mapping row was "
						"updated, the upload is retried with a freshly resolved tag ID."
					),
				)

			invalid_correspondent_id = _find_invalid_correspondent_id(response)
			if invalid_correspondent_id and repair_stale_correspondent_id(
				file_name, invalid_correspondent_id, settings
			):
				repaired = True
				_log(
					"Info",
					title="Stale Paperless correspondent ID repaired - retrying the upload",
					details=(
						f"Paperless NGX correspondent ID {invalid_correspondent_id} no longer exists "
						"there (presumably deleted directly in Paperless NGX). The mapping row was "
						"updated, the upload is retried with a freshly resolved correspondent ID."
					),
				)

			invalid_document_type_id = _find_invalid_document_type_id(response)
			if invalid_document_type_id and repair_stale_document_type_id(
				file_name, invalid_document_type_id, settings
			):
				repaired = True
				_log(
					"Info",
					title="Stale Paperless document type ID repaired - retrying the upload",
					details=(
						f"Paperless NGX document type ID {invalid_document_type_id} no longer exists "
						"there (presumably deleted directly in Paperless NGX). The whitelist row was "
						"updated, the upload is retried with a freshly resolved document type ID."
					),
				)

			for invalid_custom_field_id in _find_invalid_custom_field_ids(response):
				if repair_stale_custom_field_id(file_name, invalid_custom_field_id, settings):
					repaired = True
					_log(
						"Info",
						title="Stale Paperless custom field ID repaired - retrying the upload",
						details=(
							f"Paperless NGX custom field ID {invalid_custom_field_id} no longer exists "
							"there (presumably deleted directly in Paperless NGX). The mapping row was "
							"updated, the upload is retried with a freshly resolved custom field ID."
						),
					)

			if repaired:
				continue

		break

	if response.status_code != 200:
		log_type = "Error"
		if response.status_code == 400 and FILE_TYPE_NOT_SUPPORTED_RE.search(response.text):
			# Known unsolvable: Paperless NGX cannot process this file type at all, a
			# retry would not change the outcome. Any other error - including an
			# unknown 400 - is retried on the next run.
			file_doc.db_set("svan_paperless_sync_status", "broken", update_modified=False)
			removed_extension = remove_unsupported_file_type_from_whitelist(
				file_name, file_doc, response, settings
			)
			if removed_extension:
				extension, mime_type = removed_extension
				log_sync_event(
					file_name,
					settings,
					"Warn",
					"remove_unsupported_file_type_from_whitelist",
					title="File type removed from the whitelist automatically",
					details=f"Extension: {extension}, MIME type: {mime_type}\n\n",
				)
			# It is removed from the whitelist automatically and no longer affects
			# future files with the same extension - hence only "Warn" rather than
			# "Error".
			log_type = "Warn"
		else:
			_release_for_retry()
		_log(
			log_type,
			title="Aborted",
			details="Aborted because of the previous message.",
		)
		return

	try:
		new_uuid = response.json()
	except Exception:
		_log(
			"Error",
			title="Response was not valid JSON - queued for the next scheduled run",
			details=f"HTTP {response.status_code}\n\n{frappe.get_traceback()}",
		)
		_release_for_retry()
		return

	_touch_mappings_last_synced(tag_ids, correspondent_id, custom_field_values)
	save_uuid(file_name, settings, new_uuid, origin=origin)


def _adopt_existing_document(file_doc, document_id, settings, log):
	"""Link a File to an existing Paperless NGX document that no other File owns.

	The document may carry tags, a correspondent or custom field values from
	elsewhere. They must never reach this File's business document through the
	webhook, so the business document's state is pushed to Paperless NGX FIRST and the
	document ID is stored only afterwards: until then the webhook finds no File for
	the document and ignores it.

	Only svan_paperless_document_id is set, finalize_sync() is NOT called: the file
	stays "syncing" and poll_syncing_files() completes it (no task UUID exists, see
	reconcile_file_classification()).

	Returns OUTCOME_OK, OUTCOME_GONE (HTTP 404, the caller uploads normally) or
	RETRY (the push failed, the caller queues the file again).
	"""
	outcome = _patch_document_metadata(file_doc, document_id, settings, "query_uuid")
	if outcome != OUTCOME_OK:
		return outcome

	file_doc.db_set("svan_paperless_document_id", document_id, update_modified=False)
	log(
		"Info",
		"File already present in Paperless NGX (checksum match) - upload skipped",
		f"Paperless NGX document ID: {document_id}. Its metadata was overwritten with the state "
		"of the business document. Will be completed on the next run of the sync status job.",
	)
	return OUTCOME_OK


def _touch_mappings_last_synced(tag_ids, correspondent_id, custom_field_values):
	"""Setzt `last_synced` an allen Mapping-Zeilen, deren Paperless-ID gerade
	erfolgreich an Paperless NGX uebertragen wurde (Upload bzw. Metadaten-Update).
	"""
	now = frappe.utils.now_datetime()
	for doctype, id_field, ids in (
		("SVAN Paperless Tag Mapping", "paperless_tag_id", tag_ids),
		("SVAN Paperless Correspondent Mapping", "paperless_correspondent_id", [correspondent_id]),
		("SVAN Paperless Custom Field Mapping", "paperless_custom_field_id", list(custom_field_values)),
	):
		ids = [i for i in ids if i]
		if ids:
			frappe.db.set_value(doctype, {id_field: ["in", ids]}, "last_synced", now, update_modified=False)


def _find_invalid_tag_id(response):
	"""Extract an invalid tag ID from a 400 response of post_document/.

	Only responsible for the "tags" field of the JSON error response (see
	INVALID_PK_RE) - an identically worded rejection of correspondent/document_type
	is deliberately not considered; those should still be treated as a permanent
	error ("broken") as before.

	Returns the first invalid tag ID named in the error message, or None (no valid
	JSON, no "tags" error, or a "tags" error for a reason other than a non-existent
	ID).
	"""
	return _find_invalid_pk(response, "tags")


def _find_invalid_correspondent_id(response):
	"""Extract an invalid correspondent ID from a 400 response of post_document/ -
	analogous to _find_invalid_tag_id(), but for the "correspondent" field.
	"""
	return _find_invalid_pk(response, "correspondent")


def _find_invalid_document_type_id(response):
	"""Extract an invalid document type ID from a 400 response of post_document/ -
	analogous to _find_invalid_tag_id(), but for the "document_type" field.
	"""
	return _find_invalid_pk(response, "document_type")


def _find_invalid_custom_field_ids(response):
	"""Extract all invalid custom field IDs from a 400 response.

	Returns a list (unlike the _find_invalid_*_id() functions, which are designed
	for single PKs), because Paperless NGX can report several invalid IDs at once
	here.

	TWO different error formats, depending on the endpoint/payload shape for
	"custom_fields" - reproduced live that Paperless NGX really does answer
	differently here:
	- post_document/ (multipart, custom_fields as a {field_id: value} dict JSON
	  string, see query_uuid()): a flat list of strings in the format "Custom field
	  with id <id> does not exist" (CUSTOM_FIELD_ID_NOT_FOUND_RE).
	- PATCH /documents/<id>/ (JSON body, custom_fields as a list of
	  {"field": <id>, "value": ...} objects, see push_metadata_update()): DRF
	  validates that as a list of nested serializers - errors therefore come back as
	  a LIST OF PER-ENTRY DICTS (one dict per custom_fields list element, empty {}
	  for a valid entry), each {"field": ["Invalid pk \"<id>\" - object does not
	  exist."]} on an error - the same wording as INVALID_PK_RE, only per list
	  element instead of as a single string for the whole field. A plain
	  isinstance(response.json().get("custom_fields"), list) comparison is not enough
	  to tell them apart (both formats are lists) - only looking at the FIRST entry
	  reveals whether it is a list of strings (post_document/) or a list of dicts
	  (PATCH).
	"""
	try:
		field_errors = response.json().get("custom_fields")
	except Exception:
		return []

	if not field_errors:
		return []

	if isinstance(field_errors, list) and isinstance(field_errors[0], dict):
		invalid_ids = []
		for item_errors in field_errors:
			field_messages = item_errors.get("field")
			if not field_messages:
				continue
			text = " ".join(field_messages) if isinstance(field_messages, list) else str(field_messages)
			match = INVALID_PK_RE.search(text)
			if match:
				invalid_ids.append(int(match.group(1)))
		return invalid_ids

	text = " ".join(field_errors) if isinstance(field_errors, list) else str(field_errors)
	return [int(match) for match in CUSTOM_FIELD_ID_NOT_FOUND_RE.findall(text)]


def _find_invalid_pk(response, field_name):
	try:
		field_errors = response.json().get(field_name)
	except Exception:
		return None

	if not field_errors:
		return None

	text = " ".join(field_errors) if isinstance(field_errors, list) else str(field_errors)
	match = INVALID_PK_RE.search(text)
	return int(match.group(1)) if match else None


def remove_unsupported_file_type_from_whitelist(file_name, file_doc, response, settings):
	"""Remove the file extension from the whitelist automatically when Paperless
	NGX rejects a file with "File type <mime> not supported" (HTTP 400).

	Called from query_uuid() as soon as the upload API returns exactly this error -
	all other 400 errors (e.g. an invalid correspondent) leave the whitelist
	untouched and still only mark the individual file as "broken". This only comes
	into play when the extension was on settings.whitelisted_file_types at all
	(otherwise the file would never have been classified as "pending") - e.g.
	because a user added a type Paperless NGX genuinely does not support to the
	whitelist manually, or because the types supported by Paperless NGX have changed
	since the last comparison. It also updates the MIME type in "SVAN Paperless File
	Type" from the error message if none is stored there yet. A failure is logged and
	changes nothing about the "broken" status already set on the current file -
	automatic removal must never interrupt the sync run.

	Saves the settings with flags.ignore_validate: SVANPaperlessSettings.validate()
	would otherwise trigger a full connection test (a live HTTP call to Paperless
	NGX, see test_paperless_connection()) for EVERY removed extension, just to
	remove a single whitelist row - an unnecessary drain on the scheduled job's
	runtime budget (max_runtime_seconds). Frappe's own core validation (mandatory
	fields, links) still runs despite ignore_validate, only the custom validate()
	hook is skipped - the version bump validate() normally takes care of
	(bump_sync_config_version_if_whitelists_changed) is therefore done manually
	here.

	Returns the tuple (extension, mime_type) when a whitelist row was actually
	REMOVED, otherwise None (no match, no extension, or the extension was not on the
	whitelist at all) - the caller decides from that whether to log an info about
	the automatic removal.
	"""
	match = FILE_TYPE_NOT_SUPPORTED_RE.search(response.text)
	if not match:
		return None

	extension = (file_doc.file_type or "").strip().lstrip(".").upper()
	if not extension:
		return None

	mime_type = match.group(1)

	try:
		file_type_name = frappe.db.exists("SVAN Paperless File Type", extension)
		if file_type_name:
			file_type_doc = frappe.get_doc("SVAN Paperless File Type", file_type_name)
			if not file_type_doc.mime_type:
				file_type_doc.mime_type = mime_type
				file_type_doc.save(ignore_permissions=True)
		else:
			file_type_doc = frappe.new_doc("SVAN Paperless File Type")
			file_type_doc.file_extension = extension
			file_type_doc.mime_type = mime_type
			# A concurrent run may have created the same extension in the meantime;
			# the row is named after the extension, so the existing one is equivalent.
			file_type_doc.insert(ignore_permissions=True, ignore_if_duplicate=True)

		settings_doc = frappe.get_doc("SVAN Paperless Settings")
		matching_rows = [
			row for row in settings_doc.whitelisted_file_types if row.file_type == file_type_doc.name
		]
		if not matching_rows:
			return None

		for row in matching_rows:
			settings_doc.remove(row)
		settings_doc.sync_config_version = (settings_doc.sync_config_version or 0) + 1
		settings_doc.flags.ignore_validate = True
		settings_doc.save(ignore_permissions=True)
	except Exception:
		log_sync_event(
			file_name,
			settings,
			"Error",
			"remove_unsupported_file_type_from_whitelist",
			title="Removing the file type from the whitelist failed",
			details=f"Extension: {extension}, MIME type: {mime_type}\n\n{frappe.get_traceback()}",
		)
		return None

	return extension, mime_type


def get_whitelisted_file_types(settings):
	"""Read the file type whitelist from the settings and resolve it into two
	sets: allowed file extensions (comparable to File.file_type) and allowed MIME
	types.

	An empty whitelist means "nothing allowed" (analogous to
	settings.whitelisted_doctypes) - not "no restriction". It is prefilled
	automatically with the file types Paperless NGX supports out of the box when the
	settings are first set up (see
	utils.default_file_types.populate_default_whitelist()).
	"""
	whitelisted_names = [row.file_type for row in settings.whitelisted_file_types]
	if not whitelisted_names:
		return set(), set()

	file_type_docs = frappe.get_all(
		"SVAN Paperless File Type",
		filters={"name": ["in", whitelisted_names]},
		fields=["file_extension", "mime_type"],
	)

	whitelisted_extensions = {d.file_extension for d in file_type_docs if d.file_extension}
	whitelisted_mimetypes = {d.mime_type for d in file_type_docs if d.mime_type}
	return whitelisted_extensions, whitelisted_mimetypes


def is_file_sync_eligible(file, whitelisted_doctypes, whitelisted_extensions, whitelisted_mimetypes):
	"""Check whether a file is relevant for syncing with Paperless NGX according to
	the current whitelists (DocType AND file type whitelist).

	The core logic of reconcile_file_classification() (called there for all "stale"
	files from frappe.get_all()) as well as of attempt_immediate_sync() (called
	there for a single, just created file via frappe.get_doc()) - file can therefore
	be either a full File document or a lightweight frappe._dict; both provide
	is_folder/uploaded_to_dropbox/uploaded_to_google_drive/attached_to_doctype/
	file_type/file_name (for missing keys frappe._dict returns None rather than
	raising an AttributeError).
	"""
	if file.is_folder or file.uploaded_to_dropbox or file.uploaded_to_google_drive:
		return False

	guessed_mimetype = mimetypes.guess_type(file.file_name or "")[0]
	return file.attached_to_doctype in whitelisted_doctypes and (
		file.file_type in whitelisted_extensions or guessed_mimetype in whitelisted_mimetypes
	)


def reconcile_file_classification(
	settings, whitelisted_doctypes, whitelisted_extensions=None, whitelisted_mimetypes=None
):
	"""Reclassify files whose sync relevance may have changed, before the actual
	sync run starts.

	A file is "relevant" (status "pending") when its DocType is on the DocType
	whitelist AND (its file extension OR the MIME type guessed from its file name)
	is on the file type whitelist - folders as well as files uploaded to
	Dropbox/Google Drive are ignored entirely regardless (see the filter below), as
	their relevance can never change. "broken" and "deleted" files are deliberately
	never touched - like "broken", "deleted" (see paperless_delete.py) is a terminal
	state: the Paperless NGX document has already been deleted (and the local
	content possibly restored from Paperless NGX beforehand, see there), so
	reclassifying it as "pending" would wrongly trigger a new upload.

	So that not every run has to check every file as their number grows, each file
	is stamped with the configuration state (SVAN Paperless
	Settings.sync_config_version) it was last evaluated against
	(svan_paperless_config_version_checked). Only files with a stale stamp are
	re-evaluated - after one pass this query stays small permanently, until one of
	the two whitelists changes again.

	The filter on svan_paperless_document_id (in addition to svan_paperless_uuid) is
	necessary because query_uuid() takes the document ID over directly on a checksum
	match (see _adopt_existing_document()) WITHOUT ever receiving a task
	UUID - such a file would otherwise remain a "stale" candidate despite having
	completed successfully and could wrongly be reset to "pending" (or "") on the
	next whitelist change.
	"""
	whitelisted_extensions = whitelisted_extensions or set()
	whitelisted_mimetypes = whitelisted_mimetypes or set()
	current_version = settings.sync_config_version or 0

	stale_files = frappe.get_all(
		"File",
		filters={
			"svan_paperless_uuid": ["in", ["", None]],
			"svan_paperless_document_id": ["in", ["", None]],
			"svan_paperless_sync_status": ["not in", ["broken", "deleted"]],
			"svan_paperless_config_version_checked": ["!=", current_version],
			"is_folder": 0,
			"uploaded_to_dropbox": 0,
			"uploaded_to_google_drive": 0,
		},
		fields=["name", "attached_to_doctype", "file_type", "file_name"],
	)

	made_pending = 0
	made_empty = 0

	for file in stale_files:
		eligible = is_file_sync_eligible(
			file, whitelisted_doctypes, whitelisted_extensions, whitelisted_mimetypes
		)
		frappe.db.set_value(
			"File",
			file.name,
			{
				"svan_paperless_sync_status": "pending" if eligible else "",
				"svan_paperless_config_version_checked": current_version,
			},
			update_modified=False,
		)
		if eligible:
			made_pending += 1
		else:
			made_empty += 1

	if made_pending or made_empty:
		log_sync_event(
			None,
			settings,
			"Info",
			"reconcile_file_classification (CronJob)",
			title="Files reclassified against the current whitelists",
			details=(
				f"Configuration state: {current_version}\n"
				f"Newly set to 'pending': {made_pending}\n"
				f"Reset to empty (not relevant): {made_empty}"
			),
		)


def reconcile_pending_files(settings=None, whitelisted_doctypes=None):
	"""Run reconcile_file_classification() on its own and commit.

	Unlike sync_pending_files(), it needs no valid API credentials, as it only
	classifies against the two whitelists without actually transferring files to
	Paperless NGX. Used both by the scheduler (sync_pending_files) and by the manual
	"Classify Now" button (trigger_manual_reconcile).
	"""
	if settings is None:
		settings = frappe.get_single("SVAN Paperless Settings")
	if whitelisted_doctypes is None:
		whitelisted_doctypes = [row.document_type for row in settings.whitelisted_doctypes]

	whitelisted_extensions, whitelisted_mimetypes = get_whitelisted_file_types(settings)

	reconcile_file_classification(
		settings, whitelisted_doctypes, whitelisted_extensions, whitelisted_mimetypes
	)
	frappe.db.commit()


def _settings_configured_for_sync(settings):
	"""Check the same mandatory fields as validate_settings(), but read-only -
	without frappe.throw() and without a sync log entry.

	validate_settings() deliberately logs an incomplete configuration as an error,
	because it is only called once per scheduled run (every 5 minutes).
	attempt_immediate_sync(), by contrast, runs on EVERY file insert system-wide -
	with validate_settings(), a Paperless connection that is not fully set up yet
	would flood the sync log with one error per file instead of, as usual, one per
	scheduled run.
	"""
	return bool(
		settings.api_url
		and settings.get_password("api_token", raise_exception=False)
		and settings.default_correspondent
		and settings.svan_default_correspondent_id
		and settings.whitelisted_doctypes
		and settings.whitelisted_file_types
	)


def attempt_immediate_sync(file_name):
	"""Optimistic first attempt right after a file was created (see
	file_events.sync_immediately(), hook "after_insert"): tries to upload THIS one
	file to Paperless NGX immediately with a shortened timeout
	(IMMEDIATE_SYNC_TIMEOUT_SECONDS) instead of waiting for the next scheduled run
	(sync_pending_files(), every 5 minutes).

	Purely optimistic and must never prevent the file from being created: every
	exception is caught, and unlike in the regular scheduled run a file that fails
	here is NEVER marked as "broken" - it stays (or is reset to) "pending" and is
	therefore picked up by the next sync_pending_files() run (full timeout, full
	repair logic) as usual. "Success" is determined by whether query_uuid() set a
	UUID or (on a checksum match) a document ID directly - regardless of the
	intermediate status query_uuid() set for it itself (e.g. "broken" for an
	unsupported file type, or unchanged "syncing" after a network error during the
	upload).
	"""
	try:
		settings = frappe.get_single("SVAN Paperless Settings")
		if not _settings_configured_for_sync(settings):
			return

		whitelisted_doctypes = [row.document_type for row in settings.whitelisted_doctypes]
		whitelisted_extensions, whitelisted_mimetypes = get_whitelisted_file_types(settings)

		file_doc = frappe.get_doc("File", file_name)
		if not is_file_sync_eligible(
			file_doc, whitelisted_doctypes, whitelisted_extensions, whitelisted_mimetypes
		):
			return

		frappe.db.set_value(
			"File",
			file_name,
			{
				"svan_paperless_sync_status": "pending",
				"svan_paperless_config_version_checked": settings.sync_config_version or 0,
			},
			update_modified=False,
		)

		settings.api_call_timeout = IMMEDIATE_SYNC_TIMEOUT_SECONDS
		query_uuid(file_name, settings, origin=SYNC_ORIGIN_IMMEDIATE)

		progressed = frappe.db.get_value(
			"File", file_name, ["svan_paperless_uuid", "svan_paperless_document_id"]
		)
		if not any(progressed or ()):
			frappe.db.set_value(
				"File", file_name, "svan_paperless_sync_status", "pending", update_modified=False
			)
			log_sync_event(
				file_name,
				settings,
				"Info",
				"attempt_immediate_sync",
				title="Immediate attempt on creation unsuccessful - queued for the next scheduled run",
				details=f"Shortened timeout: {IMMEDIATE_SYNC_TIMEOUT_SECONDS}s.",
			)

		frappe.db.commit()
	except Exception:
		frappe.log_error(
			title="svan_paperless_bridge: immediate attempt on creation failed",
			message=frappe.get_traceback(),
		)


def sync_pending_files(enforce_limits=True):
	"""Scheduled task: syncs pending files with Paperless NGX.

	Validates the SVAN Paperless Settings once through validate_settings()
	(including the DocType AND file type whitelist - an empty whitelist counts as an
	incomplete configuration there and aborts the run), then reclassifies all files
	that are new or changed since the last whitelist change through
	reconcile_file_classification(), loads all files classified as "pending"
	afterwards and triggers the UUID request per file. This deliberately works
	WITHOUT an overarching database transaction: a commit follows every file, so
	that an error on one file does not undo the progress already made.

	When enforce_limits is True (the default, as the scheduler calls this function),
	the run limits itself to at most settings.max_files_per_run files and
	settings.max_runtime_seconds seconds of total runtime (the stopwatch starts at
	the beginning of this function, so it includes the reconcile phase) - necessary
	because Frappe would otherwise terminate the scheduled job forcibly after 300
	seconds (RQ queue "default"). When the time budget is reached, the file loop
	stops in a controlled way (no error, just an info log) and the remaining files
	stay "pending" for the next run. The manual "Transfer New Files" button
	(trigger_manual_sync) deliberately calls with enforce_limits=False, as it is
	meant to do exactly "sync everything now" and runs in the "long" queue (1500s
	timeout).
	"""
	start_time = time.monotonic()
	settings = frappe.get_single("SVAN Paperless Settings")

	try:
		validate_settings(settings)
	except Exception:
		# validate_settings() already records an incomplete configuration
		# (including an empty whitelist) in the sync log itself before raising
		# through frappe.throw() - so no additional logging is needed here.
		return

	whitelisted_doctypes = [row.document_type for row in settings.whitelisted_doctypes]

	reconcile_pending_files(settings, whitelisted_doctypes)

	file_query_kwargs = {}
	if enforce_limits:
		# 0 deliberately pauses uploads while classification still runs; "or 100" alone
		# would turn it into 100.
		if settings.max_files_per_run == 0:
			return
		file_query_kwargs["limit_page_length"] = settings.max_files_per_run or 100

	file_filters = {
		"svan_paperless_uuid": ["in", ["", None]],
		"svan_paperless_sync_status": "pending",
		"is_folder": 0,
		"uploaded_to_dropbox": 0,
		"uploaded_to_google_drive": 0,
		"attached_to_doctype": ["in", whitelisted_doctypes],
	}
	# No file type filter: "pending" is the current classification, which may rest on the
	# MIME type rather than the extension.
	file_names = frappe.get_all(
		"File",
		filters=file_filters,
		pluck="name",
		# A fixed order is needed so that a limit_page_length works through the
		# files fairly, in order, across several runs instead of catching a
		# potentially different subset on every run.
		order_by="creation asc",
		**file_query_kwargs,
	)

	api_timeout = settings.api_call_timeout or 30
	max_runtime_seconds = settings.max_runtime_seconds or 240

	for file_name in file_names:
		if enforce_limits:
			elapsed = time.monotonic() - start_time
			if elapsed + api_timeout + TIME_CHECK_SAFETY_MARGIN_SECONDS > max_runtime_seconds:
				log_sync_event(
					None,
					settings,
					"Info",
					"sync_pending_files (CronJob)",
					title="Runtime limit reached - the scheduled job is stopping in a controlled way",
					details=(
						f"After {elapsed:.1f}s of the {max_runtime_seconds}s runtime "
						"budget, the next file would exceed it. Remaining files stay "
						"'pending' and are processed in the next scheduled run."
					),
				)
				break

		try:
			query_uuid(file_name, settings)
		except Exception:
			log_sync_event(
				file_name,
				settings,
				"Error",
				"sync_pending_files (CronJob)",
				title="Unexpected error during the file upload",
				details=frappe.get_traceback(),
			)

		frappe.db.commit()


def push_metadata_update(file_name, settings):
	"""Apply tags/correspondent/document type/custom field values of an already
	"synced" file to the existing Paperless NGX document via PATCH
	(svan_paperless_document_id) - called from sync_metadata_updates() for every
	file flagged by change_watch.py (svan_paperless_needs_update = 1).

	On success only svan_paperless_needs_update is reset - no new UUID/document ID
	is due, the document already exists. An HTTP 404 (the document disappeared from
	Paperless NGX in the meantime, e.g. because paperless_delete.py is running in
	parallel or it was deleted there manually) marks the file as "broken" instead -
	another attempt would be pointless, and the flag is reset as well. Any other
	failure keeps the flag, so the next run tries again. See _patch_document_metadata()
	for the request itself.
	"""
	file_doc = frappe.get_doc("File", file_name)

	if not file_doc.svan_paperless_document_id:
		# Should not happen thanks to the filters in sync_metadata_updates() -
		# logged defensively instead of raising an exception.
		frappe.db.set_value("File", file_name, "svan_paperless_needs_update", 0, update_modified=False)
		log_sync_event(
			file_name,
			settings,
			"Warn",
			"push_metadata_update",
			title="No Paperless NGX document linked - update skipped",
			details="svan_paperless_document_id is empty; the flag was reset anyway.",
		)
		return

	outcome = _patch_document_metadata(
		file_doc, file_doc.svan_paperless_document_id, settings, "push_metadata_update"
	)

	if outcome == OUTCOME_OK:
		frappe.db.set_value("File", file_name, "svan_paperless_needs_update", 0, update_modified=False)
	elif outcome == OUTCOME_GONE:
		frappe.db.set_value(
			"File",
			file_name,
			{"svan_paperless_needs_update": 0, "svan_paperless_sync_status": "broken"},
			update_modified=False,
		)
		log_sync_event(
			file_name,
			settings,
			"Warn",
			"push_metadata_update",
			title="Document no longer exists in Paperless NGX - marked as 'broken'",
		)


def _patch_document_metadata(file_doc, document_id, settings, source):
	"""PATCH tags/correspondent/document type/custom field values of the File's
	business document onto the existing Paperless NGX document.

	Determines the same four values as the initial upload in query_uuid() FRESHLY
	(the current data, no diffs) and uses the same repair loop as there
	(repair_stale_*) for IDs that have become invalid (HTTP 400) - the error
	response formats of PATCH /documents/<id>/ go through the same DRF
	PrimaryKeyRelatedField validators as post_document/.

	For PATCH /documents/<id>/, Paperless NGX treats custom_fields as a COMPLETE
	replacement, not as a merge (verified live: a PATCH with only one of two custom
	fields set deleted the second, unsent field outright). So that custom field
	values we do not manage (other mapping rows, values set manually in Paperless
	NGX) are never touched, the document's current custom field set is loaded via
	GET beforehand, cleaned of the IDs we "own"
	(get_owned_paperless_custom_field_ids_for_document()) and then extended by our
	own freshly resolved values - an owned ID without a current value (its source
	field was cleared) is deliberately NOT reinserted, which removes the value in
	Paperless NGX in line with the forward deletion rule (see AGENTS.md).

	source is the function name for the sync log. Returns OUTCOME_OK, OUTCOME_GONE
	(HTTP 404: the document does not exist any more) or RETRY (transient failure,
	already logged).
	"""
	file_name = file_doc.name

	def _log(log_type, title, details=None):
		log_sync_event(file_name, settings, log_type, source, title=title, details=details)

	token = settings.get_password("api_token", raise_exception=False)
	url = settings.api_url.rstrip("/") + f"/documents/{document_id}/"
	headers = {"Authorization": f"Token {token}"}

	try:
		owned_custom_field_ids = get_owned_paperless_custom_field_ids_for_document(
			file_doc.attached_to_doctype
		)
		get_response = requests.get(
			url, headers=headers, timeout=settings.api_call_timeout or 30, verify=True
		)
		if get_response.status_code == 404:
			_log("Warn", title="Response: HTTP 404 - document no longer exists", details=get_response.text)
			return OUTCOME_GONE
		get_response.raise_for_status()
		foreign_custom_fields = [
			entry
			for entry in get_response.json().get("custom_fields", [])
			if entry.get("field") not in owned_custom_field_ids
		]
	except Exception:
		_log("Error", title="Error while loading the current document", details=frappe.get_traceback())
		return RETRY

	response = None
	for attempt in range(1, MAX_UPLOAD_REPAIR_ATTEMPTS + 1):
		tag_ids, correspondent_id, document_type_id, custom_field_values = _resolve_upload_metadata(
			file_name, file_doc, settings
		)

		data = {"tags": tag_ids, "correspondent": correspondent_id}
		if document_type_id:
			data["document_type"] = document_type_id
		# Unlike post_document/, PATCH takes JSON and custom_fields as a list of
		# {"field": id, "value": value}.
		data["custom_fields"] = foreign_custom_fields + [
			{"field": field_id, "value": value} for field_id, value in custom_field_values.items()
		]

		try:
			# frappe.as_json() instead of json=: values may be datetime.date, which json.dumps() rejects.
			response = requests.patch(
				url,
				headers={**headers, "Content-Type": "application/json"},
				data=frappe.as_json(data),
				timeout=settings.api_call_timeout or 30,
				verify=True,
			)
		except Exception:
			_log("Error", title="Request failed", details=frappe.get_traceback())
			return RETRY

		if response.status_code == 404:
			_log("Warn", title="Response: HTTP 404 - document no longer exists", details=response.text)
			return OUTCOME_GONE

		_log("Info", title=f"Response: HTTP {response.status_code}", details=response.text)

		if response.status_code == 400 and attempt < MAX_UPLOAD_REPAIR_ATTEMPTS:
			repaired = False

			invalid_tag_id = _find_invalid_tag_id(response)
			if invalid_tag_id and repair_stale_tag_id(file_name, invalid_tag_id, settings):
				repaired = True
				_log(
					"Info",
					title="Stale Paperless tag ID repaired - retrying the update",
					details=f"Paperless NGX tag ID {invalid_tag_id} no longer exists there.",
				)

			invalid_correspondent_id = _find_invalid_correspondent_id(response)
			if invalid_correspondent_id and repair_stale_correspondent_id(
				file_name, invalid_correspondent_id, settings
			):
				repaired = True
				_log(
					"Info",
					title="Stale Paperless correspondent ID repaired - retrying the update",
					details=f"Paperless NGX correspondent ID {invalid_correspondent_id} no longer exists there.",
				)

			invalid_document_type_id = _find_invalid_document_type_id(response)
			if invalid_document_type_id and repair_stale_document_type_id(
				file_name, invalid_document_type_id, settings
			):
				repaired = True
				_log(
					"Info",
					title="Stale Paperless document type ID repaired - retrying the update",
					details=f"Paperless NGX document type ID {invalid_document_type_id} no longer exists there.",
				)

			for invalid_custom_field_id in _find_invalid_custom_field_ids(response):
				if repair_stale_custom_field_id(file_name, invalid_custom_field_id, settings):
					repaired = True
					_log(
						"Info",
						title="Stale Paperless custom field ID repaired - retrying the update",
						details=f"Paperless NGX custom field ID {invalid_custom_field_id} no longer exists there.",
					)

			if repaired:
				continue

		break

	if response.status_code != 200:
		_log("Error", title="Aborted", details="Aborted because of the previous message.")
		return RETRY

	_touch_mappings_last_synced(tag_ids, correspondent_id, custom_field_values)
	_log("Info", title="Metadata update successful")
	return OUTCOME_OK


def sync_metadata_updates(enforce_limits=True):
	"""Scheduled task: transfers tag/correspondent/custom field changes for files
	that are already "synced" and have svan_paperless_needs_update = 1 to Paperless
	NGX via PATCH (see change_watch.py for setting the flag and
	push_metadata_update() for the actual PATCH call).

	Deliberately structured like sync_pending_files(): its own runtime limit
	(max_runtime_seconds), a commit after every file instead of one overarching
	transaction, so that an error on one file does not undo the progress already
	made. No max_files_per_run limit of its own: a PATCH is a single, lightweight
	request (not a file upload), so the runtime limit alone is enough protection
	against Frappe's 300 second scheduled job timeout.
	"""
	start_time = time.monotonic()
	settings = frappe.get_single("SVAN Paperless Settings")

	try:
		validate_settings(settings)
	except Exception:
		return

	file_names = frappe.get_all(
		"File",
		filters={"svan_paperless_sync_status": "synced", "svan_paperless_needs_update": 1},
		pluck="name",
		order_by="modified asc",
	)

	max_runtime_seconds = settings.max_runtime_seconds or 240

	for file_name in file_names:
		if enforce_limits and time.monotonic() - start_time > max_runtime_seconds:
			log_sync_event(
				None,
				settings,
				"Info",
				"sync_metadata_updates (CronJob)",
				title="Runtime limit reached - the scheduled job is stopping in a controlled way",
				details="Remaining files are processed in the next scheduled run.",
			)
			break

		try:
			push_metadata_update(file_name, settings)
		except Exception:
			log_sync_event(
				file_name,
				settings,
				"Error",
				"sync_metadata_updates (CronJob)",
				title="Unexpected error during the metadata update",
				details=frappe.get_traceback(),
			)

		frappe.db.commit()


def reconcile_metadata_changes(enforce_limits=True):
	"""Scheduled task: detects tags deleted OR renamed in Paperless NGX, renamed
	correspondents AND custom fields that were deleted or renamed/changed in type,
	and transfers that to ERPNext actively, without having to wait for a webhook
	call that may or may not come later.

	Necessary because Paperless NGX fires no webhook for a plain deletion/rename of
	a tag or custom field, nor for a plain rename of a correspondent (in each case
	without a document itself being changed) - the only configurable workflow
	trigger is "Document Updated" (see webhook_sync.py).
	webhook_sync._apply_tags()/tag_sync.ensure_frappe_tag_for_paperless_id() do
	detect the tag case reliably, but only incidentally, if a document carrying the
	affected tag happens to be saved again in Paperless NGX LATER - for a
	correspondent rename and for a globally deleted/renamed custom field there is
	(unlike for relinking a party link, which necessarily involves a document
	change) no reactive path through the webhook at all, so this scheduled job is
	the ONLY way to detect it. Without it, a deletion/rename would stay invisible in
	ERPNext indefinitely (reproduced live, see tags).

	Three independent phases, each fetching the complete current Paperless NGX
	list ONCE (one paginated GET call) instead of one request per mapping row:
	1. Tags: see tag_sync.fetch_paperless_tags() - if a mapped, bidirectional
	   paperless_tag_id is missing there, the tag counts as deleted
	   (_remove_tag_from_affected_documents()); if it appears under a different
	   name, as renamed (tag_sync.rename_frappe_tag_from_paperless(), including its
	   conflict handling).
	2. Correspondents: see correspondent_sync.fetch_paperless_correspondents() -
	   BOTH cases are handled for mapped, bidirectional rows. If an ID is missing entirely, the correspondent
	   counts as deleted globally and the party link is cleared on all affected
	   business documents (_clear_correspondent_from_affected_documents()). If the
	   current name differs from the display name of the linked party, the party
	   master record is renamed through
	   correspondent_sync.rename_frappe_party_from_paperless(). Relinking to a
	   DIFFERENT party runs purely reactively through the webhook (see
	   webhook_sync._resolve_party_swap_change()).
	3. Custom fields: see custom_field_sync.fetch_paperless_custom_fields() - if a
	   mapped, bidirectional paperless_custom_field_id is missing there, the custom
	   field counts as deleted (_clear_custom_field_from_affected_documents() -
	   clearing the value on all affected source fields, analogous to removing a
	   tag); if the name or data type differs, the mapping row itself is brought in
	   line (see _reconcile_custom_field_changes()) - purely cosmetic, changing
	   nothing about values already synced.

	Only bidirectional rows are checked: for a row with bidirectional=0, "changes in
	Paperless NGX do not affect ERPNext" applies here as well (see AGENTS.md).
	A shared runtime limit (settings.max_runtime_seconds) applies across ALL THREE
	phases - if it is already reached during an earlier phase, the next phase is not
	started in this run at all.
	"""
	start_time = time.monotonic()
	settings = frappe.get_single("SVAN Paperless Settings")

	try:
		validate_settings(settings)
	except Exception:
		return

	max_runtime_seconds = settings.max_runtime_seconds or 240

	if not _reconcile_tag_changes(settings, start_time, max_runtime_seconds, enforce_limits):
		return

	if not _reconcile_correspondent_renames(settings, start_time, max_runtime_seconds, enforce_limits):
		return

	_reconcile_custom_field_changes(settings, start_time, max_runtime_seconds, enforce_limits)


def _reconcile_tag_changes(settings, start_time, max_runtime_seconds, enforce_limits):
	"""The tag phase of reconcile_metadata_changes() - see the docstring there.

	Returns False when the runtime limit was already reached during this phase (the
	caller then skips the correspondent phase as well), otherwise True.
	"""
	try:
		current_tags_by_id = {tag["id"]: tag["name"] for tag in fetch_paperless_tags(settings)}
	except Exception:
		log_sync_event(
			None,
			settings,
			"Error",
			"reconcile_metadata_changes (CronJob)",
			title="Paperless NGX tag list could not be loaded",
			details=frappe.get_traceback(),
		)
		return True

	mappings = frappe.get_all(
		"SVAN Paperless Tag Mapping",
		filters={"bidirectional": 1, "paperless_tag_id": ["!=", 0]},
		fields=["name", "frappe_tag", "paperless_tag_id"],
	)

	for mapping in mappings:
		if enforce_limits and time.monotonic() - start_time > max_runtime_seconds:
			log_sync_event(
				None,
				settings,
				"Info",
				"reconcile_metadata_changes (CronJob)",
				title="Runtime limit reached - the scheduled job is stopping in a controlled way",
			)
			return False

		current_name = current_tags_by_id.get(mapping.paperless_tag_id)

		if current_name is None:
			_remove_tag_from_affected_documents(mapping, settings)
		elif current_name != mapping.frappe_tag:
			try:
				rename_frappe_tag_from_paperless(
					None, mapping.paperless_tag_id, mapping.frappe_tag, current_name, settings
				)
			except Exception:
				log_sync_event(
					None,
					settings,
					"Error",
					"reconcile_metadata_changes (CronJob)",
					title="Unexpected error while renaming",
					details=f"Frappe tag: {mapping.frappe_tag}\n\n{frappe.get_traceback()}",
				)
			frappe.db.commit()

	return True


def _reconcile_correspondent_renames(settings, start_time, max_runtime_seconds, enforce_limits):
	"""The correspondent phase of reconcile_metadata_changes() - see the docstring
	there.

	Returns False when the runtime limit was already reached during this phase (the
	caller then skips the custom field phase as well), otherwise True - analogous to
	_reconcile_tag_changes().
	"""
	try:
		current_correspondents_by_id = {c["id"]: c["name"] for c in fetch_paperless_correspondents(settings)}
	except Exception:
		log_sync_event(
			None,
			settings,
			"Error",
			"reconcile_metadata_changes (CronJob)",
			title="Paperless NGX correspondent list could not be loaded",
			details=frappe.get_traceback(),
		)
		return True

	mappings = frappe.get_all(
		"SVAN Paperless Correspondent Mapping",
		filters={"bidirectional": 1},
		fields=["name", "frappe_party_type", "frappe_party", "paperless_correspondent_id"],
	)

	for mapping in mappings:
		if enforce_limits and time.monotonic() - start_time > max_runtime_seconds:
			log_sync_event(
				None,
				settings,
				"Info",
				"reconcile_metadata_changes (CronJob)",
				title="Runtime limit reached - the scheduled job is stopping in a controlled way",
			)
			return False

		current_name = current_correspondents_by_id.get(mapping.paperless_correspondent_id)
		if current_name is None:
			_clear_correspondent_from_affected_documents(mapping, settings)
			continue

		display_name = get_party_display_name(mapping.frappe_party_type, mapping.frappe_party)
		if current_name == display_name:
			continue

		try:
			rename_frappe_party_from_paperless(
				None, mapping.frappe_party_type, mapping.frappe_party, current_name, settings
			)
		except Exception:
			log_sync_event(
				None,
				settings,
				"Error",
				"reconcile_metadata_changes (CronJob)",
				title="Unexpected error while renaming",
				details=f"Party: {mapping.frappe_party_type} {mapping.frappe_party}\n\n{frappe.get_traceback()}",
			)
		frappe.db.commit()

	return True


def _clear_correspondent_from_affected_documents(mapping, settings):
	"""Clear the party link on all business documents that point at this party
	deleted in Paperless NGX AND are actually linked to an already synced file -
	called from _reconcile_correspondent_renames() when a mapped
	paperless_correspondent_id no longer appears in the current Paperless NGX list.

	See correspondent_sync.get_party_field() for access to
	party_fieldname/target DocType (for a Dynamic Link, only documents whose type
	field names the mapping's party type are affected). Clearing the
	link amounts to the same thing as "relinking to no correspondent" (analogous to
	webhook_sync._resolve_party_swap_change()/correspondent_sync.ensure_party_for_paperless_correspondent()
	for a correspondent removed ON ONE document) - only for a GLOBAL deletion in
	Paperless NGX, for which (as with tags) there is no webhook.

	Limited to synced files (File.svan_paperless_document_id set) analogous to
	_remove_tag_from_affected_documents() - a business document that merely happens
	to link the same party without ever having been synced with Paperless NGX is
	left untouched.

	Up to MAX_SAVE_ATTEMPTS attempts per document (reloading the document on each
	attempt, analogous to webhook_sync._apply_field_change()) - if clearing fails
	permanently (e.g. because the field is mandatory), that is logged as "Warn" and
	the previous value stays unchanged; the remaining matches are not blocked by it.
	The mapping row itself is deliberately kept, so that a party created later under
	the same name in Paperless NGX automatically matches it again.
	"""
	candidates = []
	for row in frappe.get_all("SVAN Paperless Whitelisted DocType", fields=["document_type"]):
		fieldname, type_fieldname, target_doctype = get_party_field(row.document_type)
		if not fieldname:
			continue
		filters = {fieldname: mapping.frappe_party}
		if type_fieldname:
			filters[type_fieldname] = mapping.frappe_party_type
		elif target_doctype != mapping.frappe_party_type:
			continue

		for business_name in frappe.get_all(row.document_type, filters=filters, pluck="name"):
			candidates.append((row.document_type, business_name, fieldname))

	if not candidates:
		return

	synced_pairs = {
		(row.attached_to_doctype, row.attached_to_name)
		for row in frappe.get_all(
			"File",
			filters={
				"attached_to_doctype": ["in", list({c[0] for c in candidates})],
				"attached_to_name": ["in", list({c[1] for c in candidates})],
				"svan_paperless_document_id": ["is", "set"],
			},
			fields=["attached_to_doctype", "attached_to_name"],
		)
	}
	affected = [c for c in candidates if (c[0], c[1]) in synced_pairs]
	if not affected:
		return

	frappe.flags.svan_paperless_applying_webhook_update = True
	try:
		for business_doctype, business_name, fieldname in affected:
			for attempt in range(1, MAX_SAVE_ATTEMPTS + 1):
				try:
					doc = frappe.get_doc(business_doctype, business_name)
					doc.set(fieldname, "")
					doc.save(ignore_permissions=True)
				except Exception:
					if attempt == MAX_SAVE_ATTEMPTS:
						log_sync_event(
							None,
							settings,
							"Warn",
							"reconcile_metadata_changes (CronJob)",
							title=f"Correspondent could not be removed after {MAX_SAVE_ATTEMPTS} attempts",
							details=(
								f"{business_doctype}.{fieldname} ({business_name})\n\n{frappe.get_traceback()}"
							),
						)
					continue
				else:
					break
	finally:
		frappe.flags.svan_paperless_applying_webhook_update = False

	log_sync_event(
		None,
		settings,
		"Info",
		"reconcile_metadata_changes (CronJob)",
		title="Correspondent deleted in Paperless NGX removed",
		details=(
			f"Party: {mapping.frappe_party_type} {mapping.frappe_party}\n"
			f"Affected business documents: {len(affected)}"
		),
	)
	frappe.db.commit()


def _remove_tag_from_affected_documents(mapping, settings):
	"""Remove a tag deleted in Paperless NGX from all business documents that still
	carry it AND are actually linked to a file already synced to Paperless NGX -
	called from _reconcile_tag_changes() (the tag phase of
	reconcile_metadata_changes()).

	"Tag Link" has no relation to Paperless NGX - tags are a system-wide Frappe
	feature, and a tag name can be attached to ANY DocType, independently of this
	app (e.g. to a ToDo or a note that never had anything to do with Paperless NGX).
	Without narrowing it down, a coincidentally identically named, completely
	unrelated use of a tag would be removed too, merely because the same tag NAME was
	deleted in Paperless NGX. The narrowing goes through
	"File.svan_paperless_document_id" (see paperless_delete.py for the same marker
	meaning "has a real Paperless NGX counterpart") rather than through the DocType
	whitelist itself: a file can long since have been uploaded and synced even if its
	DocType was removed from the whitelist in the meantime - the business document
	still remains linked to a real Paperless NGX document.

	Isolated in an individual try/except (analogous to webhook_sync._apply_tags()) so
	that a failure on one document does not block the others.
	frappe.flags.svan_paperless_applying_webhook_update is set while doing so, so
	that change_watch.py ignores the hooks triggered by it - the tag already deleted
	in Paperless NGX would otherwise have to be reported there again unnecessarily
	(if ineffectively) through a metadata update.

	The "SVAN Paperless Tag Mapping" row itself is deliberately NOT deleted, only its
	tag removed from the affected documents - a tag created later under the same name
	is automatically linked to this row again through it, see
	tag_sync.ensure_frappe_tag_for_paperless_id().
	"""
	tagged = frappe.get_all(
		"Tag Link", filters={"tag": mapping.frappe_tag}, fields=["document_type", "document_name"]
	)
	if not tagged:
		return

	tagged_pairs = {(row.document_type, row.document_name) for row in tagged}
	synced_pairs = {
		(row.attached_to_doctype, row.attached_to_name)
		for row in frappe.get_all(
			"File",
			filters={
				"attached_to_doctype": ["in", list({row.document_type for row in tagged})],
				"attached_to_name": ["in", list({row.document_name for row in tagged})],
				"svan_paperless_document_id": ["is", "set"],
			},
			fields=["attached_to_doctype", "attached_to_name"],
		)
	}
	affected = [
		frappe._dict(document_type=doctype, document_name=name)
		for doctype, name in tagged_pairs
		if (doctype, name) in synced_pairs
	]
	if not affected:
		return

	frappe.flags.svan_paperless_applying_webhook_update = True
	try:
		for row in affected:
			try:
				frappe.get_doc(row.document_type, row.document_name).remove_tag(mapping.frappe_tag)
			except Exception:
				log_sync_event(
					None,
					settings,
					"Warn",
					"reconcile_tag_changes (CronJob)",
					title=f'Tag "{mapping.frappe_tag}" could not be removed',
					details=f"{row.document_type} {row.document_name}\n\n{frappe.get_traceback()}",
				)
	finally:
		frappe.flags.svan_paperless_applying_webhook_update = False

	log_sync_event(
		None,
		settings,
		"Info",
		"reconcile_tag_changes (CronJob)",
		title="Tag deleted in Paperless NGX removed",
		details=(
			f"Frappe tag: {mapping.frappe_tag} (was Paperless NGX tag ID {mapping.paperless_tag_id})\n"
			f"Affected business documents: {len(affected)}"
		),
	)
	frappe.db.commit()


def _reconcile_custom_field_changes(settings, start_time, max_runtime_seconds, enforce_limits):
	"""The custom field phase of reconcile_metadata_changes() - see the docstring
	there.

	Analogous to _reconcile_tag_changes(): if a mapped, bidirectional
	paperless_custom_field_id is missing from the current Paperless NGX list, the
	custom field counts as deleted globally
	(_clear_custom_field_from_affected_documents()); if the name or data type of an
	ID that still exists differs from the state stored on the mapping row, the
	mapping row itself is brought in line - purely cosmetic (see
	SVANPaperlessCustomFieldMapping; paperless_custom_field_name/_type are only
	needed for creating it again automatically later, see
	custom_field_sync.create_missing_custom_field()) and changing nothing about
	values already synced.
	"""
	try:
		current_by_id = {cf["id"]: cf for cf in fetch_paperless_custom_fields(settings)}
	except Exception:
		log_sync_event(
			None,
			settings,
			"Error",
			"reconcile_metadata_changes (CronJob)",
			title="Paperless NGX custom field list could not be loaded",
			details=frappe.get_traceback(),
		)
		return

	mappings = frappe.get_all(
		"SVAN Paperless Custom Field Mapping",
		filters={"bidirectional": 1, "paperless_custom_field_id": ["!=", 0]},
		fields=[
			"name",
			"paperless_custom_field_id",
			"paperless_custom_field_name",
			"paperless_custom_field_type",
		],
	)

	for mapping in mappings:
		if enforce_limits and time.monotonic() - start_time > max_runtime_seconds:
			log_sync_event(
				None,
				settings,
				"Info",
				"reconcile_metadata_changes (CronJob)",
				title="Runtime limit reached - the scheduled job is stopping in a controlled way",
			)
			return

		current = current_by_id.get(mapping.paperless_custom_field_id)
		if current is None:
			_clear_custom_field_from_affected_documents(mapping, settings)
			continue

		if (
			current["name"] == mapping.paperless_custom_field_name
			and current["data_type"] == mapping.paperless_custom_field_type
		):
			continue

		try:
			mapping_doc = frappe.get_doc("SVAN Paperless Custom Field Mapping", mapping.name)
			mapping_doc.paperless_custom_field_name = current["name"]
			mapping_doc.paperless_custom_field_type = current["data_type"]
			mapping_doc.save(ignore_permissions=True)
		except Exception:
			log_sync_event(
				None,
				settings,
				"Error",
				"reconcile_metadata_changes (CronJob)",
				title="Unexpected error while updating the name/type",
				details=f"SVAN Paperless Custom Field Mapping: {mapping.name}\n\n{frappe.get_traceback()}",
			)
		frappe.db.commit()


def _clear_custom_field_from_affected_documents(mapping, settings):
	"""Clear the value on all business documents whose source field points at a
	custom field deleted globally in Paperless NGX AND which are actually linked to
	an already synced file - called from _reconcile_custom_field_changes() when a
	mapped paperless_custom_field_id no longer appears in the current Paperless NGX
	list.

	Unlike for tags (the "Tag Link" table) or correspondents (the value of the party
	link field), there is no identity value here by which affected records could be
	searched - a custom field value is a freely chosen value (an order number, a
	date, ...), not a reference. The candidates are therefore ALL records of
	source_doctype whose source_fieldname currently carries any value at all
	(["is", "set"], as already used for File.svan_paperless_document_id elsewhere) -
	a field that is already empty does not need clearing again. A field removed or
	renamed on the target DocType in the meantime is skipped (analogous to
	custom_field_sync.get_paperless_custom_field_values_for_document()) instead of
	aborting with "Unknown column".

	Limited to synced files (File.svan_paperless_document_id set) analogous to
	_remove_tag_from_affected_documents()/
	_clear_correspondent_from_affected_documents(), and written with the same
	MAX_SAVE_ATTEMPTS retries per document. The mapping row itself is deliberately
	kept (including its now stale paperless_custom_field_id) - a custom field created
	later under the same name is automatically linked to this row again on the next
	push through the name comparison in
	custom_field_sync.create_missing_custom_field(), analogous to tags.
	"""
	source_rows = frappe.get_all(
		"SVAN Paperless Custom Field Source",
		filters={"parent": mapping.name, "parenttype": "SVAN Paperless Custom Field Mapping"},
		fields=["source_doctype", "source_fieldname"],
	)
	if not source_rows:
		return

	candidates = []
	for row in source_rows:
		if not row.source_doctype or not row.source_fieldname:
			continue
		if not frappe.get_meta(row.source_doctype).get_field(row.source_fieldname):
			continue

		for business_name in frappe.get_all(
			row.source_doctype, filters={row.source_fieldname: ["is", "set"]}, pluck="name"
		):
			candidates.append((row.source_doctype, business_name, row.source_fieldname))

	if not candidates:
		return

	synced_pairs = {
		(row.attached_to_doctype, row.attached_to_name)
		for row in frappe.get_all(
			"File",
			filters={
				"attached_to_doctype": ["in", list({c[0] for c in candidates})],
				"attached_to_name": ["in", list({c[1] for c in candidates})],
				"svan_paperless_document_id": ["is", "set"],
			},
			fields=["attached_to_doctype", "attached_to_name"],
		)
	}
	affected = [c for c in candidates if (c[0], c[1]) in synced_pairs]
	if not affected:
		return

	frappe.flags.svan_paperless_applying_webhook_update = True
	try:
		for business_doctype, business_name, fieldname in affected:
			for attempt in range(1, MAX_SAVE_ATTEMPTS + 1):
				try:
					doc = frappe.get_doc(business_doctype, business_name)
					doc.set(fieldname, "")
					doc.save(ignore_permissions=True)
				except Exception:
					if attempt == MAX_SAVE_ATTEMPTS:
						log_sync_event(
							None,
							settings,
							"Warn",
							"reconcile_metadata_changes (CronJob)",
							title=f"Custom field value could not be cleared after {MAX_SAVE_ATTEMPTS} attempts",
							details=f"{business_doctype}.{fieldname} ({business_name})\n\n{frappe.get_traceback()}",
						)
					continue
				else:
					break
	finally:
		frappe.flags.svan_paperless_applying_webhook_update = False

	log_sync_event(
		None,
		settings,
		"Info",
		"reconcile_metadata_changes (CronJob)",
		title="Value of a custom field deleted in Paperless NGX cleared",
		details=(
			f"Paperless custom field ID: {mapping.paperless_custom_field_id} ({mapping.paperless_custom_field_name})\n"
			f"Affected business documents: {len(affected)}"
		),
	)
	frappe.db.commit()


def query_task_status(file_doc, settings):
	"""Query the Paperless NGX task status for file_doc.svan_paperless_uuid
	(GET /api/tasks/?task_id=<uuid>).

	Returns the Paperless NGX document ID for a task that completed successfully
	(status "success", compared case-insensitively - see the comment at task_status
	further down), otherwise None. A task that is still running
	("pending"/"started") is skipped silently - the next scheduled run queries
	again. A failed task ("failure") or a success without a document ID puts the
	file back to "pending" for a new upload (_queue_for_new_upload()) - the cause
	(Tika, Gotenberg, OCR setup, ...) is presumably solvable on the Paperless NGX side.
	Only known unsolvable failures mark it "broken": UNSOLVABLE_TASK_ERROR_RE and a
	rejection as a duplicate (PAPERLESS_CONSUMER_DELETE_DUPLICATES=true in Paperless
	NGX): every file needs a document of its own, so that setting has to be off.

	No Accept version header is sent DELIBERATELY; the default version configured by
	Paperless NGX is used instead (currently 10) - a project decision: the
	maintenance effort of a pinned version (checking/updating it regularly) is
	deliberately avoided, a future format change would at worst show up visibly as an
	error in the sync log. Only the current format is evaluated (API v10: a paginated
	task list with "results", the field "related_document_ids", structured
	"result_data") - an older Paperless NGX version (the v9 format) is deliberately
	not supported, as this project is only operated against current Paperless NGX
	versions.
	"""
	token = settings.get_password("api_token", raise_exception=False)
	url = settings.api_url.rstrip("/") + "/tasks/"
	headers = {"Authorization": f"Token {token}"}

	try:
		response = requests.get(
			url,
			headers=headers,
			params={"task_id": file_doc.svan_paperless_uuid},
			timeout=settings.api_call_timeout or 30,
			verify=True,
		)
	except Exception:
		log_sync_event(
			file_doc.name,
			settings,
			"Error",
			"query_task_status",
			title="Request failed",
			details=frappe.get_traceback(),
		)
		return None

	if response.status_code != 200:
		log_sync_event(
			file_doc.name,
			settings,
			"Error",
			"query_task_status",
			title=f"Response: HTTP {response.status_code}",
			details=response.text,
		)
		return None

	try:
		payload = response.json()
	except Exception:
		log_sync_event(
			file_doc.name,
			settings,
			"Error",
			"query_task_status",
			title="Response was not valid JSON",
			details=f"HTTP {response.status_code}\n\n{frappe.get_traceback()}",
		)
		return None

	tasks = payload.get("results", [])

	if not tasks:
		# Can happen shortly after the upload, for example, before Paperless NGX
		# has registered the task - not an error, simply checked again on the next
		# run.
		log_sync_event(
			file_doc.name,
			settings,
			"Warn",
			"query_task_status",
			title="Task not found - will be checked again on the next run",
			details=f"UUID: {file_doc.svan_paperless_uuid}",
		)
		return None

	task = tasks[0]
	# Paperless NGX returns lower-case status values, its v9 compatibility format upper-case
	# ones; normalized so either matches.
	task_status = (task.get("status") or "").lower()

	if task_status in ("pending", "started"):
		return None

	if task_status == "success":
		related_document_ids = task.get("related_document_ids") or []
		if related_document_ids:
			return related_document_ids[0]
		log_sync_event(
			file_doc.name,
			settings,
			"Warn",
			"query_task_status",
			title="Task successful, but no document ID in the response - uploading again",
			details=frappe.as_json(task),
		)
		_queue_for_new_upload(file_doc)
		return None

	result_data = task.get("result_data") or {}
	duplicate_of = result_data.get("duplicate_of")

	if duplicate_of:
		log_sync_event(
			file_doc.name,
			settings,
			"Error",
			"query_task_status",
			title="Rejected by Paperless NGX as a duplicate - marked as 'broken'",
			details=(
				f"Duplicate of Paperless NGX document #{duplicate_of}. Every file needs a document of "
				"its own: set PAPERLESS_CONSUMER_DELETE_DUPLICATES to false in Paperless NGX."
			),
		)
		file_doc.db_set("svan_paperless_sync_status", "broken", update_modified=False)
		return None

	details = frappe.as_json(result_data or task)

	if UNSOLVABLE_TASK_ERROR_RE.search(result_data.get("error_message") or ""):
		log_sync_event(
			file_doc.name,
			settings,
			"Error",
			"query_task_status",
			title=f"Task failed (status: {task_status}) - file cannot be processed, marked as 'broken'",
			details=details,
		)
		file_doc.db_set("svan_paperless_sync_status", "broken", update_modified=False)
		return None

	log_sync_event(
		file_doc.name,
		settings,
		"Warn",
		"query_task_status",
		title=f"Task failed (status: {task_status}) - uploading again",
		details=details,
	)
	_queue_for_new_upload(file_doc)
	return None


def _queue_for_new_upload(file_doc):
	"""Back to "pending" without task UUID and document ID: the next scheduled run
	uploads the file again. Safe after a failed task or a vanished document - a
	document that may still exist is adopted by its checksum (see
	_adopt_existing_document()), so nothing gets duplicated. The content is still
	local, offloading only happens in finalize_sync().
	"""
	file_doc.db_set(
		{
			"svan_paperless_uuid": "",
			"svan_paperless_document_id": "",
			"svan_paperless_sync_status": "pending",
		},
		update_modified=False,
	)


def finalize_sync(file_doc, settings, delete_physical_file=None):
	"""Complete the sync as soon as svan_paperless_document_id is known: optionally
	switches the physical file over to the internal preview route and marks it as
	"synced".

	delete_physical_file determines the behaviour and defaults (None) to
	settings.delete_physical_file at the time of the call - it can be passed
	explicitly to decide independently of the current setting (see
	retroactively_delete_physical_file(), which uses it to switch a single, already
	"synced" file to "deleted" retroactively without regard to the global setting):

	- Enabled: the physical file in ERPNext is deleted and file_url switched to the
	  internal preview route (see get_preview_url() in preview.py) - that route
	  streams the document content server-side from Paperless NGX through the API
	  token on demand. The File RECORD itself is kept, only the locally stored file
	  content is removed.
	- Disabled (the default, the previous behaviour): the physical file stays
	  untouched and file_url still points at the local file - there is nothing else
	  to do besides setting the sync status.

	file_doc._delete_file_on_disk() respects Frappe's own deduplication by
	content_hash: if several File records share the same physical file content, only
	the thumbnail is deleted rather than the content itself. As soon as
	file_doc.file_url points at the internal preview route (prefix "/api/method/"),
	Frappe automatically treats the file as a remote file (is_remote_file, see
	URL_PREFIXES in frappe/core/doctype/file/file.py) - the same branch that files
	uploaded by users through "Attach URL" go through. Frappe's own core validation
	(validate_file_path/-url/-on_disk) therefore skips remote files automatically,
	and no special handling is needed.

	Like save_uuid(), it tries up to MAX_SAVE_ATTEMPTS times to reload and save the
	File document. If all attempts fail, "broken" is forced through db_set/set_value
	finally: with the deletion option enabled the physical file is already gone at
	that point, so another automatic attempt in the next scheduled run would have no
	local file content to work with.
	"""
	if delete_physical_file is None:
		delete_physical_file = bool(settings.delete_physical_file)

	if delete_physical_file:
		try:
			file_doc._delete_file_on_disk()
		except Exception:
			log_sync_event(
				file_doc.name,
				settings,
				"Error",
				"finalize_sync",
				title="Physical file could not be deleted",
				details=frappe.get_traceback(),
			)
			return

	for attempt in range(1, MAX_SAVE_ATTEMPTS + 1):
		doc = frappe.get_doc("File", file_doc.name)
		try:
			doc.svan_paperless_sync_status = "synced"
			if delete_physical_file:
				doc.file_url = get_preview_url(doc)
			doc.save(ignore_permissions=True)
		except Exception:
			if attempt == MAX_SAVE_ATTEMPTS:
				values = {"svan_paperless_sync_status": "broken"}
				if delete_physical_file:
					values["file_url"] = get_preview_url(file_doc)
				frappe.db.set_value("File", file_doc.name, values, update_modified=False)
				log_sync_event(
					file_doc.name,
					settings,
					"Error",
					"finalize_sync",
					title=f"Saving failed after {MAX_SAVE_ATTEMPTS} attempts - marked as 'broken'",
					details=frappe.get_traceback(),
				)
			continue
		else:
			log_sync_event(
				file_doc.name,
				settings,
				"Info",
				"finalize_sync",
				title=("Physical file replaced - sync complete" if delete_physical_file else "Sync complete"),
				details=(f"Preview route: {get_preview_url(doc)}" if delete_physical_file else None),
			)
			return


def _document_still_exists(file_name, document_id, settings):
	"""Check through the service token whether a Paperless NGX document still
	exists - called from process_syncing_file() immediately before finalize_sync()
	to detect a race between a successful upload task and a deletion in Paperless
	NGX in the meantime: the task status stays "success" with the original document
	ID even after the document has been deleted (Paperless NGX does not clean that
	up afterwards), so the file would otherwise wrongly be marked as "synced".

	Unlike its namesake in paperless_delete.py (where a failure protects a deletion
	and is therefore fail-closed through frappe.throw()), this is deliberately NOT
	fail-closed: a network error/an unexpected response counts as "exists" and the
	file is synced as before - an actual 404 later is detected by
	push_metadata_update()/preview.stream() anyway and marked as "broken" then.
	"""
	token = settings.get_password("api_token", raise_exception=False)
	url = settings.api_url.rstrip("/") + f"/documents/{document_id}/"

	try:
		response = requests.get(
			url,
			headers={"Authorization": f"Token {token}"},
			timeout=settings.api_call_timeout or 30,
			verify=True,
		)
	except Exception:
		log_sync_event(
			file_name,
			settings,
			"Warn",
			"process_syncing_file",
			title="Error during the existence check in Paperless NGX - assumed to exist",
			details=frappe.get_traceback(),
		)
		return True

	return response.status_code != 404


def process_syncing_file(file_name, settings):
	"""Process a single "syncing" file: queries the Paperless NGX task status if
	needed and completes the sync afterwards. Called from poll_syncing_files().

	If svan_paperless_document_id is already set, a previous finalize_sync() call
	had failed (e.g. a save error after MAX_SAVE_ATTEMPTS) - the task query is then
	skipped and the already known document ID used directly, as another task query
	would return nothing new.

	A document ID that another File already owns is never taken over: another File
	adopted the document in between (see _adopt_existing_document()), so this file
	is uploaded again as a document of its own.

	In both cases it additionally checks through _document_still_exists()
	immediately before finalize_sync() whether the document still exists in
	Paperless NGX - on a 404 the file is uploaded again instead of marked "synced".
	"""
	file_doc = frappe.get_doc("File", file_name)

	if not file_doc.svan_paperless_document_id:
		document_id = query_task_status(file_doc, settings)
		if not document_id:
			return
		if is_document_id_held_by_other_file(document_id, file_name):
			_requeue_for_own_document(file_doc, document_id, settings)
			return
		file_doc.db_set("svan_paperless_document_id", document_id, update_modified=False)

	document_id = file_doc.svan_paperless_document_id
	if not _document_still_exists(file_name, document_id, settings):
		_queue_for_new_upload(file_doc)
		log_sync_event(
			file_name,
			settings,
			"Warn",
			"process_syncing_file",
			title="Document no longer exists in Paperless NGX - uploading again",
			details=f"Document ID: {document_id}",
		)
		return

	finalize_sync(file_doc, settings)


def _requeue_for_own_document(file_doc, document_id, settings):
	"""Release a "syncing" file whose uploaded document another File owns by now:
	clear the task UUID and queue it for a new upload. Its content is still local -
	offloading only happens in finalize_sync().
	"""
	_queue_for_new_upload(file_doc)
	log_sync_event(
		file_doc.name,
		settings,
		"Warn",
		"process_syncing_file",
		title="Document already owned by another file - uploading again",
		details=f"Paperless NGX document #{document_id} belongs to another File. This file is queued for a new upload.",
	)


def poll_syncing_files(enforce_limits=True):
	"""Scheduled task: queries the processing status of the corresponding Paperless
	NGX task for all files with sync status "syncing" and completes the sync on
	success (see process_syncing_file()).

	Runs as a scheduled job of its own, independently of sync_pending_files(): files
	with status "syncing" already have a UUID and are therefore no longer touched by
	sync_pending_files() (which filters on status == "pending").

	With enforce_limits=True (the default, as the scheduler calls this function) it
	limits itself to settings.max_runtime_seconds seconds of total runtime, like
	sync_pending_files(), to avoid the scheduler's forced termination after 300
	seconds (RQ queue "default"). Unlike sync_pending_files() there is deliberately
	no additional max_files_per_run limit here: that field is documented explicitly
	for the upload job, and the lightweight task status API call per file here is far
	cheaper than a full file upload.
	"""
	start_time = time.monotonic()
	settings = frappe.get_single("SVAN Paperless Settings")

	try:
		validate_settings(settings)
	except Exception:
		# validate_settings() already records an incomplete configuration in the
		# sync log itself before raising through frappe.throw().
		return

	max_runtime_seconds = settings.max_runtime_seconds or 240

	file_names = frappe.get_all(
		"File",
		filters={"svan_paperless_sync_status": "syncing", "is_folder": 0},
		pluck="name",
		order_by="creation asc",
	)

	for file_name in file_names:
		if enforce_limits and time.monotonic() - start_time > max_runtime_seconds:
			log_sync_event(
				None,
				settings,
				"Info",
				"poll_syncing_files (CronJob)",
				title="Runtime limit reached - the scheduled job is stopping in a controlled way",
				details=(
					f"Runtime budget of {max_runtime_seconds}s reached. Remaining "
					"files stay 'syncing' and are processed in the next scheduled "
					"run."
				),
			)
			break

		try:
			process_syncing_file(file_name, settings)
		except Exception:
			log_sync_event(
				file_name,
				settings,
				"Error",
				"poll_syncing_files (CronJob)",
				title="Unexpected error during the status query",
				details=frappe.get_traceback(),
			)

		frappe.db.commit()


def retroactively_delete_physical_file(file_name, settings):
	"""Delete the physical file of a single, already "synced" file retroactively,
	provided its original has been kept so far (file_url does not point at the
	internal preview route yet) - the core of
	retroactively_delete_physical_files().

	Calls finalize_sync() with delete_physical_file forced to True rather than
	implementing the deletion/save logic a second time: the same robust retry logic
	(MAX_SAVE_ATTEMPTS, "broken" fallback) therefore applies here as well,
	independently of the current value of settings.delete_physical_file.
	"""
	file_doc = frappe.get_doc("File", file_name)

	if is_preview_url(file_doc.file_url):
		return

	finalize_sync(file_doc, settings, delete_physical_file=True)


def retroactively_delete_physical_files():
	"""Ad-hoc run: retroactively deletes the physical file for all already "synced"
	files whose original was KEPT when the sync was originally completed
	(delete_physical_file was disabled at that time, see finalize_sync()) -
	recognizable by file_url NOT pointing at the internal preview route yet (see
	is_preview_url() in preview.py). The manual trigger for the "Delete Physical
	Files Retroactively" button in SVAN Paperless Settings.

	Deliberately selects by the file's own state (file_url still local), NOT by the
	current value of delete_physical_file - regardless of how the setting currently
	stands, the run always covers exactly those files that still have their local
	content.

	Processes file by file with its own try/except (a failure on one file does not
	abort the run) and commits individually, analogous to poll_syncing_files().
	Clicking the button again after a partial failure is safe:
	file_doc._delete_file_on_disk() is a no-op when the physical file is already
	gone.
	"""
	settings = frappe.get_single("SVAN Paperless Settings")

	file_names = frappe.get_all(
		"File",
		filters={
			"svan_paperless_sync_status": "synced",
			"file_url": ["not like", f"/api/method/{PREVIEW_METHOD_PATH}%"],
			"is_folder": 0,
		},
		pluck="name",
	)

	for file_name in file_names:
		try:
			retroactively_delete_physical_file(file_name, settings)
		except Exception:
			log_sync_event(
				file_name,
				settings,
				"Error",
				"retroactively_delete_physical_files",
				title="Retroactive deletion of the physical file failed",
				details=frappe.get_traceback(),
			)

		frappe.db.commit()


@frappe.whitelist()
def trigger_manual_sync():
	"""Manual ad-hoc trigger for the button in SVAN Paperless Settings.

	Runs as a background job, as up to settings.api_call_timeout seconds of API
	timeout can occur per file. Deliberately calls with enforce_limits=False: the
	runtime/batch limits of sync_pending_files() only apply to the automatic
	scheduled job - the manual button is meant to do exactly "sync everything now"
	and runs in the "long" queue with a 1500 second timeout.
	"""
	frappe.only_for("System Manager")
	frappe.enqueue(
		"svan_paperless_bridge.paperless_sync.sync_pending_files",
		queue="long",
		job_name="svan_paperless_manual_sync",
		enforce_limits=False,
	)


@frappe.whitelist()
def trigger_manual_metadata_sync():
	"""Manual ad-hoc trigger for the "Update Metadata Now" button in SVAN Paperless
	Settings.

	Runs as a background job, analogous to trigger_manual_sync(): deliberately calls
	with enforce_limits=False, as the runtime limit of sync_metadata_updates() only
	applies to the automatic cron job - the manual button is meant to process all
	queued metadata updates right now and runs in the "long" queue with a 1500 second
	timeout.
	"""
	frappe.only_for("System Manager")
	frappe.enqueue(
		"svan_paperless_bridge.paperless_sync.sync_metadata_updates",
		queue="long",
		job_name="svan_paperless_manual_metadata_sync",
		enforce_limits=False,
	)


@frappe.whitelist()
def trigger_manual_poll():
	"""Manual ad-hoc trigger for the "Finalize Pending Transfers" button in SVAN
	Paperless Settings.

	Runs as a background job, analogous to trigger_manual_sync(): up to two API
	calls (task status, share link) with up to settings.api_call_timeout seconds
	each can occur per file. Deliberately calls with enforce_limits=False: the
	runtime limit of poll_syncing_files() only applies to the automatic scheduled
	job - the manual button is meant to do exactly "process all 'syncing' files now"
	and runs in the "long" queue with a 1500 second timeout.
	"""
	frappe.only_for("System Manager")
	frappe.enqueue(
		"svan_paperless_bridge.paperless_sync.poll_syncing_files",
		queue="long",
		job_name="svan_paperless_manual_poll",
		enforce_limits=False,
	)


@frappe.whitelist()
def trigger_retroactive_delete_physical_files():
	"""Manual ad-hoc trigger for the "Delete Physical Files Retroactively" button
	in SVAN Paperless Settings.

	Runs as a background job in the "long" queue: physical deletions plus a .save()
	per affected file can take noticeable time with many files.
	"""
	frappe.only_for("System Manager")
	frappe.enqueue(
		"svan_paperless_bridge.paperless_sync.retroactively_delete_physical_files",
		queue="long",
		job_name="svan_paperless_retroactive_delete",
	)


@frappe.whitelist()
def trigger_manual_reconcile():
	"""Manual ad-hoc trigger for the "Classify Now" button in SVAN Paperless
	Settings.

	Triggers reconcile_pending_files() only, WITHOUT syncing - useful to see right
	after a whitelist change which files now count as "pending", without waiting for
	the next scheduled run or a full sync. Runs as a background job, as the request
	would otherwise block when there are very many files to check.
	"""
	frappe.only_for("System Manager")
	frappe.enqueue(
		"svan_paperless_bridge.paperless_sync.reconcile_pending_files",
		queue="short",
		job_name="svan_paperless_manual_reconcile",
	)


@frappe.whitelist()
def trigger_manual_metadata_reconcile():
	"""Manual ad-hoc trigger for the "Reconcile Global Metadata Changes" button in
	SVAN Paperless Settings.

	Triggers reconcile_metadata_changes() right away (enforce_limits=False - the
	manual button should not be cut short by the runtime limit intended for the
	scheduled job) instead of waiting for the next scheduled run. Runs as a
	background job, as the request would otherwise block when many documents are
	affected.
	"""
	frappe.only_for("System Manager")
	frappe.enqueue(
		"svan_paperless_bridge.paperless_sync.reconcile_metadata_changes",
		queue="short",
		job_name="svan_paperless_manual_metadata_reconcile",
		enforce_limits=False,
	)


@frappe.whitelist()
def reset_broken_files():
	"""Manual ad-hoc trigger for the "Reset Broken" button in SVAN Paperless
	Settings.

	Resets all files with sync status "broken" to "" so that they are classified
	against the current whitelist again on the next reconcile run and - if relevant -
	synced again. svan_paperless_config_version_checked, the task UUID and the
	document ID are reset along with it: reconcile_file_classification() skips broken
	files permanently and only considers files without UUID and document ID, so
	they would otherwise never be classified again. A document that still exists is
	adopted by its checksum on the new upload (see _adopt_existing_document()). Runs
	synchronously (a single UPDATE, no API access), so no background job is needed.
	"""
	frappe.only_for("System Manager")

	broken_files = frappe.get_all("File", filters={"svan_paperless_sync_status": "broken"}, pluck="name")

	if not broken_files:
		return 0

	frappe.db.sql(
		"""
		UPDATE `tabFile`
		SET svan_paperless_sync_status = '',
		    svan_paperless_uuid = '',
		    svan_paperless_document_id = '',
		    svan_paperless_needs_update = 0,
		    svan_paperless_config_version_checked = 0
		WHERE svan_paperless_sync_status = 'broken'
		"""
	)
	frappe.db.commit()

	log_sync_event(
		None,
		frappe.get_single("SVAN Paperless Settings"),
		"Info",
		"reset_broken_files (Manual)",
		title="Broken status of all files reset",
		details=f"{len(broken_files)} file(s) reset from 'broken' to empty.",
	)

	return len(broken_files)
