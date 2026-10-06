# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
import requests

from svan_paperless_bridge.svan_paperless_ngx_bridge.doctype.svan_paperless_whitelisted_doctype.svan_paperless_whitelisted_doctype import (
	get_party_type_fieldname,
	resolve_value_field,
)
from svan_paperless_bridge.utils.sync_log import log_sync_event

DEFAULT_PAGE_SIZE = 100

# Duplicated from paperless_sync, which imports this module.
MAX_SAVE_ATTEMPTS = 5


def fetch_paperless_correspondents(settings, name=None):
	"""Fetch correspondents from Paperless NGX (GET /correspondents/), paginated.

	name (optional): a server-side filter (?name=, presumably icontains rather than
	iexact) - the caller still has to compare exactly itself.

	Raises on a connection/HTTP/JSON error; the caller does the logging.
	"""
	# get_password() also sees a token that is not saved yet.
	token = settings.get_password("api_token", raise_exception=False)
	headers = {"Authorization": f"Token {token}"}
	timeout = settings.api_call_timeout or 30

	url = settings.api_url.rstrip("/") + "/correspondents/"
	params = {"page_size": DEFAULT_PAGE_SIZE}
	if name:
		params["name"] = name

	correspondents = []
	while url:
		response = requests.get(url, headers=headers, params=params, timeout=timeout, verify=True)
		response.raise_for_status()
		payload = response.json()

		for entry in payload.get("results", []):
			if entry.get("id") is not None and entry.get("name"):
				correspondents.append({"id": entry["id"], "name": entry["name"]})

		url = payload.get("next")
		params = None

	return correspondents


def create_missing_correspondent(name, settings):
	"""Find a correspondent of the same name in Paperless NGX or create it via
	POST /correspondents/. Returns its ID.

	Raises on a connection/HTTP/JSON error; the caller does the logging.
	"""
	for existing in fetch_paperless_correspondents(settings, name=name):
		if existing["name"].strip().lower() == name.strip().lower():
			return existing["id"]

	token = settings.get_password("api_token", raise_exception=False)
	headers = {"Authorization": f"Token {token}"}
	url = settings.api_url.rstrip("/") + "/correspondents/"

	response = requests.post(
		url, headers=headers, data={"name": name}, timeout=settings.api_call_timeout or 30, verify=True
	)
	response.raise_for_status()
	return response.json()["id"]


def rename_paperless_correspondent(mapping_name, paperless_correspondent_id, new_name, settings):
	"""Rename an already mapped Paperless NGX correspondent via
	PATCH /correspondents/<id>/.

	Called from change_watch._rename_correspondent_if_mapped() when the display name
	of a party changes (e.g. Customer.customer_name) for which a "SVAN Paperless
	Correspondent Mapping" row already exists. Necessary because that mapping row is
	found by (frappe_party_type, frappe_party) - the STABLE party identity, not the
	display name: a pure name change still finds the row correctly but changes
	nothing about the paperless_correspondent_id already stored. Without this PATCH
	the correspondent in Paperless NGX would keep its old name permanently
	(analogous to tag_sync.rename_paperless_tag(), reproduced live).

	Raises on a connection/HTTP error - the caller is responsible for logging,
	analogous to the other functions in this module.
	"""
	token = settings.get_password("api_token", raise_exception=False)
	headers = {"Authorization": f"Token {token}"}
	url = settings.api_url.rstrip("/") + f"/correspondents/{paperless_correspondent_id}/"

	response = requests.patch(
		url, headers=headers, data={"name": new_name}, timeout=settings.api_call_timeout or 30, verify=True
	)
	response.raise_for_status()

	frappe.db.set_value(
		"SVAN Paperless Correspondent Mapping",
		mapping_name,
		"last_synced",
		frappe.utils.now_datetime(),
		update_modified=False,
	)


