# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

"""Files created in Paperless NGX are attached to their business document.

A Paperless NGX document qualifies when its document type belongs to a whitelisted
DocType that the "Reference For Import" of the Settings (a custom field mapping) has
a source on, the reference has a value and, if an import tag is set, the document
carries it (see import_rules.py). The reference value then has to match exactly one
business document, and while the archive is on, its title must not carry the
archive mark (archive.py). Candidates come from the webhook (an update of a document no File
holds) and from import_from_paperless() once a day or by button, which only looks at
documents changed since its last run. They are queued in "SVAN Paperless Import", an
internal work table.

An imported file is an ordinary synced file afterwards: the Paperless tags are
added to the business document (except the import tags), then Frappe wins and its
metadata is pushed to the document. Changed documents that already have files are
handed to document_relink.py, which moves or detaches them.
"""

import time

import frappe
import requests

from svan_paperless_bridge.archive import is_archived
from svan_paperless_bridge.document_relink import DETACHED, MOVED, relink_document
from svan_paperless_bridge.import_rules import ImportRules, find_business_documents
from svan_paperless_bridge.name_sync import file_name_from, remember_imported_names
from svan_paperless_bridge.paperless_sync import OUTCOME_OK, finalize_sync, push_metadata_update
from svan_paperless_bridge.svan_paperless_ngx_bridge.doctype.svan_paperless_settings.svan_paperless_settings import (
	DEFAULT_MAX_IMPORT_ATTEMPTS,
	validate_settings,
)
from svan_paperless_bridge.tag_sync import ensure_frappe_tag_for_paperless_id
from svan_paperless_bridge.utils.sync_log import log_sync_event

IMPORT_DOCTYPE = "SVAN Paperless Import"
SETTINGS_DOCTYPE = "SVAN Paperless Settings"
IMPORT_SOURCE = "paperless"

PENDING = "pending"
IMPORTED = "imported"
NOT_FOUND = "not_found"
AMBIGUOUS = "ambiguous"
FAILED = "failed"
# A change of the document in Paperless NGX starts these over; AMBIGUOUS needs the reset button.
RESTARTABLE = (NOT_FOUND, FAILED)

# The list endpoint would otherwise return the full OCR text of every document.
SCAN_FIELDS = "id,title,document_type,custom_fields,tags,modified"


def import_from_paperless(enforce_limits=True):
	"""Scheduled task: move or detach the linked files of the documents changed in
	Paperless NGX since the last run, queue the unlinked ones that qualify, then work
	through the queue. Like the other jobs it respects max_runtime_seconds unless
	started by the button.
	"""
	start_time = time.monotonic()
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	try:
		validate_settings(settings)
	except Exception:
		return

	rules = ImportRules(settings)
	if not rules:
		return

	try:
		_scan_changed_documents(rules, settings)
	except Exception:
		log_sync_event(
			None,
			settings,
			"Error",
			"import_from_paperless (CronJob)",
			title="Scan for files to import failed",
			details=frappe.get_traceback(),
		)

	_process_queue(rules, settings, start_time, enforce_limits)


def import_unlinked_document(document_id, settings):
	"""Webhook path for a document no File holds. Returns False if it does not
	qualify for an import.
	"""
	rules = ImportRules(settings)
	if not rules:
		return False

	document = _fetch_document(document_id, settings)
	if not document or not _qualifies(document, rules, settings):
		return False

	_queue(document_id)
	frappe.db.commit()
	process_import(document_id, rules, settings, document)
	frappe.db.commit()
	return True


def relink_linked_document(document_id, settings, document=None):
	"""Webhook path for a document that has files: move or detach them. Returns
	True if it did, so the webhook does not apply the document to a business
	document it no longer belongs to.
	"""
	rules = ImportRules(settings)
	if not rules:
		return False

	document = document or _fetch_document(document_id, settings)
	if not document:
		return False
	return _relink(document, rules, settings) in (MOVED, DETACHED)


