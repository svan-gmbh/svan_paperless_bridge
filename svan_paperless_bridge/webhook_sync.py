# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

"""
The reverse of paperless_sync.py: applies changes made to tags, the
correspondent or mapped custom fields IN Paperless NGX back to ERPNext -
triggered by a webhook Paperless NGX calls actively, not by a scheduled job. The
workflow needed for this (trigger "Document Updated", action "Webhook") is
deliberately NOT set up in Paperless NGX from this app but configured there
manually - see AGENTS.md for step-by-step instructions.
generate_webhook_secret() only provides the secret that has to be stored in the
webhook action as the header "Webhook-Secret".

Considers ONLY metadata of the business document a file is attached to -
metadata directly on the File document itself no longer plays any role in the
sync, analogous to paperless_sync.py (see the module docstring there). A
business document that no longer exists is skipped.

Sequence per call:
1. receive_update() checks the webhook secret and determines ALL File records
   with this svan_paperless_document_id from document_id - normally exactly one
   (a document belongs to one File), several only for files that already shared
   a document before that rule. Several files pointing at the same business
   document are deduplicated to ONE business document - each business document is
   updated only once.
2. The full current document state is loaded from Paperless NGX ONCE via GET
   (not from the webhook body itself - which deliberately carries only the
   document ID, see AGENTS.md for the rationale) and applied to every affected
   business document (_apply_document_to_business_record()).

Tags and custom field values are synced genuinely both ways (adding, changing
AND removing), but PER tag/custom field mapping row only if "Bidirectional" is
enabled there. For a link field as a custom field target it is switched off
categorically on save (see
svan_paperless_custom_field_mapping.disable_bidirectional_for_link_targets())
- a master record potentially shared by several documents must never be renamed
through it.

A party fieldname (whitelist column "Party Fieldname") must be a Link or Dynamic
Link (see svan_paperless_whitelisted_doctype.validate_party_fieldname()) - a party
is always a master record of its own (Customer/Supplier/Lead/...), never the
business document itself. The correspondent therefore has exactly ONE reverse
direction with TWO aspects, both only for correspondents whose "SVAN Paperless
Correspondent Mapping" row has "Bidirectional" enabled (a missing row is created
automatically, analogous to tags): (1) a NAME change of the correspondent in
Paperless NGX renames the linked party master record (e.g.
Customer.customer_name) - see
correspondent_sync.rename_frappe_party_from_paperless(). (2) A CHANGED
correspondent of ONE document relinks the party LINK of the corresponding
business document (e.g. Sales Order.customer to a different Customer) - see
_resolve_party_swap_change()/correspondent_sync.ensure_party_for_paperless_correspondent().

The document type is deliberately NOT synced back - in Paperless NGX it
represents a kind of business document that is already determined in ERPNext by
the DocType itself (analogous to the rationale in
paperless_sync._resolve_upload_metadata()).

If saving a business document fails (e.g. because a value cleared by the sync
back violates a mandatory field), that is logged as a warning - processing of
the remaining business documents/fields never aborts because of it.
"""

import hmac

import frappe
import requests
from frappe import _
from frappe.model import no_value_fields
from frappe.utils.data import cast

from svan_paperless_bridge.correspondent_sync import (
	ensure_party_for_paperless_correspondent,
	get_party_field,
	is_party_bidirectional,
)
from svan_paperless_bridge.tag_sync import ensure_frappe_tag_for_paperless_id
from svan_paperless_bridge.utils.sync_log import log_sync_event

WEBHOOK_SECRET_HEADER = "Webhook-Secret"

# Duplicated from paperless_sync to avoid a circular import.
MAX_SAVE_ATTEMPTS = 5


@frappe.whitelist()
def generate_webhook_secret():
	"""Generate a new webhook secret, ALWAYS overwriting any existing one - the
	target of the "Generate Secret" button right next to SVAN Paperless
	Settings.webhook_secret (the client script asks for confirmation beforehand if
	one is already configured).

	The webhook itself (the workflow trigger/action in Paperless NGX) is not set up
	from this app - a webhook already configured there has to be updated manually
	with the new value after a rotation (the "Webhook-Secret" header of the webhook
	action), otherwise _verify_webhook_secret() rejects every further call.
	"""
	frappe.only_for("System Manager")

	settings = frappe.get_single("SVAN Paperless Settings")
	settings.webhook_secret = frappe.generate_hash(length=40)
	settings.save(ignore_permissions=True)

	frappe.msgprint(
		_(
			'A new webhook secret has been generated. Please update the "Webhook-Secret" header of '
			"the webhook action in Paperless NGX to this value manually."
		),
		indicator="orange",
		alert=True,
	)