def get_configured_party_fieldname(attached_to_doctype):
	"""Return the party_fieldname configured for attached_to_doctype (a row in
	"SVAN Paperless Whitelisted DocType"), REGARDLESS of the current field value -
	unlike resolve_party(), which also reads the VALUE and returns (None, None,
	None) for an empty one.

	Needed to tell apart two cases that resolve_party() both treats as (None, None,
	None): "no party_fieldname is configured for this DocType at all" (a forward
	push should then still fall back to the global default correspondent) versus
	"party_fieldname is configured but currently empty" (a forward push should then
	explicitly CLEAR the correspondent instead of falling back to the default, see
	paperless_sync._resolve_upload_metadata()).
	"""
	return frappe.db.get_value(
		"SVAN Paperless Whitelisted DocType", {"document_type": attached_to_doctype}, "party_fieldname"
	)


def get_party_field(business_doctype):
	"""Return (fieldname, type_fieldname, target_doctype) for the party_fieldname
	configured on business_doctype.

	Link field: type_fieldname is None and target_doctype is the linked DocType.
	Dynamic Link (e.g. Quotation.party_name): target_doctype is None and the party
	type is read per document from type_fieldname (Quotation.quotation_to).
	Returns (None, None, None) if nothing usable is configured - defensive against
	rows that bypassed validate_party_fieldname() (legacy data, direct database
	access), since party_fieldname ends up in column queries.
	"""
	party_fieldname = get_configured_party_fieldname(business_doctype)
	field = resolve_value_field(business_doctype, party_fieldname)
	if not field:
		return None, None, None

	if field.fieldtype == "Link":
		return party_fieldname, None, field.options

	type_fieldname = get_party_type_fieldname(business_doctype, field)
	if type_fieldname:
		return party_fieldname, type_fieldname, None

	return None, None, None


def get_possible_party_types(business_doctype, type_fieldname):
	"""Return the party DocTypes a Dynamic Link party field can point at: the
	options of a Select type field, otherwise (Link to DocType, open set) every
	party type that already has a correspondent mapping row - a party without one
	has never been synced, so nothing in Paperless NGX depends on it yet.
	"""
	type_field = frappe.get_meta(business_doctype).get_field(type_fieldname)
	if type_field and type_field.fieldtype == "Select":
		return [option for option in (type_field.options or "").split("\n") if option]

	return frappe.get_all("SVAN Paperless Correspondent Mapping", pluck="frappe_party_type", distinct=True)


def resolve_party(attached_to_doctype, attached_to_name):
	"""Determine the party DocType, value and display name of a business document
	from its configured party field (see get_party_field()). The display name comes
	from the title field of the linked party master record.

	Returns (None, None, None) if no party field is configured or usable, or the
	party (or, for a Dynamic Link, its type) is empty on the document.
	"""
	fieldname, type_fieldname, party_type = get_party_field(attached_to_doctype)
	if not fieldname:
		return None, None, None

	if type_fieldname:
		party_type, party_value = frappe.db.get_value(
			attached_to_doctype, attached_to_name, [type_fieldname, fieldname]
		) or (None, None)
	else:
		party_value = frappe.db.get_value(attached_to_doctype, attached_to_name, fieldname)

	if not party_type or not party_value:
		return None, None, None

	return party_type, party_value, get_party_display_name(party_type, party_value)


def get_party_display_name(party_type, party_value):
	"""Resolve the display name of a party from its title field, falling back to
	the party value itself when the title field is empty.
	"""
	title_field = frappe.get_meta(party_type).get_title_field()
	return frappe.db.get_value(party_type, party_value, title_field) or party_value


def fetch_paperless_correspondent_name(paperless_correspondent_id, settings):
	"""Fetch the current name of ONE correspondent from Paperless NGX
	(GET /correspondents/<id>/) - for syncing back a changed correspondent (see
	webhook_sync._apply_correspondent()): the webhook call itself carries only the
	ID, not the current name.

	Raises on a connection, HTTP or JSON error - the caller is responsible for
	logging, analogous to tag_sync.fetch_paperless_tag_name().
	"""
	token = settings.get_password("api_token", raise_exception=False)
	headers = {"Authorization": f"Token {token}"}
	url = settings.api_url.rstrip("/") + f"/correspondents/{paperless_correspondent_id}/"

	response = requests.get(url, headers=headers, timeout=settings.api_call_timeout or 30, verify=True)
	response.raise_for_status()
	return response.json().get("name")