def process_import(document_id, rules, settings, document=None):
	"""Look for the business document of one queued Paperless NGX document and
	attach the file to it. The queue row is locked, so the webhook and the
	scheduled job never import the same document twice.
	"""
	row = frappe.db.get_value(
		IMPORT_DOCTYPE, str(document_id), ["name", "status", "attempts"], as_dict=True, for_update=True
	)
	if not row or row.status != PENDING:
		return

	if frappe.db.exists("File", {"svan_paperless_document_id": str(document_id)}):
		_update_row(row, IMPORTED, details="Linked to a file of ERPNext in the meantime.")
		return

	document = document or _fetch_document(document_id, settings)
	if document is None:
		return
	matches = _qualifies(document, rules, settings)
	if not matches:
		frappe.delete_doc(IMPORT_DOCTYPE, row.name, ignore_permissions=True)
		return

	business_documents = find_business_documents(matches)
	attempts = (row.attempts or 0) + 1
	references = ", ".join(f"{rule.doctype}.{rule.source_fieldname} = {value}" for rule, value in matches)

	if len(business_documents) > 1:
		found = ", ".join(f"{doctype} {name}" for doctype, name in business_documents)
		_update_row(row, AMBIGUOUS, attempts, details=f"{references}\nMatches: {found}")
		_log(row, settings, "Warn", "Reference matches several business documents - not imported", found)
		return

	if not business_documents:
		status = NOT_FOUND if attempts >= _max_attempts(settings) else PENDING
		_update_row(row, status, attempts, details=references)
		if status == NOT_FOUND:
			_log(row, settings, "Warn", f"No business document found after {attempts} attempts", references)
		return

	doctype, name = business_documents[0]
	frappe.db.savepoint("svan_paperless_import")
	try:
		file_name = _import_file(document, doctype, name, rules, settings)
	except Exception:
		frappe.db.rollback(save_point="svan_paperless_import")
		status = FAILED if attempts >= _max_attempts(settings) else PENDING
		_update_row(row, status, attempts, details=f"{doctype} {name}\n\n{frappe.get_traceback()}")
		_log(row, settings, "Error", "Import failed", frappe.get_traceback())
		return

	_update_row(row, IMPORTED, attempts, doctype=doctype, name=name, file_name=file_name, details=references)
	log_sync_event(
		file_name,
		settings,
		"Info",
		"paperless_import.process_import",
		title="File imported from Paperless NGX",
		details=f"Paperless NGX document ID: {document_id}\nBusiness document: {doctype} {name}",
	)


@frappe.whitelist()
def trigger_manual_import():
	"""Button "Import From Paperless NGX Now": the scheduled job without limits."""
	frappe.only_for("System Manager")
	frappe.enqueue(
		"svan_paperless_bridge.paperless_import.import_from_paperless",
		queue="long",
		job_name="svan_paperless_manual_import",
		enforce_limits=False,
	)


@frappe.whitelist()
def reset_import_queue():
	"""Button "Reset Import Queue": entries that stopped (not found, ambiguous,
	failed) get new attempts. Returns their number.
	"""
	frappe.only_for("System Manager")
	names = frappe.get_all(
		IMPORT_DOCTYPE, filters={"status": ["in", [NOT_FOUND, AMBIGUOUS, FAILED]]}, pluck="name"
	)
	for name in names:
		frappe.db.set_value(IMPORT_DOCTYPE, name, {"status": PENDING, "attempts": 0})
	return len(names)


def _scan_changed_documents(rules, settings):
	"""Relink or queue the documents changed since the stored watermark (the highest
	"modified" of Paperless NGX seen so far, so the clocks of both systems do not
	matter). All documents are looked at, not only qualifying ones: a linked file
	whose document lost its document type or reference has to be detached. Sorted
	by "modified", so the watermark can be stored after each page.
	"""
	url = settings.api_url.rstrip("/") + "/documents/"
	params = {"ordering": "modified", "fields": SCAN_FIELDS, "page_size": 100}
	if settings.import_scan_watermark:
		params["modified__gt"] = settings.import_scan_watermark

	while url:
		response = requests.get(
			url,
			headers=_headers(settings),
			params=params,
			timeout=settings.api_call_timeout or 30,
			verify=True,
		)
		response.raise_for_status()
		payload = response.json()
		documents = payload.get("results") or []

		page_ids = [str(document["id"]) for document in documents]
		linked_ids = set(
			frappe.get_all(
				"File",
				filters={"svan_paperless_document_id": ["in", page_ids or [""]]},
				pluck="svan_paperless_document_id",
			)
		)
		for document in documents:
			if str(document["id"]) in linked_ids:
				_relink_logged(document, rules, settings)
			elif _qualifies(document, rules, settings):
				_queue(document["id"])

		if documents:
			frappe.db.set_single_value(
				SETTINGS_DOCTYPE, "import_scan_watermark", documents[-1]["modified"], update_modified=False
			)
		frappe.db.commit()

		# The next URL carries the query parameters already.
		url, params = payload.get("next"), None


