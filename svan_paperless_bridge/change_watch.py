# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

"""
Detects changes to tags, the correspondent or mapped custom field values AFTER
the initial Paperless NGX sync and flags affected files for a subsequent
metadata update (see paperless_sync.push_metadata_update()/
sync_metadata_updates()). Only business documents are watched (see "SVAN
Paperless Whitelisted DocType"/"SVAN Paperless Custom Field Source") - metadata
DIRECTLY ON THE FILE DOCUMENT ITSELF is deliberately not watched and no longer
plays any role in the sync (see paperless_sync._resolve_upload_metadata()).

Deliberately does NOT react to every DocType change in the system: the wildcard
hook (doc_events["*"]["on_change"], see hooks.py) first checks nothing but a
cached set of watched DocTypes (an O(1) lookup, analogous to Frappe's own
webhook mechanism in frappe/integrations/doctype/webhook) and returns
immediately if there is no match. The actual Paperless NGX access never happens
here, only in the next run of the scheduled job responsible for it.

IMPORTANT: "on_change" rather than "on_update" - reproduced live that
"on_update" does NOT fire for an already submitted document (docstatus=1) when
an "allow_on_submit" field (e.g. a delivery date) is changed afterwards. In that
case Frappe internally calls "on_update_after_submit" (see
frappe/model/document.py: Document.run_post_save_methods(), which branches on
self._action). "on_change", by contrast, is ALWAYS called at the end of
run_post_save_methods() - regardless of whether it was a regular save, a
submit, a cancel or a change after submit. For whitelisted DocTypes such as
Sales Order/Purchase Invoice, whose documents are mostly submitted in normal
operation, "on_update" was practically ineffective as a result.
"""

import frappe

from svan_paperless_bridge.correspondent_sync import (
	get_party_field,
	get_possible_party_types,
	rename_paperless_correspondent,
)

CACHE_KEY = "svan_paperless_bridge:watched_doctype_map"


def _build_watched_doctype_map():
	"""Build the map "watched DocType -> relevant fields/roles" from the current
	whitelist and custom field mapping configuration.

	Each entry contains:
	  - own_relevant_fields: fieldnames whose change is relevant DIRECTLY ON THIS
	    DocType - all mapped custom field source fields with source_doctype == this
	    DocType, PLUS (for a whitelisted business DocType) its own party_fieldname:
	    if WHICH party is linked changes (or the field is cleared), the document has
	    to be resynced just as it does for a change to a custom field source value -
	    reproduced live that this went unnoticed WITHOUT that entry (clearing e.g.
	    Sales Order.contact_person triggered NO metadata update on its own; only a
	    coincidental SIMULTANEOUS tag change on the same document dragged it along).
	  - party_roles: a list of {business_doctype, link_fieldname, type_fieldname,
	    title_field} if THIS DocType acts as the party target of another,
	    whitelisted business DocType (see correspondent_sync.get_party_field()). A
	    change to the title field here then affects all business records referring
	    to it via link_fieldname (a reverse lookup, see
	    handle_watched_doctype_on_change()). For a Dynamic Link, type_fieldname is
	    set (it is part of own_relevant_fields as well), and every possible party
	    type gets the role (see correspondent_sync.get_possible_party_types()).

	A DocType can appear in both roles at once (e.g. "Customer" is both whitelisted
	itself and the party target for "Sales Invoice") - both lists are filled
	independently of each other.
	"""
	watched = {}

	def _entry(doctype):
		return watched.setdefault(doctype, {"own_relevant_fields": set(), "party_roles": []})

	whitelist_rows = frappe.get_all("SVAN Paperless Whitelisted DocType", fields=["document_type"])
	for row in whitelist_rows:
		_entry(row.document_type)
		fieldname, type_fieldname, target_doctype = get_party_field(row.document_type)
		if not fieldname:
			continue

		_entry(row.document_type)["own_relevant_fields"].add(fieldname)
		if type_fieldname:
			_entry(row.document_type)["own_relevant_fields"].add(type_fieldname)
			party_doctypes = get_possible_party_types(row.document_type, type_fieldname)
		else:
			party_doctypes = [target_doctype]

		for party_doctype in party_doctypes:
			if not frappe.db.exists("DocType", party_doctype):
				continue
			_entry(party_doctype)["party_roles"].append(
				{
					"business_doctype": row.document_type,
					"link_fieldname": fieldname,
					"type_fieldname": type_fieldname,
					"title_field": frappe.get_meta(party_doctype).get_title_field(),
				}
			)

	source_rows = frappe.get_all(
		"SVAN Paperless Custom Field Source", fields=["source_doctype", "source_fieldname"]
	)
	for row in source_rows:
		_entry(row.source_doctype)["own_relevant_fields"].add(row.source_fieldname)

	return watched