def get_paperless_correspondent_id_for_document(file_name, attached_to_doctype, attached_to_name, settings):
	"""Determine the document-specific Paperless NGX correspondent for the business
	document a file is attached to (attached_to_doctype/-name).

	Resolves the party (see resolve_party()) through "SVAN Paperless Correspondent
	Mapping" and creates a new correspondent automatically if needed.

	Returns None when no party can be determined or the creation fails - the caller
	(paperless_sync._resolve_upload_metadata()) then decides whether to fall back to
	the global default correspondent or to clear the correspondent in Paperless NGX
	explicitly.
	"""
	if not attached_to_doctype or not attached_to_name:
		return None

	party_type, party_value, display_name = resolve_party(attached_to_doctype, attached_to_name)
	if not party_type or not party_value:
		return None

	correspondent_id = frappe.db.get_value(
		"SVAN Paperless Correspondent Mapping",
		{"frappe_party_type": party_type, "frappe_party": party_value},
		"paperless_correspondent_id",
	)

	if correspondent_id:
		return correspondent_id

	return auto_create_correspondent_for_party(file_name, party_type, party_value, display_name, settings)


def auto_create_correspondent_for_party(
	file_name, party_type, party_value, display_name, settings, _retry=False
):
	"""Create a correspondent in Paperless NGX automatically for a party without an
	existing mapping row and store the mapping.

	If the correspondent found or created by name is already assigned to ANOTHER
	party (e.g. the same company as both Customer AND Supplier), that existing
	mapping is left untouched and the current party falls back to the default
	correspondent.

	If a concurrent process wins the race for the row (unique constraint violation
	on save), the function runs once more (_retry) and then works with the row that
	exists now - see tag_sync.ensure_paperless_tag_mapping().
	"""
	try:
		correspondent_id = create_missing_correspondent(display_name, settings)

		# paperless_correspondent_id is unique in the database: an upsert rather
		# than insert(), because create_missing_correspondent() can also return
		# an existing ID (a name match) for which a mapping row already exists.
		existing_name = frappe.db.get_value(
			"SVAN Paperless Correspondent Mapping", {"paperless_correspondent_id": correspondent_id}
		)
		if existing_name:
			existing = frappe.db.get_value(
				"SVAN Paperless Correspondent Mapping",
				existing_name,
				["frappe_party_type", "frappe_party"],
				as_dict=True,
			)
			if existing.frappe_party and (existing.frappe_party_type, existing.frappe_party) != (
				party_type,
				party_value,
			):
				log_sync_event(
					file_name,
					settings,
					"Warn",
					"auto_create_correspondent_for_party",
					title="Automatic mapping skipped - correspondent already mapped elsewhere",
					details=(
						f"Paperless NGX correspondent '{display_name}' (ID {correspondent_id}) is already "
						f"mapped to {existing.frappe_party_type} {existing.frappe_party}. "
						f"{party_type} {party_value} is left without a mapping and uses the default correspondent."
					),
				)
				return None
		else:
			existing_name = frappe.db.get_value(
				"SVAN Paperless Correspondent Mapping",
				{"frappe_party_type": party_type, "frappe_party": party_value},
			)

		mapping_doc = (
			frappe.get_doc("SVAN Paperless Correspondent Mapping", existing_name)
			if existing_name
			else frappe.new_doc("SVAN Paperless Correspondent Mapping")
		)

		mapping_doc.paperless_correspondent_id = correspondent_id
		mapping_doc.frappe_party_type = party_type
		mapping_doc.frappe_party = party_value
		mapping_doc.last_synced = frappe.utils.now_datetime()
		mapping_doc.flags.silent_bidirectional_override = True
		try:
			_save_mapping_doc(mapping_doc)
		except frappe.UniqueValidationError:
			if _retry:
				raise
			return auto_create_correspondent_for_party(
				file_name, party_type, party_value, display_name, settings, _retry=True
			)
	except Exception:
		log_sync_event(
			file_name,
			settings,
			"Error",
			"auto_create_correspondent_for_party",
			title="Automatic creation of the correspondent failed",
			details=f"Party: {party_type} {party_value} (name: {display_name})\n\n{frappe.get_traceback()}",
		)
		return None

	log_sync_event(
		file_name,
		settings,
		"Info",
		"auto_create_correspondent_for_party",
		title="Correspondent created automatically",
		details=f"Party: {party_type} {party_value}\nCorrespondent: {display_name} (ID {correspondent_id})",
	)

	return correspondent_id