def _qualifies(document, rules, settings):
	"""rules.matches() of a document no file holds; none while it is archived."""
	if settings.archive_deleted_files and is_archived(document.get("title")):
		return []
	return rules.matches(document)


def _relink_logged(document, rules, settings):
	try:
		_relink(document, rules, settings)
	except Exception:
		frappe.db.rollback()
		log_sync_event(
			None,
			settings,
			"Error",
			"import_from_paperless (CronJob)",
			title="Moving or detaching the linked files failed",
			details=f"Paperless NGX document ID: {document['id']}\n\n{frappe.get_traceback()}",
		)


def _relink(document, rules, settings):
	outcome = relink_document(document, rules, settings)
	if outcome == DETACHED:
		# A later valid reference imports the document again.
		frappe.db.delete(IMPORT_DOCTYPE, {"name": str(document["id"])})
		frappe.db.commit()
	return outcome


def _process_queue(rules, settings, start_time, enforce_limits):
	max_runtime_seconds = settings.max_runtime_seconds or 240
	for document_id in frappe.get_all(
		IMPORT_DOCTYPE, filters={"status": PENDING}, pluck="paperless_document_id", order_by="modified asc"
	):
		if enforce_limits and time.monotonic() - start_time > max_runtime_seconds:
			break
		try:
			process_import(document_id, rules, settings)
		except Exception:
			frappe.db.rollback()
			log_sync_event(
				None,
				settings,
				"Error",
				"import_from_paperless (CronJob)",
				title="Unexpected error during the import",
				details=f"Paperless NGX document ID: {document_id}\n\n{frappe.get_traceback()}",
			)
		frappe.db.commit()


def _queue(document_id):
	"""Add a document to the queue, or give a stopped entry new attempts."""
	name = str(document_id)
	status = frappe.db.get_value(IMPORT_DOCTYPE, name, "status")
	if status in RESTARTABLE:
		frappe.db.set_value(IMPORT_DOCTYPE, name, {"status": PENDING, "attempts": 0})
	elif not status:
		frappe.get_doc({"doctype": IMPORT_DOCTYPE, "paperless_document_id": int(document_id)}).insert(
			ignore_permissions=True, ignore_if_duplicate=True
		)


def _import_file(document, doctype, name, rules, settings):
	"""Create the File from the newest version (the original is the oldest one)
	and complete it like an uploaded one.
	"""
	file_doc = create_version_file(document, _newest_version_id(document), doctype, name, settings)

	tag_ids = [tag_id for tag_id in document.get("tags") or [] if tag_id not in rules.import_tag_ids]
	_add_tags(doctype, name, tag_ids, settings, file_doc.name)
	# Frappe wins: the push replaces tags (dropping the import tag), correspondent and custom fields.
	# Right away, as a pending update would keep the file from following a change in Paperless NGX.
	if push_metadata_update(file_doc.name, settings) != OUTCOME_OK:
		frappe.db.set_value("File", file_doc.name, "svan_paperless_needs_update", 1, update_modified=False)
	return file_doc.name


def create_version_file(document, version_id, doctype, name, settings):
	"""A synced File of the business document holding one version of the document,
	named after it (_version_file_name()). Frappe's suffix for a name that exists on
	disk stays in the path only; the attachment list shows files of the same name once.
	"""
	document_id = str(document["id"])
	file_name = _version_file_name(document, version_id, settings)
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"attached_to_doctype": doctype,
			"attached_to_name": name,
			"is_private": 1,
			"content": _download_version(document_id, version_id, settings),
			"svan_source": IMPORT_SOURCE,
			"svan_paperless_document_id": document_id,
			"svan_paperless_version_id": version_id,
			"svan_paperless_sync_status": "syncing",
			"svan_paperless_config_version_checked": settings.sync_config_version or 0,
		}
	).insert(ignore_permissions=True)
	if file_doc.file_name != file_name:
		# Frappe added a suffix as the name exists on disk; only the path needs it.
		file_doc.db_set("file_name", file_name, update_modified=False)
	remember_imported_names(file_doc, document)
	finalize_sync(file_doc, settings)
	return file_doc


