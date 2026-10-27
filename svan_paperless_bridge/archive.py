# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

"""While the archive is on, deleting a file in ERPNext never deletes anything in
Paperless NGX. The label of the file's version gets " (archived)", and the title
too once the last file of the document is deleted.

A document without file whose title carries the marker is not imported while the
archive is on (paperless_import.py); a version without file whose label carries it
never (version_import.py), as the comparison would otherwise bring it back on its
own once the archive is switched off. A linked file with the marker is synced like
any other: only the missing link makes it count.
"""

import os

import frappe
import requests

from svan_paperless_bridge.file_versions import own_version_id
from svan_paperless_bridge.name_sync import LABEL_MAX_LENGTH, TITLE_MAX_LENGTH
from svan_paperless_bridge.utils.sync_log import log_sync_event_after_rollback

MARKER = "(archived)"
# Marks written in the open transaction, taken back if it is rolled back.
ARCHIVED_FLAG = "svan_paperless_archived_marks"


def is_archived(value):
	return MARKER in (value or "").lower()


def archived_name(value, file_name, max_length):
	"""value with the marker before the file's extension, shortened before it if
	needed. An empty value (e.g. a version label never set) takes the file name.
	"""
	value = value or file_name
	if is_archived(value):
		return value
	extension = os.path.splitext(file_name)[1]
	if extension and value.lower().endswith(extension.lower()):
		stem = value[: -len(extension)]
	else:
		stem, extension = value, ""
	suffix = f" {MARKER}{extension}"
	return stem[: max_length - len(suffix)].rstrip() + suffix


def archive_file(file_doc, settings, whole_document):
	"""Mark the version of file_doc and, with whole_document, the title. Raises if
	Paperless NGX fails; a document already gone needs no mark.
	"""
	document_id = str(file_doc.svan_paperless_document_id)
	version_id = str(own_version_id(file_doc))
	response = _request("get", f"/documents/{document_id}/", settings)
	if response.status_code == 404:
		return False
	response.raise_for_status()
	document = response.json()

	version = next((v for v in document.get("versions") or [] if str(v["id"]) == version_id), None)
	if version is not None:
		_mark(
			f"/documents/{document_id}/versions/{version_id}/",
			"version_label",
			version.get("version_label") or "",
			archived_name(version.get("version_label"), file_doc.file_name, LABEL_MAX_LENGTH),
			settings,
		)
	if whole_document:
		_mark(
			f"/documents/{document_id}/",
			"title",
			document.get("title") or "",
			archived_name(document.get("title"), file_doc.file_name, TITLE_MAX_LENGTH),
			settings,
		)
	return True


def _mark(path, field, previous, value, settings):
	if value == previous:
		return
	_request("patch", path, settings, json={field: value}).raise_for_status()

	marks = frappe.flags.get(ARCHIVED_FLAG)
	if marks is None:
		marks = frappe.flags[ARCHIVED_FLAG] = []
		frappe.db.after_rollback.add(lambda: _undo_marks(settings))
		frappe.db.after_commit.add(lambda: frappe.flags.pop(ARCHIVED_FLAG, None))
	marks.append((path, field, previous))


def _undo_marks(settings):
	"""The deletion was rolled back: the files are still linked, and their names would
	otherwise take the marker over from Paperless NGX.
	"""
	for path, field, previous in reversed(frappe.flags.pop(ARCHIVED_FLAG, None) or []):
		try:
			_request("patch", path, settings, json={field: previous}).raise_for_status()
		except Exception:
			log_sync_event_after_rollback(
				None,
				settings,
				"Warn",
				"archive.archive_file",
				title="Taking back the archive mark failed - the file name may take it over",
				details=f"Target: {path}\nField: {field}\nPrevious value: {previous}\n\n{frappe.get_traceback()}",
			)


def _request(method, path, settings, **kwargs):
	return requests.request(
		method,
		settings.api_url.rstrip("/") + path,
		headers={"Authorization": f"Token {settings.get_password('api_token', raise_exception=False)}"},
		timeout=settings.api_call_timeout or 30,
		verify=True,
		**kwargs,
	)