def _save_mapping_doc(mapping_doc):
	"""Insert or save a "SVAN Paperless Correspondent Mapping" row inside a
	savepoint - see tag_sync._save_mapping_doc() for the rationale (a concurrent
	process can create a row with the same paperless_correspondent_id in between;
	the caller then looks the now existing row up again).
	"""
	frappe.db.savepoint("svan_correspondent_mapping")
	try:
		if mapping_doc.is_new():
			mapping_doc.insert(ignore_permissions=True)
		else:
			mapping_doc.save(ignore_permissions=True)
	except frappe.UniqueValidationError:
		frappe.db.rollback(save_point="svan_correspondent_mapping")
		raise
	frappe.db.release_savepoint("svan_correspondent_mapping")


def _paperless_correspondent_exists(paperless_correspondent_id, settings):
	"""Check via GET /correspondents/<id>/ whether a Paperless NGX correspondent
	still exists - analogous to tag_sync._paperless_tag_exists(): only an
	unambiguous HTTP 404 counts as "no longer exists".
	"""
	token = settings.get_password("api_token", raise_exception=False)
	headers = {"Authorization": f"Token {token}"}
	url = settings.api_url.rstrip("/") + f"/correspondents/{paperless_correspondent_id}/"
	try:
		response = requests.get(url, headers=headers, timeout=settings.api_call_timeout or 30, verify=True)
	except Exception:
		return True
	return response.status_code != 404


def is_party_bidirectional(party_type, party_value):
	"""Return the "Bidirectional" value of the mapping row of a party - False if
	the party has no mapping row (nothing is synced back for it, analogous to tags).
	"""
	return bool(
		frappe.db.get_value(
			"SVAN Paperless Correspondent Mapping",
			{"frappe_party_type": party_type, "frappe_party": party_value},
			"bidirectional",
		)
	)