def _verify_webhook_secret(settings):
	"""Check the secret Paperless NGX sends in the WEBHOOK_SECRET_HEADER header
	against SVAN Paperless Settings.webhook_secret.

	Fail-closed (analogous to the delete permission check in paperless_delete.py):
	if the secret is missing from the settings (webhook not set up yet) or does not
	match, the call is ALWAYS rejected - including for Administrator/System
	Manager, as this call never comes from a logged-in Frappe user.
	hmac.compare_digest() instead of "==" for a constant-time comparison.

	Both rejection cases are logged as "Warn" in the sync log BEFORE the
	frappe.throw() (through log_sync_event(), ignore_permissions=True - still
	running as "Guest", as this check happens BEFORE the elevation to Administrator
	in receive_update()) - the secret value actually received is deliberately NEVER
	logged (not even on a mismatch), so that a secret that was intercepted or
	guessed is not stored in plain text in the sync log. Each with an explicit
	frappe.db.commit() right after the logging: Frappe automatically rolls back the
	current transaction when an exception is passed through to the end of the
	request unhandled - without that commit the log entry would have disappeared
	again through the frappe.throw() immediately following it (reproduced live:
	without the commit, the sync log stayed empty for a rejected call).
	"""
	expected = settings.webhook_secret
	if not expected:
		log_sync_event(
			None,
			settings,
			"Warn",
			"webhook_sync.receive_update",
			title="Webhook rejected - no secret configured in ERPNext",
			details='SVAN Paperless Settings.webhook_secret is empty - see the "Generate Secret" button.',
		)
		frappe.db.commit()
		frappe.throw(_("The webhook is not set up (no secret configured)."), frappe.PermissionError)

	received = frappe.get_request_header(WEBHOOK_SECRET_HEADER) or ""
	if not hmac.compare_digest(received, expected):
		log_sync_event(
			None,
			settings,
			"Warn",
			"webhook_sync.receive_update",
			title="Webhook rejected - wrong or missing secret",
			details=(
				f'Header "{WEBHOOK_SECRET_HEADER}" was '
				+ ("set but did not match." if received else "not set.")
			),
		)
		frappe.db.commit()
		frappe.throw(_("Invalid webhook secret."), frappe.PermissionError)


