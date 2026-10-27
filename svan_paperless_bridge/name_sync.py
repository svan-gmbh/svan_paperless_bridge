# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

"""File names are matched with Paperless NGX in both directions: the file holding
the original with the document's title and the label of its version, every other
file with the label of its version (versions have no title of their own).

Each file remembers the name last matched (svan_paperless_synced_name) and the
title and label Paperless NGX had then. A side whose value differs from those has
changed it; while a rename in ERPNext waits for its push, ERPNext wins. A file
linked before the matching existed has none of them yet: the first check only
takes the current values, so differences from back then stay until a name is
changed.
"""

import os

import frappe
import requests

from svan_paperless_bridge.file_versions import own_version_id
from svan_paperless_bridge.preview import get_preview_url, is_preview_url
from svan_paperless_bridge.utils.sync_log import log_sync_event
from svan_paperless_bridge.utils.whitelist import whitelisted_doctypes, whitelisted_filter

# Paperless NGX rejects longer values with HTTP 400; File.file_name is a Data field.
TITLE_MAX_LENGTH = 128
LABEL_MAX_LENGTH = 64
FILE_NAME_MAX_LENGTH = 140

FILE_FIELDS = [
	"name",
	"file_name",
	"attached_to_doctype",
	"svan_paperless_document_id",
	"svan_paperless_version_id",
	"svan_paperless_sync_status",
	"svan_paperless_synced_name",
	"svan_paperless_synced_title",
	"svan_paperless_synced_label",
]


def remember_sent_names(file_doc, title, label):
	"""What an upload sent. A value changed in Paperless NGX before the poll completes
	the file is taken over then (align_uploaded_file()).
	"""
	_remember(file_doc.name, file_doc.file_name, title, label)


def align_uploaded_file(file_doc, settings):
	"""After an upload from ERPNext: the title (original only) and the version label
	take the file name. Paperless NGX ignores a label sent with a new document.
	Returns the document if a value was changed in Paperless NGX during the upload:
	it is left as it is and taken over by apply_paperless_names() once the file is
	synced. On failure the file is matched like one linked before, from its current
	values.
	"""
	document_id = str(file_doc.svan_paperless_document_id)
	try:
		document = _get(f"/documents/{document_id}/", settings)
		title, label = _paperless_values(document, own_version_id(file_doc))
		# Adopted documents carry no UUID; their values were never sent from here.
		if (
			file_doc.svan_paperless_uuid
			and file_doc.svan_paperless_synced_name
			and (title, label)
			!= (file_doc.svan_paperless_synced_title or "", file_doc.svan_paperless_synced_label or "")
		):
			return document
		title, label = _write_names(
			document_id, own_version_id(file_doc), file_doc.file_name, title, label, settings
		)
	except Exception:
		log_sync_event(
			file_doc.name,
			settings,
			"Warn",
			"name_sync.align_uploaded_file",
			title="Setting the file name as title/version label failed - Paperless NGX keeps its values",
			details=f"Paperless NGX document ID: {document_id}\n\n{frappe.get_traceback()}",
		)
		return None
	_remember(file_doc.name, file_doc.file_name, title, label)
	return None


def remember_imported_names(file_doc, document):
	"""An imported file starts from its own name and the values Paperless NGX has now."""
	title, label = _paperless_values(document, own_version_id(file_doc))
	_remember(file_doc.name, file_doc.file_name, title, label)