def ensure_party_for_paperless_correspondent(
	file_name, target_doctype, paperless_correspondent_id, settings, match_by_title=True, _retry=False
):
	"""The reverse of auto_create_correspondent_for_party(), analogous to
	tag_sync.ensure_frappe_tag_for_paperless_id(): determine the party record of
	target_doctype that belongs to a Paperless NGX correspondent (handed over by
	webhook_sync._resolve_party_swap_change() after a change IN Paperless NGX) and
	make sure a "SVAN Paperless Correspondent Mapping" row exists for it.

	If a row for this paperless_correspondent_id exists, its frappe_party is only
	returned when "bidirectional" is enabled there - otherwise None, meaning syncing
	back is deliberately disabled for exactly this correspondent. A row pointing at a
	party of ANOTHER DocType than target_doctype is a conflict (Warn, None).

	Without a row and with match_by_title=False (a Dynamic Link party field, where
	the type of the matched record could be wrong for the business document), the
	change is skipped with an Info entry (None). Otherwise the current correspondent
	name is loaded and exactly ONE record
	in target_doctype is searched by its title field (exactly, not "like"). 0
	matches -> "" (nothing to map; the caller decides whether the link is cleared -
	a new master record is never created automatically, mandatory fields cannot be
	filled generically). >1 matches -> error log, None. Exactly 1 match -> its
	mapping row is created ("bidirectional" = field default, set to 0 by the
	controller for party types that cannot be renamed). If that party already has a
	row for ANOTHER Paperless NGX correspondent, it is a real conflict while the old
	correspondent still exists there (Warn, None); if the old one is gone (deleted
	and recreated under the same name), the row is relinked to the new ID and keeps
	its "bidirectional" value.

	If a concurrent process wins the race for the row (unique constraint on
	paperless_correspondent_id), the function runs once more (_retry) and then
	works with the row that exists now; only if that fails as well, a Warn is logged
	and None returned.

	Returns the party name to relink to (only for a bidirectional mapping), "" (no
	match), or None (do nothing: sync back disabled, conflict, ambiguity, failure).
	"""
	existing = frappe.db.get_value(
		"SVAN Paperless Correspondent Mapping",
		{"paperless_correspondent_id": paperless_correspondent_id},
		["frappe_party_type", "frappe_party", "bidirectional"],
		as_dict=True,
	)
	if existing and existing.frappe_party:
		if existing.frappe_party_type != target_doctype:
			log_sync_event(
				file_name,
				settings,
				"Warn",
				"ensure_party_for_paperless_correspondent",
				title="Correspondent change skipped - mapped to the wrong party type",
				details=(
					f"Paperless NGX correspondent ID {paperless_correspondent_id} is already mapped to "
					f"{existing.frappe_party_type} {existing.frappe_party}, but a record of type "
					f"{target_doctype} was expected."
				),
			)
			return None
		return existing.frappe_party if existing.bidirectional else None

	if not match_by_title:
		log_sync_event(
			file_name,
			settings,
			"Info",
			"ensure_party_for_paperless_correspondent",
			title="Correspondent change skipped - no mapping for a dynamic party field",
			details=(
				f"Paperless NGX correspondent ID {paperless_correspondent_id} has no mapping row. "
				f"A dynamic party field is only relinked to parties of type {target_doctype} "
				"that already have one."
			),
		)
		return None

	try:
		display_name = fetch_paperless_correspondent_name(paperless_correspondent_id, settings)
	except Exception:
		log_sync_event(
			file_name,
			settings,
			"Error",
			"ensure_party_for_paperless_correspondent",
			title="Correspondent name could not be loaded from Paperless NGX",
			details=(
				f"Paperless NGX correspondent ID: {paperless_correspondent_id}\n\n{frappe.get_traceback()}"
			),
		)
		return None
	if not display_name:
		return ""

	title_field = frappe.get_meta(target_doctype).get_title_field()
	matches = frappe.get_all(target_doctype, filters={title_field: display_name}, pluck="name")

	if len(matches) > 1:
		log_sync_event(
			file_name,
			settings,
			"Error",
			"ensure_party_for_paperless_correspondent",
			title="Correspondent change skipped - ambiguous match",
			details=(
				f"{len(matches)} {target_doctype} records found with {title_field} = '{display_name}' "
				f"({', '.join(matches)}) - no unambiguous mapping possible, no change made."
			),
		)
		return None

	if not matches:
		return ""

	party_value = matches[0]

	# A legacy row for this ID without a party is filled instead of duplicated.
	mapping_doctype = "SVAN Paperless Correspondent Mapping"
	existing_name = frappe.db.get_value(
		mapping_doctype, {"paperless_correspondent_id": paperless_correspondent_id}
	) or frappe.db.get_value(
		mapping_doctype, {"frappe_party_type": target_doctype, "frappe_party": party_value}
	)
	relinked = False
	if existing_name:
		old_id = frappe.db.get_value(mapping_doctype, existing_name, "paperless_correspondent_id")
		if old_id and old_id != paperless_correspondent_id:
			if _paperless_correspondent_exists(old_id, settings):
				log_sync_event(
					file_name,
					settings,
					"Warn",
					"ensure_party_for_paperless_correspondent",
					title="Automatic mapping skipped - party already mapped elsewhere",
					details=(
						f"{target_doctype} {party_value} is already mapped to Paperless NGX correspondent "
						f"ID {old_id}. Paperless NGX correspondent ID {paperless_correspondent_id} is left "
						"without a mapping."
					),
				)
				return None
			relinked = True

	party_label = f"Party: {target_doctype} {party_value}"
	mapping_doc = (
		frappe.get_doc("SVAN Paperless Correspondent Mapping", existing_name)
		if existing_name
		else frappe.new_doc("SVAN Paperless Correspondent Mapping")
	)
	mapping_doc.paperless_correspondent_id = paperless_correspondent_id
	mapping_doc.frappe_party_type = target_doctype
	mapping_doc.frappe_party = party_value
	mapping_doc.last_synced = frappe.utils.now_datetime()
	mapping_doc.flags.silent_bidirectional_override = True
	try:
		_save_mapping_doc(mapping_doc)
	except frappe.UniqueValidationError:
		if not _retry:
			return ensure_party_for_paperless_correspondent(
				file_name, target_doctype, paperless_correspondent_id, settings, match_by_title, _retry=True
			)
		log_sync_event(
			file_name,
			settings,
			"Warn",
			"ensure_party_for_paperless_correspondent",
			title="Mapping could not be saved - conflicting row created concurrently",
			details=f"Paperless NGX correspondent ID: {paperless_correspondent_id}\n{party_label}",
		)
		return None

	log_sync_event(
		file_name,
		settings,
		"Info",
		"ensure_party_for_paperless_correspondent",
		title=(
			"Mapping relinked to recreated Paperless correspondent"
			if relinked
			else "Mapping created automatically"
		),
		details=f"Paperless NGX correspondent ID: {paperless_correspondent_id}\n{party_label}",
	)
	return party_value if mapping_doc.bidirectional else None