def _newest_version_id(document):
	"""The newest version not archived; the newest one if all are."""
	versions = document.get("versions") or []
	if not versions:
		return str(document["id"])
	versions = [v for v in versions if not is_archived(v.get("version_label"))] or versions
	return str(max(versions, key=lambda v: (v.get("added") or "", v["id"]))["id"])


def _version_file_name(document, version_id, settings):
	"""The version label (the name of a version, see name_sync.py) with the extension
	of the name the version was uploaded with. The download only names the document
	(with the extension of the version); its metadata has the version's own name.
	"""
	document_id = str(document["id"])
	fallback = document.get("original_file_name") or f"{document_id}.pdf"
	try:
		response = requests.get(
			settings.api_url.rstrip("/") + f"/documents/{document_id}/metadata/",
			headers=_headers(settings),
			params={"version": version_id},
			timeout=settings.api_call_timeout or 30,
			verify=True,
		)
		response.raise_for_status()
		uploaded_name = response.json().get("original_filename") or fallback
	except Exception:
		uploaded_name = fallback
	label = next(
		(v.get("version_label") for v in document.get("versions") or [] if str(v["id"]) == str(version_id)),
		None,
	)
	return file_name_from(label, uploaded_name) if label else uploaded_name


def _add_tags(doctype, name, paperless_tag_ids, settings, file_name):
	"""Add the Paperless tags to the business document; nothing is removed."""
	tags = [ensure_frappe_tag_for_paperless_id(file_name, tag_id, settings) for tag_id in paperless_tag_ids]
	business_doc = frappe.get_doc(doctype, name)
	# Not Document.get_tags(): it drops a tag (see tag_sync.get_paperless_tag_ids_for_document()).
	existing = set(
		frappe.get_all("Tag Link", filters={"document_type": doctype, "document_name": name}, pluck="tag")
	)
	for tag in tags:
		if tag and tag not in existing:
			business_doc.add_tag(tag)


def _fetch_document(document_id, settings):
	"""The document, or None if it is gone (the queue entry is then dropped) or
	could not be loaded (logged, retried on the next run).
	"""
	try:
		response = requests.get(
			settings.api_url.rstrip("/") + f"/documents/{document_id}/",
			headers=_headers(settings),
			timeout=settings.api_call_timeout or 30,
			verify=True,
		)
		if response.status_code == 404:
			frappe.delete_doc(IMPORT_DOCTYPE, str(document_id), ignore_permissions=True, ignore_missing=True)
			return None
		response.raise_for_status()
		return response.json()
	except Exception:
		log_sync_event(
			None,
			settings,
			"Error",
			"paperless_import._fetch_document",
			title="Document to import could not be loaded",
			details=f"Paperless NGX document ID: {document_id}\n\n{frappe.get_traceback()}",
		)
		return None


def _download_version(document_id, version_id, settings):
	response = requests.get(
		settings.api_url.rstrip("/") + f"/documents/{document_id}/download/",
		headers=_headers(settings),
		params={"original": "true", "version": version_id},
		timeout=settings.api_call_timeout or 30,
		verify=True,
	)
	response.raise_for_status()
	return response.content


def _update_row(row, status, attempts=None, doctype=None, name=None, file_name=None, details=None):
	values = {"status": status, "last_attempt": frappe.utils.now_datetime(), "details": details}
	if attempts is not None:
		values["attempts"] = attempts
	if file_name:
		values.update({"business_doctype": doctype, "business_name": name, "file": file_name})
	frappe.db.set_value(IMPORT_DOCTYPE, row.name, values)


def _log(row, settings, log_type, title, details):
	log_sync_event(
		None,
		settings,
		log_type,
		"paperless_import.process_import",
		title=title,
		details=f"Paperless NGX document ID: {row.name}\n{details}",
	)


def _max_attempts(settings):
	return settings.max_import_attempts or DEFAULT_MAX_IMPORT_ATTEMPTS


def _headers(settings):
	return {"Authorization": f"Token {settings.get_password('api_token', raise_exception=False)}"}