def get_watched_doctype_map():
	"""Cached version of _build_watched_doctype_map().

	See invalidate_watched_doctype_map_cache() for invalidation on configuration
	changes.
	"""
	return frappe.cache().get_value(CACHE_KEY, generator=_build_watched_doctype_map)


def invalidate_watched_doctype_map_cache():
	"""Discard the cached watched-DocType map.

	Called from SVANPaperlessSettings.on_update() (whitelist or party_fieldname
	change), SVANPaperlessCustomFieldMapping.on_update()/on_trash() (a new,
	changed or deleted custom field source row) and
	SVANPaperlessCorrespondentMapping.on_update() (a new party type,
	see correspondent_sync.get_possible_party_types()) - unconditionally, as these
	configuration changes are rare and rebuilding the map on the next access is
	cheap.
	"""
	frappe.cache().delete_value(CACHE_KEY)


def handle_watched_doctype_on_change(doc, method=None):
	"""doc_events["*"]["on_change"] - see hooks.py.

	Fires on EVERY successful save of EVERY DocType in the system (regular save,
	submit, cancel, change after submit - see the module docstring for why
	"on_change" instead of "on_update"), but returns immediately after a single
	dict lookup for the overwhelming majority. Only on an actual match is
	has_value_changed() checked as well and _flag_business_record() called if
	needed - as a rule there is no Paperless NGX access at this point, only the
	setting of a flag (see there). EXCEPTION: if the title field of a party target
	changes (the party_roles case), it is additionally checked right away (not via
	the scheduled job) whether a correspondent already exists for it in Paperless
	NGX, and that correspondent is renamed via PATCH if needed (see
	_rename_correspondent_if_mapped()) - otherwise a later metadata update would
	send the same, already mapped (and therefore unchanged) correspondent again
	without anything changing visibly in Paperless NGX (analogous to the tag rename
	case, reproduced live).
	"""
	entry = get_watched_doctype_map().get(doc.doctype)
	if not entry:
		return

	try:
		if entry["own_relevant_fields"] and any(
			doc.has_value_changed(fieldname) for fieldname in entry["own_relevant_fields"]
		):
			_flag_business_record(doc.doctype, doc.name)

		for role in entry["party_roles"]:
			title_field = role["title_field"]
			if not title_field or not doc.has_value_changed(title_field):
				continue
			_rename_correspondent_if_mapped(doc.doctype, doc.name, doc.get(title_field))
			filters = {role["link_fieldname"]: doc.name}
			if role.get("type_fieldname"):
				filters[role["type_fieldname"]] = doc.doctype
			business_names = frappe.get_all(role["business_doctype"], filters=filters, pluck="name")
			for business_name in business_names:
				_flag_business_record(role["business_doctype"], business_name)
	except Exception:
		# Must never prevent the business document from being saved - a missed
		# metadata update is uncritical (the next relevant change or a manual
		# resync makes up for it), a save blocked by it would not be.
		frappe.log_error(
			title="svan_paperless_bridge: change detection failed",
			message=frappe.get_traceback(),
		)