def can_rename_via_title_field(party_type):
	"""Check GENERICALLY, without any real database write, whether
	rename_frappe_party_from_paperless() can take effect for party_type at all.

	Some party DocTypes recalculate their title field from OTHER fields on save -
	"Contact", for example: Contact.validate() ALWAYS rebuilds full_name from
	first_name/middle_name/last_name/company_name. A plain doc.set(title_field,
	...); doc.save() would seem to run through there without an error, but the value
	set would be discarded again within the same validate() (reproduced live with
	"asad" -> "asad2" - the function wrongly logged "renamed successfully" although
	nothing had changed). The DocType meta alone does not reveal this (full_name is
	a perfectly normal, writable Data field, no fetch_from) - hence a real but
	purely in-memory test here: a frappe.new_doc(party_type) that is NEVER saved
	gets its title field set to a test value, then validate() runs (including doc
	event hooks) WITHOUT any database access - if the test value is still there
	afterwards, renaming counts as supported.

	If the title field is "name" itself, the rename goes through
	frappe.rename_doc() rather than through a field - which always works,
	independently of validate().

	An exception during the probe (e.g. because validate() requires other mandatory
	fields) counts as "not supported" (fail-closed) - at worst that prevents a
	rename that would actually have worked, never the other way round, i.e. a false
	success message as in the Contact case above.
	"""
	title_field = frappe.get_meta(party_type).get_title_field()
	if title_field == "name":
		return True

	probe_value = "__svan_paperless_rename_probe__"
	try:
		doc = frappe.new_doc(party_type)
		doc.set(title_field, probe_value)
		doc.run_method("validate")
	except Exception:
		return False

	return doc.get(title_field) == probe_value


