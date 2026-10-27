# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

"""Consequences of the "Reference For Import" for the configuration.

While a reference is set, Paperless NGX decides which business document a linked
file belongs to (see document_relink.py). Two things keep that from detaching
files by mistake:

- A gap (a whitelisted DocType the reference has no source on) blocks the upload
  of new files of that DocType, as their documents would carry no reference.
- Setting or changing the reference, or changing its sources, flags every linked
  file of a DocType with a source for a metadata update, so Paperless NGX gets the
  reference values before anything is moved or detached (backfill).

Any change of what qualifies for the import (reference, its sources, import tag,
whitelisted DocTypes and their document types) restarts the import scan from the
beginning: the documents scanned before would otherwise never be looked at again.
"""

import json

import frappe
from frappe import _

SETTINGS = "SVAN Paperless Settings"
CUSTOM_FIELD_MAPPING = "SVAN Paperless Custom Field Mapping"
CUSTOM_FIELD_SOURCE = "SVAN Paperless Custom Field Source"


def reference_gaps(settings, sources=None):
	"""Whitelisted DocTypes the reference has no source on; empty without a
	reference. sources overrides the stored ones (a mapping being saved).
	"""
	if not settings.import_reference_mapping:
		return []
	if sources is None:
		sources = _stored_sources(settings.import_reference_mapping)
	return [
		row.document_type
		for row in settings.whitelisted_doctypes
		if row.document_type and row.document_type not in sources
	]


def has_reference_gap(doctype, settings):
	return doctype in reference_gaps(settings)


def flag_files_for_reference_backfill(mapping_name):
	"""Flag the linked files of every DocType the mapping has a source on for a
	metadata update. Returns their number.
	"""
	doctypes = list(_stored_sources(mapping_name))
	if not doctypes:
		return 0
	files = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": ["in", doctypes],
			"svan_paperless_document_id": ["is", "set"],
			"svan_paperless_sync_status": "synced",
		},
		pluck="name",
	)
	if files:
		frappe.db.set_value(
			"File", {"name": ["in", files]}, "svan_paperless_needs_update", 1, update_modified=False
		)
	return len(files)


def reference_changed_on_settings(settings):
	"""True if this save sets a reference or switches to another one."""
	before = settings.get_doc_before_save()
	old = before.import_reference_mapping if before else None
	return bool(settings.import_reference_mapping) and settings.import_reference_mapping != old


def import_rules_changed_on_settings(settings):
	"""True if this save changes what qualifies for the import."""
	before = settings.get_doc_before_save()
	return not before or _import_rule_inputs(before) != _import_rule_inputs(settings)


def restart_import_scan():
	# Without update_modified=False an open Settings form could no longer be saved.
	frappe.db.set_single_value(SETTINGS, "import_scan_watermark", "", update_modified=False)


def sources_changed_on_mapping(mapping):
	"""True if this save changes the sources of the reference mapping."""
	from svan_paperless_bridge.svan_paperless_ngx_bridge.doctype.svan_paperless_settings.svan_paperless_settings import (
		is_import_reference,
	)

	if not is_import_reference(mapping.name):
		return False
	before = mapping.get_doc_before_save()
	return not before or _sources_of(before) != _sources_of(mapping)


@frappe.whitelist()
def check_reference_change(doc):
	"""What saving this (unsaved) Settings or Custom Field Mapping would bring
	about, for the confirmation in the form: {"gaps": [...], "backfill": n}.
	backfill is None if no backfill would run.
	"""
	frappe.only_for("System Manager")
	doc = frappe.get_doc(json.loads(doc) if isinstance(doc, str) else doc)

	if doc.doctype == SETTINGS:
		backfill = None
		if doc.import_reference_mapping and doc.import_reference_mapping != frappe.db.get_single_value(
			SETTINGS, "import_reference_mapping"
		):
			backfill = _count_linked_files(_stored_sources(doc.import_reference_mapping))
		return {"gaps": reference_gaps(doc), "backfill": backfill}

	if doc.doctype == CUSTOM_FIELD_MAPPING:
		settings = frappe.get_single(SETTINGS)
		if doc.is_new() or settings.import_reference_mapping != doc.name:
			return {"gaps": [], "backfill": None}
		sources = _sources_of(doc)
		backfill = None
		if sources != _stored_sources(doc.name):
			backfill = _count_linked_files(sources)
		return {"gaps": reference_gaps(settings, sources), "backfill": backfill}

	frappe.throw(_("Unsupported DocType {0}.").format(doc.doctype))


def _count_linked_files(sources):
	if not sources:
		return 0
	return frappe.db.count(
		"File",
		{
			"attached_to_doctype": ["in", list(sources)],
			"svan_paperless_document_id": ["is", "set"],
			"svan_paperless_sync_status": "synced",
		},
	)


def _stored_sources(mapping_name):
	"""{source DocType: source fieldname} of a saved mapping."""
	rows = frappe.get_all(
		CUSTOM_FIELD_SOURCE,
		filters={"parent": mapping_name, "parenttype": CUSTOM_FIELD_MAPPING},
		fields=["source_doctype", "source_fieldname"],
	)
	return {row.source_doctype: row.source_fieldname for row in rows if row.source_doctype}


def _import_rule_inputs(settings):
	return (
		settings.import_reference_mapping or "",
		settings.import_tag or "",
		sorted(
			(row.document_type, row.paperless_document_type_name or "")
			for row in settings.whitelisted_doctypes
		),
	)


def _sources_of(mapping):
	return {row.source_doctype: row.source_fieldname for row in mapping.field_sources if row.source_doctype}