def _rename_correspondent_if_mapped(party_type, party_value, new_display_name):
	"""Rename the corresponding Paperless NGX correspondent directly via PATCH -
	if a "SVAN Paperless Correspondent Mapping" row already exists for this party.

	correspondent_sync.get_paperless_correspondent_id_for_document() maps by
	(frappe_party_type, frappe_party) - the STABLE party identity (e.g. Customer
	plus its docname), NOT the display name. A pure name change (e.g.
	Customer.customer_name) therefore still finds the existing mapping row
	correctly, but without this PATCH it would only send the same, already existing
	Paperless correspondent ID again - nothing would change visibly in Paperless
	NGX (analogous to the tag rename case, see tag_sync.rename_paperless_tag()). A
	failure here does not block the caller from flagging the affected files.
	"""
	if not new_display_name:
		return

	mapping = frappe.db.get_value(
		"SVAN Paperless Correspondent Mapping",
		{"frappe_party_type": party_type, "frappe_party": party_value},
		["name", "paperless_correspondent_id"],
		as_dict=True,
	)
	if not mapping or not mapping.paperless_correspondent_id:
		return

	settings = frappe.get_single("SVAN Paperless Settings")
	try:
		rename_paperless_correspondent(
			mapping.name, mapping.paperless_correspondent_id, new_display_name, settings
		)
	except Exception:
		frappe.log_error(
			title="svan_paperless_bridge: renaming the Paperless correspondent failed",
			message=frappe.get_traceback(),
		)


def handle_tag_link_change(doc, method=None):
	"""doc_events["Tag Link"]["after_insert"/"on_trash"] - see hooks.py.

	Reliably covers ADDING a tag (Document.add_tag()/DocTags.add() always creates a
	real "Tag Link" record via .insert() for that, regardless of the calling route -
	the UI tag widget or a programmatic call). This hook does NOT reliably cover
	REMOVING a tag: DocTags.remove() (frappe/desk/doctype/tag/tag.py) deletes the
	tag link with a raw frappe.db.delete(), WITHOUT using frappe.delete_doc() -
	"on_trash" never fires for it (reproduced live on a real site). For the usual
	case (the tag widget in the Desk) see remove_tag_override() below instead.
	"on_trash" is kept here nonetheless, in case a tag link is ever deleted
	regularly through frappe.delete_doc() (e.g. manually from the Tag Link list
	view).
	"""
	try:
		if doc.document_type not in get_watched_doctype_map():
			return
		_flag_business_record(doc.document_type, doc.document_name)
	except Exception:
		frappe.log_error(
			title="svan_paperless_bridge: tag change detection failed",
			message=frappe.get_traceback(),
		)