def get_party_rename_supported(party_type):
	"""Return whether renaming the party master record (see
	rename_frappe_party_from_paperless()) technically takes effect for party_type.

	Prefers the flag already determined by can_rename_via_title_field() on a
	whitelist row with a Link party field to this party_type, which avoids
	repeating the in-memory probe for every mapping row. Without such a row (deleted
	meanwhile, or only Dynamic Link fields point at this type) the probe runs
	directly.
	"""
	for row in frappe.get_all(
		"SVAN Paperless Whitelisted DocType",
		fields=["document_type", "party_rename_supported"],
	):
		fieldname, type_fieldname, target_doctype = get_party_field(row.document_type)
		if fieldname and not type_fieldname and target_doctype == party_type:
			return bool(row.party_rename_supported)

	return can_rename_via_title_field(party_type)


def rename_frappe_party_from_paperless(file_name, party_type, party_value, new_display_name, settings):
	"""Rename the display name of a party master record (e.g.
	Customer.customer_name) when the corresponding Paperless NGX correspondent was
	renamed there in the meantime - the sync back for
	correspondent_sync.rename_paperless_correspondent() (the forward direction),
	analogous to tag_sync.rename_frappe_tag_from_paperless() for tags.

	Called from paperless_sync._reconcile_correspondent_renames() (a phase of
	reconcile_metadata_changes()) - a pure correspondent rename without an
	accompanying document change does NOT trigger a webhook in Paperless NGX (see
	the rationale there), so there is (unlike for relinking the party link, see
	webhook_sync._resolve_party_swap_change()) no reactive caller.

	Two cases depending on the DocType configuration: if the title field of
	party_type is "name" itself (the record name CARRIES the display name, with no
	separate title field), the rename goes through frappe.rename_doc() -
	party_value (= the previous name) changes as a result, and "SVAN Paperless
	Correspondent Mapping.frappe_party" is updated along with it by
	frappe.rename_doc() automatically by cascade (a link field). Otherwise (the more
	common case, e.g. Customer.customer_name) only the VALUE of the separate title
	field is set via doc.save() (up to MAX_SAVE_ATTEMPTS attempts, analogous to
	paperless_sync.save_uuid()) - party_value (the stable ID) stays unchanged. Unlike
	for tags there is NO name conflict check beforehand: the display name of a party
	(customer_name or similar) is usually NOT globally unique - a conflict would
	only arise from a real unique constraint and would be handled by doc.save()
	itself like any other save error.

	A failure must never prevent the remaining changes from being applied - both
	callers handle it in an isolated try/except.
	"""
	title_field = frappe.get_meta(party_type).get_title_field()

	if title_field == "name":
		if party_value == new_display_name:
			return
		try:
			frappe.rename_doc(party_type, party_value, new_display_name)
		except Exception:
			log_sync_event(
				file_name,
				settings,
				"Warn",
				"rename_frappe_party_from_paperless",
				title="Automatic rename failed",
				details=(f"{party_type} '{party_value}' -> '{new_display_name}'\n\n{frappe.get_traceback()}"),
			)
			return
		log_sync_event(
			file_name,
			settings,
			"Info",
			"rename_frappe_party_from_paperless",
			title="Party master record renamed automatically",
			details=f"{party_type}: '{party_value}' -> '{new_display_name}'",
		)
		return

	current_value = frappe.db.get_value(party_type, party_value, title_field)
	if current_value == new_display_name:
		return

	for attempt in range(1, MAX_SAVE_ATTEMPTS + 1):
		try:
			doc = frappe.get_doc(party_type, party_value)
			doc.set(title_field, new_display_name)
			doc.save(ignore_permissions=True)
		except Exception:
			if attempt == MAX_SAVE_ATTEMPTS:
				log_sync_event(
					file_name,
					settings,
					"Warn",
					"rename_frappe_party_from_paperless",
					title=f"Rename failed after {MAX_SAVE_ATTEMPTS} attempts",
					details=(
						f"{party_type} {party_value}.{title_field}: '{current_value}' -> "
						f"'{new_display_name}'\n\n{frappe.get_traceback()}"
					),
				)
			continue
		else:
			log_sync_event(
				file_name,
				settings,
				"Info",
				"rename_frappe_party_from_paperless",
				title="Party master record renamed automatically",
				details=f"{party_type} {party_value}.{title_field}: '{current_value}' -> '{new_display_name}'",
			)
			return