def _fetch_paperless_document(document_id, settings):
	"""Load the full current state of ONE document from Paperless NGX
	(GET /documents/<id>/) - the webhook body itself carries only the document ID
	(see AGENTS.md), so the actual content (tags, correspondent, custom field
	values) is fetched freshly here.

	Returns the parsed JSON dict, or None on any failure (network/HTTP/JSON error,
	or the document no longer exists) - receive_update() then aborts processing in a
	controlled way, without returning a 500 to Paperless NGX.
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
			None,
			settings,
			"Error",
			"webhook_sync.receive_update",
			title="Error while loading the document from Paperless NGX",
			details=f"Paperless NGX document ID: {document_id}\n\n{frappe.get_traceback()}",
		)
		return None

	if response.status_code == 404:
		log_sync_event(
			None,
			settings,
			"Warn",
			"webhook_sync.receive_update",
			title="Document not found in Paperless NGX - skipped",
			details=f"Paperless NGX document ID: {document_id}",
		)
		return None

	if response.status_code != 200:
		log_sync_event(
			None,
			settings,
			"Error",
			"webhook_sync.receive_update",
			title=f"Response: HTTP {response.status_code}",
			details=response.text,
		)
		return None

	try:
		return response.json()
	except Exception:
		log_sync_event(
			None,
			settings,
			"Error",
			"webhook_sync.receive_update",
			title="Response was not valid JSON",
			details=f"HTTP {response.status_code}\n\n{frappe.get_traceback()}",
		)
		return None


def _resolve_business_record(file_name):
	"""Determine the business document linked to file_name, if present and still
	existing - metadata directly on the File document itself plays no role in the
	sync back (see the module docstring).

	Returns (business_doctype, business_name) or (None, None).
	"""
	attached_to_doctype, attached_to_name = frappe.db.get_value(
		"File", file_name, ["attached_to_doctype", "attached_to_name"]
	)
	if not attached_to_doctype or not attached_to_name:
		return None, None
	if not frappe.db.exists(attached_to_doctype, attached_to_name):
		return None, None
	return attached_to_doctype, attached_to_name


def _apply_tags(business_doctype, business_name, paperless_tag_ids, settings, log_file_name):
	"""Two-way sync (adding + removing) of a business document's tags against the
	current tag set of the Paperless NGX document - PER tag only if its "SVAN
	Paperless Tag Mapping" row has "Bidirectional" enabled (a missing row is created
	automatically, default 1, see tag_sync.ensure_frappe_tag_for_paperless_id()). A
	tag set on the business document WITHOUT a (still) existing mapping row is left
	untouched when removing - without a Paperless counterpart there is no way to tell
	whether it was removed there deliberately or was never mapped.

	frappe.flags.svan_paperless_applying_webhook_update is set around
	add_tag()/remove_tag() so that change_watch._flag_business_record() ignores the
	hooks triggered by them (see the comment there) - otherwise the change just
	taken over from Paperless NGX would immediately be flagged for a metadata update
	back to Paperless NGX.

	Returns (attempted, succeeded) - attempted=True if at least one tag was supposed
	to be added/removed, succeeded=True if at least one of those actually worked.
	_apply_document_to_business_record() uses this to word the final summary log
	entry correctly (see the docstring there).
	"""
	paperless_tag_ids = set(paperless_tag_ids or [])

	frappe_tags_to_add = set()
	for paperless_tag_id in paperless_tag_ids:
		tag_name = ensure_frappe_tag_for_paperless_id(log_file_name, paperless_tag_id, settings)
		if tag_name:
			frappe_tags_to_add.add(tag_name)

	current_tags = frappe.get_all(
		"Tag Link",
		filters={"document_type": business_doctype, "document_name": business_name},
		pluck="tag",
	)

	mapping_by_tag = {}
	if current_tags:
		mapping_by_tag = {
			row.frappe_tag: row
			for row in frappe.get_all(
				"SVAN Paperless Tag Mapping",
				filters={"frappe_tag": ["in", current_tags]},
				fields=["frappe_tag", "paperless_tag_id", "bidirectional"],
			)
		}

	tags_to_remove = [
		tag
		for tag in current_tags
		if (mapping := mapping_by_tag.get(tag))
		and mapping.bidirectional
		and mapping.paperless_tag_id not in paperless_tag_ids
	]
	tags_to_add = [tag for tag in frappe_tags_to_add if tag not in current_tags]

	if not tags_to_add and not tags_to_remove:
		return False, False

	succeeded = False
	frappe.flags.svan_paperless_applying_webhook_update = True
	try:
		doc = frappe.get_doc(business_doctype, business_name)
		# Each tag in its own try/except: a transient deadlock in add_tag()/remove_tag() would
		# otherwise abort the whole sync back for this business document.
		for tag in tags_to_add:
			try:
				doc.add_tag(tag)
				succeeded = True
			except Exception:
				log_sync_event(
					log_file_name,
					settings,
					"Warn",
					"webhook_sync._apply_tags",
					title=f'Tag "{tag}" could not be added',
					details=f"{business_doctype} {business_name}\n\n{frappe.get_traceback()}",
				)
		for tag in tags_to_remove:
			try:
				doc.remove_tag(tag)
				succeeded = True
			except Exception:
				log_sync_event(
					log_file_name,
					settings,
					"Warn",
					"webhook_sync._apply_tags",
					title=f'Tag "{tag}" could not be removed',
					details=f"{business_doctype} {business_name}\n\n{frappe.get_traceback()}",
				)
	finally:
		frappe.flags.svan_paperless_applying_webhook_update = False

	return True, succeeded


def _resolve_party_swap_change(
	business_doctype, business_name, paperless_correspondent_id, settings, log_file_name
):
	"""Determine whether the party LINK of the business document (e.g.
	Sales Order.customer) should be relinked to a different record because the
	correspondent of THIS document changed in Paperless NGX - the only sync-back
	route for the correspondent of a single document.

	Analogous to tags, it is controlled PER correspondent by "Bidirectional" on its
	"SVAN Paperless Correspondent Mapping" row (a missing row is created
	automatically for a Link field, see
	correspondent_sync.ensure_party_for_paperless_correspondent()): without it the
	correspondent is skipped silently. Removing the correspondent in Paperless NGX
	(or one without a matching record) clears the link only if the CURRENT party has
	a bidirectional mapping row.

	A Dynamic Link (e.g. Quotation.party_name) is relinked only within the party
	type the document currently has (Quotation.quotation_to) and only to a party
	that already has a mapping row - the type field is never changed, so e.g. a
	Supplier can never end up in a Quotation. Without a type nothing happens.

	Unlike renaming (see rename_frappe_party_from_paperless()), a relink is NOT
	caught up by the periodic reconciliation but happens purely reactively through
	this webhook - the correspondent of ONE document only changes when that document
	itself is saved in Paperless NGX.

	Returns (fieldname, new_value) if a change is needed, otherwise None (not
	configured, target record not unambiguously identifiable, or already up to date).
	"""
	fieldname, type_fieldname, target_doctype = get_party_field(business_doctype)
	if not fieldname:
		return None

	if type_fieldname:
		target_doctype, current_value = frappe.db.get_value(
			business_doctype, business_name, [type_fieldname, fieldname]
		) or (None, None)
		if not target_doctype:
			return None
	else:
		current_value = frappe.db.get_value(business_doctype, business_name, fieldname)
	current_value = current_value or ""

	if paperless_correspondent_id:
		new_value = ensure_party_for_paperless_correspondent(
			log_file_name,
			target_doctype,
			paperless_correspondent_id,
			settings,
			match_by_title=not type_fieldname,
		)
	else:
		new_value = ""
	if new_value is None:
		return None

	if not new_value and not (current_value and is_party_bidirectional(target_doctype, current_value)):
		return None
	if current_value == new_value:
		return None

	return fieldname, new_value


def _resolve_custom_field_changes(business_doctype, business_name, custom_field_entries, settings):
	"""Determine which of the business document's own (NON-link) target fields have
	to be changed to a custom field value reported by Paperless NGX - adding,
	changing AND removing (a field we manage without a current entry in
	custom_field_entries counts as removed in Paperless NGX and is cleared), but PER
	mapping row only if "Bidirectional" is enabled there (which applies to all of
	its source fields together).

	A link field as a target is skipped (a double safeguard -
	svan_paperless_custom_field_mapping.disable_bidirectional_for_link_targets()
	already switches "Bidirectional" off for a mapping row with a link target on save).

	Returns a (possibly empty) dict {fieldname: new_value}.
	"""
	source_rows = frappe.get_all(
		"SVAN Paperless Custom Field Source",
		filters={"source_doctype": business_doctype, "parenttype": "SVAN Paperless Custom Field Mapping"},
		fields=["parent", "source_fieldname"],
	)
	if not source_rows:
		return {}

	mapping_by_name = {
		row.name: row
		for row in frappe.get_all(
			"SVAN Paperless Custom Field Mapping",
			filters={"name": ["in", list({row.parent for row in source_rows})]},
			fields=["name", "paperless_custom_field_id", "bidirectional"],
		)
	}
	value_by_paperless_id = {
		entry["field"]: entry.get("value")
		for entry in (custom_field_entries or [])
		if entry.get("field") is not None
	}

	meta = frappe.get_meta(business_doctype)
	changes = {}
	for row in source_rows:
		mapping = mapping_by_name.get(row.parent)
		if not mapping or not mapping.bidirectional or not mapping.paperless_custom_field_id:
			continue

		field = meta.get_field(row.source_fieldname)
		if not field or field.fieldtype == "Link" or field.fieldtype in no_value_fields:
			continue

		new_value = value_by_paperless_id.get(mapping.paperless_custom_field_id)
		if new_value is None:
			new_value = ""

		current_value = frappe.db.get_value(business_doctype, business_name, row.source_fieldname)
		if _is_unchanged(field.fieldtype, current_value, new_value):
			continue

		changes[row.source_fieldname] = new_value

	return changes


def _is_unchanged(fieldtype, current_value, new_value):
	"""Compare in the type of the Frappe field: Paperless sends a date as the string
	"2026-03-17" while Frappe returns a date object, which would otherwise count as a
	change on every webhook and rewrite the same value.
	"""
	if current_value in (None, "") and new_value == "":
		return True
	try:
		return current_value == cast(fieldtype, new_value)
	except Exception:
		return current_value == new_value


def _apply_field_change(business_doctype, business_name, fieldname, value, settings, log_file_name):
	"""Write ONE field value with doc.save() (rather than frappe.db.set_value() as
	before) to the business document - only that way does real field validation
	apply (e.g. a mandatory field violated by a cleared value, or a field that can
	no longer be changed after submit).

	EVERY field separately instead of bundled in a shared doc.save(): reproduced
	live that a single field rejected by Frappe (e.g. UpdateAfterSubmitError for a
	non-"allow_on_submit" field) would otherwise also bring down a completely
	independent, perfectly valid second field of the same update (the
	transaction/validation scope of doc.save() is the entire document).

	Up to MAX_SAVE_ATTEMPTS attempts (reloading the document on each attempt,
	analogous to paperless_sync.save_uuid()/finalize_sync()): on a multi-user system
	another user may have changed the same business document between loading and
	saving (frappe.QueryDeadlockError covers both real deadlocks and "Record has
	changed since last read" in this Frappe version, see AGENTS.md) - on such a
	system that is the normal case, not an edge case. If all attempts fail (for
	whatever reason, including a genuine validation rejection), it is logged as a
	warning instead of aborting the processing of the remaining
	fields/business documents (see AGENTS.md) - unlike save_uuid()/finalize_sync()
	there is no "must become terminal" requirement here, so there is no final
	db_set() enforcement; the file/document simply keeps its previous value.

	Returns True on success, otherwise False -
	_apply_document_to_business_record() uses this to word the final summary log
	entry correctly (see the docstring there).
	"""
	frappe.flags.svan_paperless_applying_webhook_update = True
	try:
		for attempt in range(1, MAX_SAVE_ATTEMPTS + 1):
			try:
				doc = frappe.get_doc(business_doctype, business_name)
				doc.set(fieldname, value)
				doc.save(ignore_permissions=True)
			except Exception:
				if attempt == MAX_SAVE_ATTEMPTS:
					log_sync_event(
						log_file_name,
						settings,
						"Warn",
						"webhook_sync._apply_field_change",
						title=f"Value could not be applied after {MAX_SAVE_ATTEMPTS} attempts",
						details=(
							f"{business_doctype}.{fieldname} = {value!r} ({business_name})\n\n"
							f"{frappe.get_traceback()}"
						),
					)
				continue
			else:
				return True
		return False
	finally:
		frappe.flags.svan_paperless_applying_webhook_update = False


def _apply_document_to_business_record(business_doctype, business_name, document, settings, log_file_name):
	"""Apply the document state loaded by _fetch_paperless_document() to ONE
	business document - called from receive_update() for every affected,
	deduplicated business record.

	The final summary log entry distinguishes THREE cases instead of
	unconditionally logging "Changes from Paperless NGX applied" as before -
	reproduced live that this message wrongly suggested success for a cancelled
	business document (where every single field change fails with "Cannot edit
	cancelled document", see the preceding "Warn" entries from
	_apply_field_change()) although literally NOTHING was applied:
	1. changes_attempted=False (nothing to do - tags/correspondent/custom fields
	   already up to date): no log entry - a webhook call without any effect on this
	   business document is not an event worth reporting (the individual resolvers
	   log nothing for "no change needed" anyway).
	2. changes_succeeded=True (at least ONE change actually applied): "Changes from
	   Paperless NGX applied" as before - even if other changes failed ALONGSIDE it
	   (those already have their own "Warn" entry).
	3. changes_attempted=True but changes_succeeded=False (ALL attempted changes
	   failed): an honest message of its own instead of the misleading success
	   wording - the concrete failures are already in the sync log as "Warn" entries
	   immediately before it.
	"""
	tags_attempted, tags_succeeded = _apply_tags(
		business_doctype, business_name, document.get("tags") or [], settings, log_file_name
	)
	changes_attempted = tags_attempted
	changes_succeeded = tags_succeeded

	swap_change = _resolve_party_swap_change(
		business_doctype, business_name, document.get("correspondent"), settings, log_file_name
	)
	if swap_change:
		changes_attempted = True
		fieldname, value = swap_change
		if _apply_field_change(business_doctype, business_name, fieldname, value, settings, log_file_name):
			changes_succeeded = True

	custom_field_changes = _resolve_custom_field_changes(
		business_doctype, business_name, document.get("custom_fields"), settings
	)
	for fieldname, value in custom_field_changes.items():
		changes_attempted = True
		if _apply_field_change(business_doctype, business_name, fieldname, value, settings, log_file_name):
			changes_succeeded = True

	if not changes_attempted:
		return

	if changes_succeeded:
		title = "Changes from Paperless NGX applied"
	else:
		title = "None of the detected changes could be applied - see the preceding warnings"

	log_sync_event(
		log_file_name,
		settings,
		"Info",
		"webhook_sync.receive_update",
		title=title,
		details=f"Business document: {business_doctype} {business_name}\nPaperless NGX document ID: {document.get('id')}",
	)


@frappe.whitelist(allow_guest=True)
def receive_update(document_id=None, **kwargs):
	"""Webhook endpoint for Paperless NGX (see AGENTS.md for how to set up the
	required workflow in Paperless NGX manually) - called actively on every update
	of a document in Paperless NGX (trigger "Document Updated"), NOT by a scheduled
	job.

	allow_guest=True, because the call never comes from a logged-in Frappe user -
	authentication runs through the secret sent in the header instead (see
	_verify_webhook_secret()). **kwargs absorbs additional fields not needed here
	that a future, extended webhook body might send along, without the function call
	built from Frappe's form_dict failing because of them.

	document_id normally belongs to ONE File record; legacy data can have several
	(see the module docstring) - their respective business documents are deduplicated to unique (business_doctype,
	business_name) pairs, so that a business document with several associated files
	is never updated more than once. Each business document is processed and
	committed individually; an error on one does not prevent the others from being
	processed.

	Sets frappe.session.user explicitly to "Administrator" AFTER a successful secret
	check: as an allow_guest route this request would otherwise run as "Guest" -
	reproduced live that Document.add_tag() (through
	frappe.desk.doctype.tag.tag.update_tags(), doc.check_permission("write")) then
	fails with "no permission for file", as Guest has no write access to
	File/business documents normally. A scheduled job (e.g.
	paperless_sync.sync_pending_files()) does not have this problem, as Frappe's
	scheduler runs as "Administrator" anyway - the webhook therefore needs the
	elevation explicitly. This is safe because _verify_webhook_secret() already lets
	only a call with a valid, secret token get this far.
	"""
	settings = frappe.get_single("SVAN Paperless Settings")
	_verify_webhook_secret(settings)
	frappe.set_user("Administrator")

	if not document_id:
		log_sync_event(
			None,
			settings,
			"Warn",
			"webhook_sync.receive_update",
			title="Webhook rejected - document_id missing",
			details="The webhook call contained no (non-empty) 'document_id' field.",
		)
		frappe.db.commit()
		frappe.throw(_("document_id is missing."))
	document_id = frappe.utils.cint(document_id)

	file_names = frappe.get_all("File", filters={"svan_paperless_document_id": document_id}, pluck="name")
	if not file_names:
		log_sync_event(
			None,
			settings,
			"Info",
			"webhook_sync.receive_update",
			title="No linked file found - skipped",
			details=f"Paperless NGX document ID: {document_id}",
		)
		return

	business_records = {}
	for file_name in file_names:
		business_doctype, business_name = _resolve_business_record(file_name)
		if not business_doctype:
			log_sync_event(
				file_name,
				settings,
				"Info",
				"webhook_sync.receive_update",
				title="No (still existing) business document linked - skipped",
				details=f"Paperless NGX document ID: {document_id}",
			)
			continue
		business_records.setdefault((business_doctype, business_name), file_name)

	if not business_records:
		return

	document = _fetch_paperless_document(document_id, settings)
	if not document:
		return

	for (business_doctype, business_name), log_file_name in business_records.items():
		try:
			_apply_document_to_business_record(
				business_doctype, business_name, document, settings, log_file_name
			)
		except Exception:
			log_sync_event(
				log_file_name,
				settings,
				"Error",
				"webhook_sync.receive_update",
				title="Unexpected error while applying the changes",
				details=frappe.get_traceback(),
			)
		frappe.db.commit()