def handle_tag_rename(doc, method, old, new, merge=False):
	"""doc_events["Tag"]["after_rename"] - see hooks.py.

	Renaming a tag (e.g. from the "Tag" list view in the Desk) goes through
	frappe.rename_doc(), NOT through a regular save - "Tag Link.tag" is set to the
	new name for EVERY affected record by a raw SQL UPDATE
	(frappe/model/rename_doc.py: update_link_field_values()) BEFORE this hook fires.
	No doc_event fires for those tag link rows themselves (neither an insert nor a
	trash) - without this dedicated hook on "Tag", a tag rename would be completely
	invisible to change_watch.py.

	It therefore actively looks up all "Tag Link" rows with the NEW name (already
	the current state at this point) and flags the business records affected.

	It ALSO renames the corresponding Paperless NGX tag directly via PATCH - if a
	"SVAN Paperless Tag Mapping" row already exists for this tag (see
	tag_sync.rename_paperless_tag()): the mapping field "frappe_tag" is a link to
	"Tag" and is updated to the new name by frappe.rename_doc() automatically -
	WITHOUT this PATCH the (still valid) mapping row would merely point at the same,
	unchanged Paperless tag and a subsequent metadata update would remain invisible
	in Paperless NGX (reproduced live). A failure here does not prevent the affected
	files from being flagged below.
	"""
	try:
		mapping = frappe.db.get_value(
			"SVAN Paperless Tag Mapping", {"frappe_tag": new}, ["name", "paperless_tag_id"], as_dict=True
		)
		if mapping and mapping.paperless_tag_id:
			from svan_paperless_bridge.tag_sync import rename_paperless_tag

			settings = frappe.get_single("SVAN Paperless Settings")
			try:
				rename_paperless_tag(mapping.name, mapping.paperless_tag_id, new, settings)
			except Exception:
				frappe.log_error(
					title="svan_paperless_bridge: renaming the Paperless tag failed",
					message=frappe.get_traceback(),
				)

		affected = frappe.get_all("Tag Link", filters={"tag": new}, fields=["document_type", "document_name"])
		refresh_user_tags_cache_for_documents({(row.document_type, row.document_name) for row in affected})
		watched = get_watched_doctype_map()
		for row in affected:
			if row.document_type in watched:
				_flag_business_record(row.document_type, row.document_name)
	except Exception:
		frappe.log_error(
			title="svan_paperless_bridge: tag rename detection failed",
			message=frappe.get_traceback(),
		)


def refresh_user_tags_cache_for_documents(document_pairs):
	"""Repair Frappe's own `_user_tags` display cache field (see
	frappe/desk/doctype/tag/tag.py, class DocTags) for the given (doctype, name)
	pairs AFTER a tag rename - called from handle_tag_rename() for ALL documents
	affected by the rename.

	frappe.rename_doc("Tag", ...) reliably updates "Tag Link.tag" (a real link
	field) everywhere by cascade, but leaves "_user_tags" - a purely
	comma-separated DISPLAY copy directly on each tagged document, NOT a link
	field - unchanged. Without this repair, a LATER add_tag()/remove_tag() on an
	affected document fails with frappe.LinkValidationError (reproduced live, see
	AGENTS.md): on the next call DocTags.update() rebuilds the tag list from the
	(then stale) "_user_tags" value and validates every name in it against a "Tag"
	ID that no longer exists after the rename - and it does so for ALL tags present
	on the document together, not only for the one just added or removed, so a
	single stale name permanently blocks every further tag change on that document.

	This applies to a rename regardless of what triggered it - both directly in
	ERPNext (the tag list view) and one synced back from Paperless NGX
	(tag_sync.rename_frappe_tag_from_paperless() triggers the same
	frappe.rename_doc("Tag", ...) call and therefore the same after_rename hook) -
	this single hook covers both cases uniformly, without
	rename_frappe_tag_from_paperless() having to know anything about it.

	A failure per document (e.g. a "_user_tags" column that was never created on
	that DocType) is only logged and skipped - the tag rename itself is already
	complete at this point and must not be affected by it.
	"""
	for document_type, document_name in document_pairs:
		try:
			current_tags = frappe.get_all(
				"Tag Link",
				filters={"document_type": document_type, "document_name": document_name},
				pluck="tag",
			)
			frappe.db.set_value(
				document_type, document_name, "_user_tags", ",".join(current_tags), update_modified=False
			)
		except Exception:
			frappe.log_error(
				title="svan_paperless_bridge: _user_tags cache repair failed",
				message=f"{document_type} {document_name}\n\n{frappe.get_traceback()}",
			)