def repair_stale_correspondent_id(file_name, invalid_paperless_correspondent_id, settings):
	"""Repair the mapping row of a correspondent deleted directly in Paperless
	NGX - analogous to tag_sync.repair_stale_tag_id().

	Called from paperless_sync.query_uuid() when Paperless NGX rejects the upload
	with "Invalid pk ... - object does not exist" for the "correspondent" field.

	Resolves the affected party again through
	auto_create_correspondent_for_party() (finding a correspondent of the same name
	or creating a new one) and thereby updates the same mapping row in place. Unlike
	in the regular call through get_paperless_correspondent_id_for_document(), the
	display name does not come from resolve_party() here (no linked business
	document is known) but is determined directly from the stored party through
	get_party_display_name().

	If NO mapping row is found for this ID, it may be the global default
	correspondent (settings.default_correspondent) instead - that one is linked not
	through a mapping row but directly through
	settings.svan_default_correspondent_id and is checked/repaired separately by
	repair_stale_default_correspondent().

	Returns True if something was found and repaired (the caller should then retry
	the upload with a freshly resolved correspondent ID), otherwise False.
	"""
	mapping_name = frappe.db.get_value(
		"SVAN Paperless Correspondent Mapping",
		{"paperless_correspondent_id": invalid_paperless_correspondent_id},
	)
	if not mapping_name:
		if str(invalid_paperless_correspondent_id) == str(settings.svan_default_correspondent_id):
			return repair_stale_default_correspondent(file_name, settings)
		return False

	mapping = frappe.db.get_value(
		"SVAN Paperless Correspondent Mapping",
		mapping_name,
		["frappe_party_type", "frappe_party"],
		as_dict=True,
	)
	if not mapping.frappe_party_type or not mapping.frappe_party:
		return False

	display_name = get_party_display_name(mapping.frappe_party_type, mapping.frappe_party)
	new_id = auto_create_correspondent_for_party(
		file_name, mapping.frappe_party_type, mapping.frappe_party, display_name, settings
	)
	return bool(new_id)


def repair_stale_default_correspondent(file_name, settings):
	"""Repair the global default correspondent (SVAN Paperless
	Settings.default_correspondent) when it was deleted directly in Paperless NGX.

	Unlike for the party/mapping based correspondents (see
	repair_stale_correspondent_id()) there is no mapping row for it - the ID sits
	directly in the settings and serves as the fallback for documents without a
	document-specific correspondent (see paperless_sync.query_uuid()).

	Triggers the same name matching/creation mechanism through
	SVANPaperlessSettings.resolve_default_correspondent_id() as saving the settings
	manually does ("test connection"), but updates the ID silently via db_set()
	instead of a full Document.save() - that would run validate() again and
	therefore trigger the same name matching a second time unnecessarily.

	Returns True on a successful repair (the caller should then retry the upload
	with a freshly resolved correspondent ID), otherwise False.
	"""
	try:
		new_id, created = settings.resolve_default_correspondent_id()
		if not new_id:
			return False
		settings.db_set("svan_default_correspondent_id", new_id, update_modified=False)
	except Exception:
		log_sync_event(
			file_name,
			settings,
			"Error",
			"repair_stale_default_correspondent",
			title="Repairing the default correspondent failed",
			details=f"Default correspondent: {settings.default_correspondent}\n\n{frappe.get_traceback()}",
		)
		return False

	log_sync_event(
		file_name,
		settings,
		"Info",
		"repair_stale_default_correspondent",
		title="Default correspondent automatically " + ("created" if created else "remapped"),
		details=f"Default correspondent: {settings.default_correspondent} (new ID {new_id})",
	)
	return True