def apply_paperless_names(document, settings, source):
	"""Rename the files of document whose title or version label was changed in
	Paperless NGX, then align title and label of the original with each other.
	"""
	document_id = str(document["id"])
	if "title" not in document or "versions" not in document:
		return
	doctypes = whitelisted_doctypes(settings)
	files = frappe.get_all(
		"File",
		filters={
			"svan_paperless_document_id": document_id,
			"attached_to_doctype": whitelisted_filter(settings),
		},
		fields=FILE_FIELDS,
	)
	if not any(_differs(row, document) for row in files):
		# The usual case on every comparison: no lock, no commit.
		return
	# The rows are read again under the lock below; a snapshot from before it could be stale.
	frappe.db.commit()
	for row in lock_files(document_id):
		if row.attached_to_doctype not in doctypes or not _differs(row, document):
			continue
		title, label = _paperless_values(document, own_version_id(row))
		if not row.svan_paperless_synced_name:
			# Linked before the matching existed.
			_remember(row.name, row.file_name, title, label)
			continue
		if row.file_name != row.svan_paperless_synced_name:
			# Renamed past the File hooks, e.g. with frappe.db.set_value().
			frappe.db.set_value("File", row.name, "svan_paperless_needs_update", 1, update_modified=False)
			continue

		changed = _changed_name(row, title, label)
		if changed:
			_rename_from_paperless(row, document_id, changed, title, label, settings, source)
		elif (title, label) != (row.svan_paperless_synced_title or "", row.svan_paperless_synced_label or ""):
			# Emptied in Paperless NGX: not taken, the file keeps its name.
			_remember(row.name, row.file_name, title, label)
	frappe.db.commit()


def push_file_names(document_id, settings):
	"""Transfer the names of the document's files renamed in ERPNext. Returns False
	if one failed; it stays renamed and is retried with the next push.
	"""
	pushed = True
	for row in lock_files(str(document_id)):
		if (
			row.svan_paperless_sync_status != "synced"
			or not row.svan_paperless_synced_name
			or row.file_name == row.svan_paperless_synced_name
		):
			continue
		try:
			# Unconditionally: Paperless NGX may have changed in the meantime, and ERPNext wins.
			title, label = _write_names(document_id, own_version_id(row), row.file_name, None, None, settings)
		except Exception:
			log_sync_event(
				row.name,
				settings,
				"Error",
				"name_sync.push_file_names",
				title="Transferring the file name failed - will be retried",
				details=f"Paperless NGX document ID: {document_id}\n\n{frappe.get_traceback()}",
			)
			pushed = False
			continue
		_remember(row.name, row.file_name, title, label)
		log_sync_event(
			row.name,
			settings,
			"Info",
			"name_sync.push_file_names",
			title="File name transferred to Paperless NGX",
			details=_details(document_id, row, title, label, previous=row.svan_paperless_synced_name),
		)
	return pushed


def _rename_from_paperless(row, document_id, changed, title, label, settings, source):
	field, value = changed
	new_name = file_name_from(value, row.file_name)
	frappe.db.savepoint("svan_paperless_name_sync")
	try:
		if new_name != row.file_name:
			file_doc = frappe.get_doc("File", row.name)
			file_doc.file_name = new_name
			if is_preview_url(file_doc.file_url):
				# The route carries the name for Frappe's extension check and the download.
				file_doc.file_url = get_preview_url(file_doc)
			file_doc.svan_paperless_synced_name = new_name
			file_doc.save(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point="svan_paperless_name_sync")
		log_sync_event(
			row.name,
			settings,
			"Error",
			source,
			title="Renaming the file failed - will be retried on the next change or comparison",
			details=f"Paperless NGX document ID: {document_id}\n\n{frappe.get_traceback()}",
		)
		return

	try:
		title, label = _write_names(document_id, own_version_id(row), new_name, title, label, settings)
	except Exception:
		log_sync_event(
			row.name,
			settings,
			"Warn",
			source,
			title="Aligning title and version label failed - they stay as they are",
			details=f"Paperless NGX document ID: {document_id}\n\n{frappe.get_traceback()}",
		)
	_remember(row.name, new_name, title, label)
	if new_name != row.file_name:
		log_sync_event(
			row.name,
			settings,
			"Info",
			source,
			title=f"File renamed after a change of the {field} in Paperless NGX",
			details=_details(document_id, row, title, label, previous=row.file_name, new=new_name),
		)


def _differs(row, document):
	"""Anything to remember or to rename for this file."""
	if row.svan_paperless_sync_status != "synced" or not _has_version(document, own_version_id(row)):
		return False
	return (
		not row.svan_paperless_synced_name
		or row.file_name != row.svan_paperless_synced_name
		or _paperless_values(document, own_version_id(row))
		!= (row.svan_paperless_synced_title or "", row.svan_paperless_synced_label or "")
	)


