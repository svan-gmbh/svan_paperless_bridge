# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

"""The linked files of whitelisted DocTypes are compared with their Paperless NGX
documents: files whose document or version was deleted there are deleted in ERPNext
as well (a document in the trash counts as deleted), new versions are imported
(version_import.py) and changed names taken over (name_sync.py).

Paperless NGX fires nothing when a document is deleted or a document or version is
restored from the trash, so a scheduled job compares all of them in batches.
Deleting or adding a version fires "Document Updated" for its document; the
webhook then handles that document at once. Paperless NGX is not touched again: if
a document came back from the trash in the meantime, it stays there, and its next
change imports it again.
"""

import time

import frappe
import requests

from svan_paperless_bridge.file_versions import own_version_id
from svan_paperless_bridge.name_sync import apply_paperless_names
from svan_paperless_bridge.paperless_delete import keeping_paperless_documents
from svan_paperless_bridge.paperless_import import IMPORT_DOCTYPE
from svan_paperless_bridge.svan_paperless_ngx_bridge.doctype.svan_paperless_settings.svan_paperless_settings import (
	validate_settings,
)
from svan_paperless_bridge.utils.sync_log import log_sync_event
from svan_paperless_bridge.utils.whitelist import whitelisted_filter
from svan_paperless_bridge.version_import import import_new_versions

# "broken" too: the preview and the metadata push mark a file so when its document is gone.
CHECKED_STATUSES = ("synced", "broken")
BATCH_SIZE = 100
FILE_FIELDS = ["name", "svan_paperless_document_id", "svan_paperless_version_id"]
# original_file_name: the fallback name of an imported version; title: the name of the original.
DOCUMENT_FIELDS = "id,title,versions,original_file_name"

CRON_SOURCE = "compare_with_paperless (CronJob)"
WEBHOOK_SOURCE = "webhook_sync.receive_update"


def compare_with_paperless(enforce_limits=True):
	"""Scheduled task: delete the files whose document or version no longer exists
	in Paperless NGX, import the versions added there and take over changed names.
	"""
	start_time = time.monotonic()
	settings = frappe.get_single("SVAN Paperless Settings")
	try:
		validate_settings(settings)
	except Exception:
		return

	files_by_document = {}
	for file_row in frappe.get_all(
		"File",
		filters={
			"svan_paperless_sync_status": ["in", CHECKED_STATUSES],
			"svan_paperless_document_id": ["is", "set"],
			"attached_to_doctype": whitelisted_filter(settings),
			"is_folder": 0,
		},
		fields=FILE_FIELDS,
		order_by="creation asc",
	):
		files_by_document.setdefault(str(file_row.svan_paperless_document_id), []).append(file_row)

	document_ids = list(files_by_document)
	max_runtime_seconds = settings.max_runtime_seconds or 240
	for offset in range(0, len(document_ids), BATCH_SIZE):
		if enforce_limits and time.monotonic() - start_time > max_runtime_seconds:
			log_sync_event(
				None,
				settings,
				"Info",
				CRON_SOURCE,
				title="Runtime limit reached - the scheduled job is stopping in a controlled way",
				details="Remaining files are checked in the next scheduled run.",
			)
			break

		batch = document_ids[offset : offset + BATCH_SIZE]
		documents = _fetch_existing_documents(batch, settings)
		if documents is None:
			continue
		for document_id in batch:
			document = documents.get(document_id)
			_delete_files_without_content(
				files_by_document[document_id], document_id, document, settings, CRON_SOURCE
			)
			if document is not None:
				import_new_versions(document, settings, CRON_SOURCE)
				apply_paperless_names(document, settings, CRON_SOURCE)


@frappe.whitelist()
def trigger_manual_compare():
	"""The "Compare With Paperless NGX Now" button in the Settings: runs the job at
	once and without its runtime limit.
	"""
	frappe.only_for("System Manager")
	frappe.enqueue(
		"svan_paperless_bridge.paperless_compare.compare_with_paperless",
		queue="long",
		job_name="svan_paperless_manual_compare",
		enforce_limits=False,
	)


def delete_files_of_deleted_versions(document, settings):
	"""Webhook path: delete the files whose version the document no longer has.
	Returns True if any were deleted.
	"""
	files = frappe.get_all(
		"File",
		filters={
			"svan_paperless_document_id": str(document["id"]),
			"svan_paperless_sync_status": ["in", CHECKED_STATUSES],
			"attached_to_doctype": whitelisted_filter(settings),
		},
		fields=FILE_FIELDS,
	)
	return _delete_files_without_content(files, str(document["id"]), document, settings, WEBHOOK_SOURCE)


def _fetch_existing_documents(document_ids, settings):
	"""The still existing documents of document_ids by ID, or None if the answer
	cannot be trusted (then nothing is deleted).
	"""
	try:
		response = requests.get(
			settings.api_url.rstrip("/") + "/documents/",
			headers={"Authorization": f"Token {settings.get_password('api_token', raise_exception=False)}"},
			params={"id__in": ",".join(document_ids), "fields": DOCUMENT_FIELDS, "page_size": BATCH_SIZE},
			timeout=settings.api_call_timeout or 30,
			verify=True,
		)
		response.raise_for_status()
		payload = response.json()
		documents = {str(document["id"]): document for document in payload["results"]}
	except Exception:
		log_sync_event(
			None,
			settings,
			"Error",
			CRON_SOURCE,
			title="Checking the documents in Paperless NGX failed - nothing deleted",
			details=frappe.get_traceback(),
		)
		return None

	# Paperless NGX ignores an unknown filter and lists all documents instead.
	if payload.get("next") or not documents.keys() <= set(document_ids):
		log_sync_event(
			None,
			settings,
			"Error",
			CRON_SOURCE,
			title="Paperless NGX listed other documents than requested - nothing deleted",
			details=f"Requested: {', '.join(document_ids)}\nListed: {', '.join(documents)}",
		)
		return None
	return documents


def _delete_files_without_content(files, document_id, document, settings, source):
	"""Delete the files of one document whose content Paperless NGX no longer has:
	all of them without document (None), otherwise those of a deleted version.
	"""
	if document is None:
		missing, title = files, "Document deleted in Paperless NGX - file deleted"
	elif "versions" not in document:
		return False
	else:
		version_ids = {document_id} | {str(version["id"]) for version in document["versions"]}
		missing = [f for f in files if str(own_version_id(f)) not in version_ids]
		title = "Version deleted in Paperless NGX - file deleted"

	for file_row in missing:
		log_sync_event(
			file_row.name,
			settings,
			"Info",
			source,
			title=title,
			details=(
				f"Paperless NGX document ID: {document_id}\n"
				f"Version ID: {own_version_id(file_row)}\n"
				"The file was deleted in ERPNext."
			),
		)
		try:
			with keeping_paperless_documents():
				frappe.delete_doc("File", file_row.name, ignore_permissions=True)
		except Exception:
			frappe.db.rollback()
			log_sync_event(
				file_row.name,
				settings,
				"Warn",
				source,
				title="Deleting the file failed - will be retried on the next run",
				details=frappe.get_traceback(),
			)
		frappe.db.commit()

	if document is None and missing:
		# An imported document restored from the trash is imported again.
		frappe.db.delete(IMPORT_DOCTYPE, {"name": document_id})
		frappe.db.commit()
	return bool(missing)
