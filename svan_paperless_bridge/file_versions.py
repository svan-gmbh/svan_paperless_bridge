# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

"""Files attached again to the same business document under the same name become
new versions of one Paperless NGX document instead of documents of their own.

Frappe appends six characters to a file name whose path already exists on disk
(content_hash[-6:], random on a collision; see generate_file_name()). The check is
site-wide, so a name may also lack the suffix (the original's content was already
offloaded) or carry one although it is the original. Both names are therefore
compared without that suffix.
"""

import os
import re

import frappe
import requests

FRAPPE_NAME_SUFFIX_RE = re.compile(r"[0-9a-f]{6}$")

# find_version_target(): an earlier file of the group is still on its way to Paperless NGX.
WAIT_FOR_ORIGINAL = "wait"

QUEUED_STATUSES = ("pending", "syncing")


def find_version_target(file_doc):
	"""Return the Paperless NGX document ID the file becomes a new version of,
	WAIT_FOR_ORIGINAL, or None for an upload as a document of its own.

	The group are the other files of the same business document with the same
	normalized name. Any synced one supplies the document (the latest, should
	several documents exist from before versioning). Without one, the earliest
	queued file of the group uploads as the original and the others wait for it.
	"""
	group = _version_group(file_doc)

	synced = [f for f in group if f.svan_paperless_sync_status == "synced" and f.svan_paperless_document_id]
	if synced:
		return max(synced, key=_creation_order).svan_paperless_document_id

	if any(
		f.svan_paperless_sync_status in QUEUED_STATUSES and _creation_order(f) < _creation_order(file_doc)
		for f in group
	):
		return WAIT_FOR_ORIGINAL

	return None


def post_version(file_doc, document_id, content, settings):
	"""Upload content as a new version of document_id; the version label is the file
	name. Returns the response (a task UUID on HTTP 200); raises on a request error.
	"""
	token = settings.get_password("api_token", raise_exception=False)
	return requests.post(
		settings.api_url.rstrip("/") + f"/documents/{document_id}/update_version/",
		headers={"Authorization": f"Token {token}"},
		data={"version_label": file_doc.file_name},
		files={"document": (file_doc.file_name, content)},
		timeout=settings.api_call_timeout or 30,
		verify=True,
	)


def own_version_id(file_doc):
	"""The version holding this file's content. Empty for files synced before
	versioning existed: those hold the original.
	"""
	return file_doc.get("svan_paperless_version_id") or file_doc.get("svan_paperless_document_id")


def is_version_file(file_doc):
	version_id = file_doc.get("svan_paperless_version_id")
	return bool(version_id) and str(version_id) != str(file_doc.get("svan_paperless_document_id"))


def lock_attachments(doctype, name):
	"""Lock the attachments of a business document until the commit and return them
	as committed by now, so the version import and the version upload never take the
	same version. Locked through the attachment link, not the document ID: a new
	attachment of the business document, possibly the next version, waits as well.
	"""
	return frappe.db.get_values(
		"File",
		{"attached_to_doctype": doctype, "attached_to_name": name},
		["name", "svan_paperless_document_id", "svan_paperless_version_id", "svan_paperless_sync_status"],
		as_dict=True,
		for_update=True,
	)


def held_version_ids(attachments, document_id):
	"""The versions of document_id the given files hold."""
	return {
		str(own_version_id(f))
		for f in attachments
		if str(f.svan_paperless_document_id or "") == str(document_id)
		and f.svan_paperless_sync_status != "deleted"
	}


def normalized_name(file_name):
	stem, extension = os.path.splitext(file_name or "")
	if len(stem) > 6 and FRAPPE_NAME_SUFFIX_RE.search(stem):
		stem = stem[:-6]
	return stem + extension


def _version_group(file_doc):
	if not (file_doc.attached_to_doctype and file_doc.attached_to_name):
		return []

	name = normalized_name(file_doc.file_name)
	siblings = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": file_doc.attached_to_doctype,
			"attached_to_name": file_doc.attached_to_name,
			"name": ["!=", file_doc.name],
			"is_folder": 0,
		},
		fields=[
			"name",
			"file_name",
			"creation",
			"svan_paperless_sync_status",
			"svan_paperless_document_id",
		],
	)
	return [f for f in siblings if normalized_name(f.file_name) == name]


def _creation_order(file_row):
	return (frappe.utils.get_datetime(file_row.creation), file_row.name)