def _changed_name(row, title, label):
	"""(field, value) changed in Paperless NGX, the title first; an emptied one counts as none."""
	if _is_original(row) and title != (row.svan_paperless_synced_title or "") and title.strip():
		return "title", title
	if label != (row.svan_paperless_synced_label or "") and label.strip():
		return "version label", label
	return None


def _write_names(document_id, version_id, file_name, title, label, settings):
	"""Set title (original only) and version label to file_name where they do not
	match it yet; None counts as not matching. Returns the values Paperless NGX has now.
	"""
	if str(version_id) == str(document_id) and not _matches(title, file_name, TITLE_MAX_LENGTH):
		title = _fit(file_name, TITLE_MAX_LENGTH)
		_patch(f"/documents/{document_id}/", {"title": title}, settings)
	if not _matches(label, file_name, LABEL_MAX_LENGTH):
		label = _fit(file_name, LABEL_MAX_LENGTH)
		_patch(f"/documents/{document_id}/versions/{version_id}/", {"version_label": label}, settings)
	return title or "", label


def _matches(value, file_name, max_length):
	"""A title often lacks the extension; that still counts as the same name."""
	target = _fit(file_name, max_length)
	return bool(value) and value in (target, os.path.splitext(target)[0])


def _fit(name, max_length):
	"""Shortened before the extension."""
	if len(name) <= max_length:
		return name
	stem, extension = os.path.splitext(name)
	if len(extension) >= max_length:
		return name[:max_length]
	return stem[: max_length - len(extension)].rstrip() + extension


def file_name_from(value, current_name):
	"""The extension of the file always stays. Frappe allows no "/" in a file name and
	drops it the same way on insert.
	"""
	name = value.replace("/", "").strip()
	extension = os.path.splitext(current_name)[1]
	if not name:
		return current_name
	if extension and not name.lower().endswith(extension.lower()):
		name += extension
	return _fit(name, FILE_NAME_MAX_LENGTH)


def _paperless_values(document, version_id):
	"""(title, label) of the file's version; the title only for the original."""
	title = (document.get("title") or "") if str(version_id) == str(document["id"]) else ""
	version = _version(document, version_id)
	return title, (version or {}).get("version_label") or ""


def _has_version(document, version_id):
	return _version(document, version_id) is not None or str(version_id) == str(document["id"])


def _version(document, version_id):
	return next((v for v in document.get("versions") or [] if str(v["id"]) == str(version_id)), None)


def _is_original(row):
	return str(own_version_id(row)) == str(row.svan_paperless_document_id)


def _remember(name, file_name, title, label):
	frappe.db.set_value(
		"File",
		name,
		{
			"svan_paperless_synced_name": file_name,
			"svan_paperless_synced_title": title or "",
			"svan_paperless_synced_label": label or "",
		},
		update_modified=False,
	)


def lock_files(document_id):
	"""The document's files, locked until the commit: the webhook fired by an own
	change must not take it for a change made in Paperless NGX.
	"""
	return frappe.db.get_values(
		"File",
		{"svan_paperless_document_id": document_id},
		FILE_FIELDS,
		as_dict=True,
		for_update=True,
		order_by="creation asc",
	)


def _details(document_id, row, title, label, previous, new=None):
	lines = [f"Previous name: {previous}"]
	if new:
		lines.append(f"New name: {new}")
	lines += [f"Paperless NGX document ID: {document_id}", f"Version ID: {own_version_id(row)}"]
	if _is_original(row):
		lines.append(f"Title: {title}")
	lines.append(f"Version label: {label}")
	return "\n".join(lines)


def _get(path, settings):
	response = requests.get(
		settings.api_url.rstrip("/") + path,
		headers=_headers(settings),
		timeout=settings.api_call_timeout or 30,
		verify=True,
	)
	response.raise_for_status()
	return response.json()


def _patch(path, data, settings):
	requests.patch(
		settings.api_url.rstrip("/") + path,
		headers=_headers(settings),
		json=data,
		timeout=settings.api_call_timeout or 30,
		verify=True,
	).raise_for_status()


def _headers(settings):
	return {"Authorization": f"Token {settings.get_password('api_token', raise_exception=False)}"}