@frappe.whitelist()
def remove_tag_override(tag: str, dt: str, dn: str):
	"""Replaces frappe.desk.doctype.tag.tag.remove_tag() through
	override_whitelisted_methods (see hooks.py) - the calling route of the tag
	widget in the Desk (and of any other caller of this whitelisted API).

	Necessary because "Tag Link" on_trash (see handle_tag_link_change()) does NOT
	detect a tag being removed: DocTags.remove() deletes the tag link with a raw
	SQL DELETE instead of frappe.delete_doc(). override_whitelisted_methods
	REPLACES the original function entirely (the original is not called
	automatically) - this therefore deliberately replicates its single line
	(DocTags(dt).remove(dn, tag)) exactly before flagging on top.

	@frappe.whitelist() is mandatory even though the function is only reached
	through override_whitelisted_methods: frappe.handler.execute_cmd() does resolve
	the target name to this override via override_whitelisted_method(), but then
	checks the RESOLVED function (not the original method name) against the
	@frappe.whitelist() registry separately (frappe.is_whitelisted()) - reproduced
	live ("function ... is not whitelisted", HTTP 403) through the tag widget in the
	Desk, because that check was bypassed by a direct test call beforehand.

	Does NOT cover the case of other Python code calling Document.remove_tag()
	directly (which bypasses the whitelist dispatch layer that
	override_whitelisted_methods hooks into) - a known, documented gap (see
	AGENTS.md).
	"""
	from frappe.desk.doctype.tag.tag import DocTags

	DocTags(dt).remove(dn, tag)

	try:
		if dt in get_watched_doctype_map():
			_flag_business_record(dt, dn)
	except Exception:
		frappe.log_error(
			title="svan_paperless_bridge: tag change detection failed",
			message=frappe.get_traceback(),
		)


def _flag_business_record(business_doctype, business_name):
	"""Flag the files affected by a detected change - the shared entry point for
	all callers in this module (handle_watched_doctype_on_change(),
	handle_tag_link_change(), handle_tag_rename(), remove_tag_override()).
	business_doctype is never "File" here - metadata directly on the File document
	itself is no longer watched (see the module docstring).

	Returns early when frappe.flags.svan_paperless_applying_webhook_update is set:
	webhook_sync sets that flag while it applies a change reported by Paperless NGX
	via webhook to the business document - tags go through the regular
	Document.add_tag() API there, which triggers the same handle_tag_link_change()
	hook through "Tag Link" after_insert, leading HERE. Without this early return,
	the change just taken over from Paperless NGX would immediately be flagged as
	svan_paperless_needs_update again and PATCHed back to Paperless NGX
	unnecessarily (though without effect) on the next scheduled run - in the worst
	case a permanent ping-pong, since that PATCH counts as "document updated" in
	Paperless NGX itself and would trigger the webhook again.
	"""
	if frappe.flags.get("svan_paperless_applying_webhook_update"):
		return

	flag_files_for_business_record(business_doctype, business_name)


def flag_files_for_business_record(attached_to_doctype, attached_to_name):
	"""Flag all "syncing"/"synced" files on attached_to_doctype/attached_to_name for
	a subsequent metadata update (svan_paperless_needs_update = 1).

	"syncing" is included because its upload request has already been sent: a change
	made while Paperless NGX is still processing the document would otherwise be lost.
	sync_metadata_updates only picks up "synced" files, so the flag simply waits for
	finalize_sync. "pending" files read the current values live during their upload,
	and "broken"/"deleted" files are never synced again. The status condition is
	repeated in the UPDATE itself as protection against a race with a deletion running
	in parallel (paperless_delete.py).

	Set by raw SQL instead of doc.save() - this avoids any risk of hook recursion
	and ensures (through the immediate commit) that the flag is guaranteed to be
	visible to the next scheduled run, regardless of how the transaction of the
	triggering save proceeds.
	"""
	file_names = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": attached_to_doctype,
			"attached_to_name": attached_to_name,
			"svan_paperless_sync_status": ["in", ["syncing", "synced"]],
		},
		pluck="name",
	)
	if not file_names:
		return

	frappe.db.sql(
		"""
		UPDATE `tabFile`
		SET svan_paperless_needs_update = 1
		WHERE name IN %(file_names)s AND svan_paperless_sync_status IN ('syncing', 'synced')
		""",
		{"file_names": file_names},
	)
	frappe.db.commit()
