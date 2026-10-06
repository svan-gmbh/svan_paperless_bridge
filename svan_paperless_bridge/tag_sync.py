# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
import requests

from svan_paperless_bridge.utils.sync_log import log_sync_event

DEFAULT_PAGE_SIZE = 100


def fetch_paperless_tags(settings, name=None):
	"""Fetch tags from Paperless NGX (GET /tags/).

	Follows the "next" link of DRF's standard pagination ({"count", "next",
	"previous", "results"}) until all pages have been retrieved. Raises on a
	connection, HTTP or JSON error - the caller (create_missing_tag) is responsible
	for logging and error handling, analogous to query_uuid() in paperless_sync.py.

	name (optional): a server-side name filter (?name=), analogous to
	SVANPaperlessSettings.test_paperless_connection() - for create_missing_tag() it
	reduces the number of pages fetched from "all tags" to "those matching this
	name", instead of paginating the full list for every tag to be created. As
	Paperless NGX presumably implements the filter as "icontains" rather than
	"iexact", the caller still has to compare exactly itself.

	Returns a list of dicts {"id": int, "name": str}. Entries without an "id" or
	without a (non-empty) "name" are skipped.
	"""
	# get_password() also sees a token that is not saved yet.
	token = settings.get_password("api_token", raise_exception=False)
	headers = {"Authorization": f"Token {token}"}
	timeout = settings.api_call_timeout or 30

	url = settings.api_url.rstrip("/") + "/tags/"
	params = {"page_size": DEFAULT_PAGE_SIZE}
	if name:
		params["name"] = name

	tags = []
	while url:
		response = requests.get(url, headers=headers, params=params, timeout=timeout, verify=True)
		response.raise_for_status()
		payload = response.json()

		for entry in payload.get("results", []):
			if entry.get("id") is not None and entry.get("name"):
				tags.append({"id": entry["id"], "name": entry["name"]})

		# "next" is a complete URL including all query parameters.
		url = payload.get("next")
		params = None

	return tags


def create_missing_tag(name, settings):
	"""Create a tag with the given name in Paperless NGX.

	Only called when ensure_paperless_tag_mapping() finds no existing mapping row
	for a Frappe tag.

	Checks case-insensitively beforehand via fetch_paperless_tags() whether a tag of
	that name already exists (analogous to
	correspondent_sync.create_missing_correspondent()) - so no duplicate tag is
	created. Otherwise it is created via POST /tags/ ("name" is the only mandatory
	field according to TagSerializer/the model; "color", "matching_algorithm",
	"is_insensitive" and "is_inbox_tag" have model defaults).

	Returns the (found or newly created) Paperless NGX tag ID. Raises on a
	connection, HTTP or JSON error - the caller is responsible for logging and the
	fallback.
	"""
	for existing in fetch_paperless_tags(settings, name=name):
		if existing["name"].strip().lower() == name.strip().lower():
			return existing["id"]

	token = settings.get_password("api_token", raise_exception=False)
	headers = {"Authorization": f"Token {token}"}
	url = settings.api_url.rstrip("/") + "/tags/"

	response = requests.post(
		url, headers=headers, data={"name": name}, timeout=settings.api_call_timeout or 30, verify=True
	)
	response.raise_for_status()
	return response.json()["id"]


def _save_mapping_doc(mapping_doc):
	"""Insert or save a "SVAN Paperless Tag Mapping" row inside a savepoint.

	Another process (e.g. a parallel upload or webhook call) can create a row with
	the same paperless_tag_id or frappe_tag between our lookup and this write. The
	unique constraint then raises frappe.UniqueValidationError; the savepoint is
	rolled back (so earlier work of the transaction survives) and the error is
	re-raised for the caller, which resolves the situation by looking the now
	existing row up again instead of aborting.
	"""
	frappe.db.savepoint("svan_tag_mapping")
	try:
		if mapping_doc.is_new():
			mapping_doc.insert(ignore_permissions=True)
		else:
			mapping_doc.save(ignore_permissions=True)
	except frappe.UniqueValidationError:
		frappe.db.rollback(save_point="svan_tag_mapping")
		raise
	frappe.db.release_savepoint("svan_tag_mapping")


def ensure_paperless_tag_mapping(file_name, frappe_tag, settings, _retry=False):
	"""Create a tag in Paperless NGX automatically for a Frappe tag without an
	existing mapping row (if needed) and store the mapping.

	Called from get_paperless_tag_ids_for_document() when no "SVAN Paperless Tag
	Mapping" row with a paperless_tag_id exists for the Frappe tag yet. A failure
	(network/API error) is logged and falls back to None like a missing mapping -
	automatic creation must never prevent a file upload.

	If the Paperless tag found or created by name is already assigned to ANOTHER
	Frappe tag (e.g. two Frappe tags differing only in capitalization), that
	existing mapping is NOT overwritten - "as long as this field is filled in, it is
	never changed automatically again" (see the field description of frappe_tag in
	the DocType JSON) also applies when the automation itself runs into a foreign
	row. The current Frappe tag is then left without a mapping.

	Race safeguard (same as
	correspondent_sync.auto_create_correspondent_for_party()): create_missing_tag()
	can return an already existing Paperless NGX ID (a name match rather than a new
	creation), so an upsert is used here instead of a blind insert() to avoid
	triggering a unique constraint error on paperless_tag_id. If a row with a
	DIFFERENT (e.g. stale, deleted in Paperless NGX) paperless_tag_id exists for the
	same Frappe tag instead, the same upsert also covers the unique constraint on
	frappe_tag - insert() would fail there as well otherwise.

	If a concurrent process wins the race for the row (unique constraint violation
	on save), the function runs once more (_retry) and then works with the row that
	exists now - through the lookups below it is found and confirmed instead of being
	created a second time.

	Returns the new/found paperless_tag_id, or None.
	"""
	try:
		paperless_tag_id = create_missing_tag(frappe_tag, settings)

		existing_name = frappe.db.get_value(
			"SVAN Paperless Tag Mapping", {"paperless_tag_id": paperless_tag_id}
		)
		if existing_name:
			existing_frappe_tag = frappe.db.get_value(
				"SVAN Paperless Tag Mapping", existing_name, "frappe_tag"
			)
			if existing_frappe_tag and existing_frappe_tag != frappe_tag:
				log_sync_event(
					file_name,
					settings,
					"Warn",
					"ensure_paperless_tag_mapping",
					title="Automatic mapping skipped - tag already mapped elsewhere",
					details=(
						f"Paperless NGX tag ID {paperless_tag_id} is already mapped to Frappe tag "
						f"'{existing_frappe_tag}'. Frappe tag '{frappe_tag}' is left without a mapping."
					),
				)
				return None
		else:
			existing_name = frappe.db.get_value("SVAN Paperless Tag Mapping", {"frappe_tag": frappe_tag})

		mapping_doc = (
			frappe.get_doc("SVAN Paperless Tag Mapping", existing_name)
			if existing_name
			else frappe.new_doc("SVAN Paperless Tag Mapping")
		)

		mapping_doc.paperless_tag_id = paperless_tag_id
		mapping_doc.frappe_tag = frappe_tag
		mapping_doc.last_synced = frappe.utils.now_datetime()
		try:
			_save_mapping_doc(mapping_doc)
		except frappe.UniqueValidationError:
			if _retry:
				raise
			return ensure_paperless_tag_mapping(file_name, frappe_tag, settings, _retry=True)
	except Exception:
		log_sync_event(
			file_name,
			settings,
			"Error",
			"ensure_paperless_tag_mapping",
			title="Automatic creation of the tag failed",
			details=f"Frappe tag: {frappe_tag}\n\n{frappe.get_traceback()}",
		)
		return None

	log_sync_event(
		file_name,
		settings,
		"Info",
		"ensure_paperless_tag_mapping",
		title="Tag created automatically",
		details=f"Frappe tag: {frappe_tag}\nPaperless NGX tag ID: {paperless_tag_id}",
	)

	return paperless_tag_id


def repair_stale_tag_id(file_name, invalid_paperless_tag_id, settings):
	"""Repair the mapping row of a tag deleted directly in Paperless NGX.

	Called from paperless_sync.query_uuid() when Paperless NGX rejects the upload
	with "Invalid pk ... - object does not exist" for the "tags" field - the cached
	paperless_tag_id of a mapping row no longer exists then, without
	get_paperless_tag_ids_for_document() having been able to detect that beforehand
	(the row does have a value set, it is merely stale).

	Resolves the affected Frappe tag again through ensure_paperless_tag_mapping()
	(finding a Paperless tag of the same name or creating a new one) and thereby
	updates the same mapping row in place - the unique constraint on frappe_tag,
	together with the frappe_tag lookup in ensure_paperless_tag_mapping(), ensures
	no duplicate is created.

	Returns True if a row was found and repaired (the caller should then retry the
	upload with freshly resolved tag IDs), otherwise False.
	"""
	mapping_name = frappe.db.get_value(
		"SVAN Paperless Tag Mapping", {"paperless_tag_id": invalid_paperless_tag_id}
	)
	if not mapping_name:
		return False

	frappe_tag = frappe.db.get_value("SVAN Paperless Tag Mapping", mapping_name, "frappe_tag")
	if not frappe_tag:
		return False

	return bool(ensure_paperless_tag_mapping(file_name, frappe_tag, settings))


def rename_paperless_tag(mapping_name, paperless_tag_id, new_name, settings):
	"""Rename an already mapped Paperless NGX tag via PATCH /tags/<id>/.

	Called from change_watch.handle_tag_rename() when a Frappe tag was renamed for
	which a "SVAN Paperless Tag Mapping" row already exists. Necessary because
	"frappe_tag" is a link field to "Tag" - frappe.rename_doc() updates that
	reference to the new name automatically by cascade (see
	frappe/model/rename_doc.py: update_link_field_values()) WITHOUT
	paperless_tag_id changing. Without this PATCH the mapping row would then point
	at the new Frappe tag name correctly but still refer to the same, unchanged
	Paperless NGX tag - a rename would have stayed completely invisible to Paperless
	NGX (reproduced live: the metadata update ran through successfully without
	anything changing visibly in Paperless NGX).

	Raises on a connection/HTTP error - the caller (handle_tag_rename()) is
	responsible for logging, analogous to the other functions in this module.
	"""
	token = settings.get_password("api_token", raise_exception=False)
	headers = {"Authorization": f"Token {token}"}
	url = settings.api_url.rstrip("/") + f"/tags/{paperless_tag_id}/"

	response = requests.patch(
		url, headers=headers, data={"name": new_name}, timeout=settings.api_call_timeout or 30, verify=True
	)
	response.raise_for_status()

	frappe.db.set_value(
		"SVAN Paperless Tag Mapping",
		mapping_name,
		"last_synced",
		frappe.utils.now_datetime(),
		update_modified=False,
	)


def fetch_paperless_tag_name(paperless_tag_id, settings):
	"""Fetch the name of ONE tag from Paperless NGX (GET /tags/<id>/).

	Called from ensure_frappe_tag_for_paperless_id() (the reverse direction, see
	there) - unlike fetch_paperless_tags() this is not a list/pagination call, as
	the ID is already known here. Raises on a connection, HTTP or JSON error - the
	caller is responsible for logging.
	"""
	token = settings.get_password("api_token", raise_exception=False)
	headers = {"Authorization": f"Token {token}"}
	url = settings.api_url.rstrip("/") + f"/tags/{paperless_tag_id}/"

	response = requests.get(url, headers=headers, timeout=settings.api_call_timeout or 30, verify=True)
	response.raise_for_status()
	return response.json().get("name")


def rename_frappe_tag_from_paperless(file_name, paperless_tag_id, current_frappe_tag, new_name, settings):
	"""The reverse of rename_paperless_tag(): renames a Frappe tag when the
	corresponding Paperless NGX tag was renamed there in the meantime - called from
	ensure_frappe_tag_for_paperless_id() for every already mapped, bidirectional row
	whose current name differs.

	If ANOTHER Frappe tag with the new name already exists, the rename is skipped
	and the previous name kept (a conflict, analogous to the other mapping conflicts
	in this module) - two tag identities are never merged automatically.
	frappe.rename_doc() updates "SVAN Paperless Tag Mapping.frappe_tag" (a link to
	"Tag") automatically by cascade, just as it does for a rename directly in ERPNext
	(see change_watch.handle_tag_rename()).

	Returns the (possibly unchanged) Frappe tag name - a failure must never prevent
	the remaining tags/fields from being applied.
	"""
	if frappe.db.exists("Tag", new_name):
		log_sync_event(
			file_name,
			settings,
			"Warn",
			"rename_frappe_tag_from_paperless",
			title="Automatic rename skipped - target tag already exists",
			details=(
				f"Paperless NGX tag ID {paperless_tag_id}: '{current_frappe_tag}' -> '{new_name}' "
				f"not carried out, tag '{new_name}' already exists in ERPNext."
			),
		)
		return current_frappe_tag

	try:
		# frappe.rename_doc() has no ignore_permissions; callers already run as Administrator.
		frappe.rename_doc("Tag", current_frappe_tag, new_name)
	except Exception:
		log_sync_event(
			file_name,
			settings,
			"Warn",
			"rename_frappe_tag_from_paperless",
			title="Automatic rename failed",
			details=f"Paperless NGX tag ID {paperless_tag_id}: '{current_frappe_tag}' -> '{new_name}'\n\n{frappe.get_traceback()}",
		)
		return current_frappe_tag

	log_sync_event(
		file_name,
		settings,
		"Info",
		"rename_frappe_tag_from_paperless",
		title="Frappe tag renamed automatically (change from Paperless NGX applied)",
		details=f"Paperless NGX tag ID {paperless_tag_id}: '{current_frappe_tag}' -> '{new_name}'",
	)
	return new_name


def _paperless_tag_exists(paperless_tag_id, settings):
	"""Check via GET /tags/<id>/ whether a Paperless NGX tag ID still exists -
	called from ensure_frappe_tag_for_paperless_id() to tell a real name conflict
	apart from an orphaned (deleted) old mapping.

	Only an unambiguous HTTP 404 counts as "no longer exists" - any other failure
	(network, 5xx, ...) conservatively counts as "still exists", so that a transient
	error never wrongly relinks an existing, valid mapping.
	"""
	token = settings.get_password("api_token", raise_exception=False)
	headers = {"Authorization": f"Token {token}"}
	url = settings.api_url.rstrip("/") + f"/tags/{paperless_tag_id}/"
	try:
		response = requests.get(url, headers=headers, timeout=settings.api_call_timeout or 30, verify=True)
	except Exception:
		return True
	return response.status_code != 404


def ensure_frappe_tag_for_paperless_id(file_name, paperless_tag_id, settings, _retry=False):
	"""The reverse of ensure_paperless_tag_mapping(): ensures a matching Frappe tag
	exists for a Paperless NGX tag (handed over by webhook_sync.receive_update()
	after a change IN Paperless NGX).

	If a "SVAN Paperless Tag Mapping" row already exists for this paperless_tag_id,
	its frappe_tag is only returned when "bidirectional" is enabled there (default
	1) - otherwise None, meaning syncing back is deliberately disabled for EXACTLY
	this tag (syncing ERPNext -> Paperless NGX is unaffected, see
	get_paperless_tag_ids_for_document()). For an already mapped, bidirectional row
	it is additionally checked via fetch_paperless_tag_name() whether the Paperless
	NGX tag was renamed in the meantime (the webhook call itself carries only IDs,
	see the module docstring of webhook_sync.py) - if so, the Frappe tag is brought
	in line right away through rename_frappe_tag_from_paperless(). Without that
	comparison, a rename IN Paperless NGX would stay permanently invisible to
	ERPNext (the counterpart to change_watch.handle_tag_rename() for the forward
	direction, reproduced live). A failure while loading the current name is
	uncritical - the existing mapping simply remains valid in that case.

	If NO mapping row exists yet, the tag name is loaded via
	fetch_paperless_tag_name(), a Frappe tag of the same name (DocType "Tag") is
	created if needed and the mapping stored (bidirectional = 1, the field default) -
	an upsert over the unique constraint on frappe_tag, analogous to
	ensure_paperless_tag_mapping().

	If the loaded tag name is already assigned to ANOTHER Paperless tag (a mapping
	row with the same frappe_tag but a different paperless_tag_id), two cases are
	distinguished: if that row's OLD Paperless tag still exists (checked by GET, see
	_paperless_tag_exists()), it is a real name conflict - the existing mapping is
	NOT overwritten (analogous to the inverse conflict case in
	ensure_paperless_tag_mapping()). If the old tag does NOT exist there any more
	(e.g. deleted and recreated under the same name), the existing mapping row is
	relinked to the new ID instead - its "bidirectional" value is left untouched, so
	a restored tag does not lose configuration that was set deliberately before.
	Reproduced live: without this distinction, a tag deleted in Paperless NGX and
	recreated under the same name stayed permanently without a mapping in ERPNext,
	although nothing spoke against syncing it back.

	If a concurrent process wins the race for the mapping row (unique constraint
	violation on save), the function runs once more (_retry) and then works with the
	row that exists now (including its "bidirectional" value) instead of creating it
	a second time. Only if that fails as well, a Warn is logged and None returned.

	Returns the (found, renamed, relinked or newly created) Frappe tag name, or None
	(failure while loading the name, or a real conflict, see above) - a tag that
	cannot be applied must never prevent the remaining changes from being applied.
	"""
	mapping = frappe.db.get_value(
		"SVAN Paperless Tag Mapping",
		{"paperless_tag_id": paperless_tag_id},
		["frappe_tag", "bidirectional"],
		as_dict=True,
	)
	if mapping:
		if not mapping.bidirectional:
			return None
		try:
			current_name = fetch_paperless_tag_name(paperless_tag_id, settings)
		except Exception:
			return mapping.frappe_tag
		if current_name and current_name != mapping.frappe_tag:
			return rename_frappe_tag_from_paperless(
				file_name, paperless_tag_id, mapping.frappe_tag, current_name, settings
			)
		return mapping.frappe_tag

	try:
		tag_name = fetch_paperless_tag_name(paperless_tag_id, settings)
	except Exception:
		log_sync_event(
			file_name,
			settings,
			"Error",
			"ensure_frappe_tag_for_paperless_id",
			title="Tag name could not be loaded from Paperless NGX",
			details=f"Paperless NGX tag ID: {paperless_tag_id}\n\n{frappe.get_traceback()}",
		)
		return None

	if not tag_name:
		return None

	existing_name = frappe.db.get_value("SVAN Paperless Tag Mapping", {"frappe_tag": tag_name})
	relinked = False
	if existing_name:
		existing_id = frappe.db.get_value("SVAN Paperless Tag Mapping", existing_name, "paperless_tag_id")
		if existing_id and existing_id != paperless_tag_id:
			if _paperless_tag_exists(existing_id, settings):
				log_sync_event(
					file_name,
					settings,
					"Warn",
					"ensure_frappe_tag_for_paperless_id",
					title="Automatic mapping skipped - Frappe tag already mapped elsewhere",
					details=(
						f"Frappe tag '{tag_name}' is already mapped to Paperless NGX tag ID {existing_id}. "
						f"Paperless NGX tag ID {paperless_tag_id} is left without a mapping."
					),
				)
				return None
			# The old tag no longer exists in Paperless NGX (deleted) - the
			# existing row is relinked to the new ID below instead of being
			# treated as a conflict (bidirectional is preserved).
			relinked = True

	if not frappe.db.exists("Tag", tag_name):
		frappe.get_doc({"doctype": "Tag", "name": tag_name}).insert(
			ignore_permissions=True, ignore_if_duplicate=True
		)

	mapping_doc = (
		frappe.get_doc("SVAN Paperless Tag Mapping", existing_name)
		if existing_name
		else frappe.new_doc("SVAN Paperless Tag Mapping")
	)
	mapping_doc.paperless_tag_id = paperless_tag_id
	mapping_doc.frappe_tag = tag_name
	mapping_doc.last_synced = frappe.utils.now_datetime()
	try:
		_save_mapping_doc(mapping_doc)
	except frappe.UniqueValidationError:
		if not _retry:
			return ensure_frappe_tag_for_paperless_id(file_name, paperless_tag_id, settings, _retry=True)
		log_sync_event(
			file_name,
			settings,
			"Warn",
			"ensure_frappe_tag_for_paperless_id",
			title="Mapping could not be saved - conflicting row created concurrently",
			details=f"Paperless NGX tag ID: {paperless_tag_id}\nFrappe tag: {tag_name}",
		)
		return None

	log_sync_event(
		file_name,
		settings,
		"Info",
		"ensure_frappe_tag_for_paperless_id",
		title="Mapping relinked to recreated Paperless tag"
		if relinked
		else "Frappe tag created automatically",
		details=f"Paperless NGX tag ID: {paperless_tag_id}\nFrappe tag: {tag_name}",
	)

	# After a relink, the row's PRESERVED "bidirectional" value still applies - the
	# user may have disabled it deliberately earlier, and merely recreating a tag of
	# the same name in Paperless NGX must not silently undo that.
	return tag_name if (not relinked or mapping_doc.bidirectional) else None


def get_paperless_tag_ids_for_document(file_name, attached_to_doctype, attached_to_name, settings):
	"""Resolve the Frappe tags of the business document a file is attached to into
	Paperless NGX tag IDs - called from
	paperless_sync._resolve_upload_metadata() (with the File document's
	attached_to_doctype/-name).

	Deliberately does not read the tags through the convenience method
	Document.get_tags() (which has an off-by-one bug) but directly from "Tag Link".
	For tags without an existing mapping row, ensure_paperless_tag_mapping() checks
	whether a tag of the same name exists in Paperless NGX and creates it there
	automatically if not - a failure is only logged and skipped, as a missing tag
	mapping must never prevent a file upload.

	Returns a (possibly empty) list of Paperless NGX tag IDs.
	"""
	if not attached_to_doctype or not attached_to_name:
		return []

	frappe_tags = frappe.get_all(
		"Tag Link",
		filters={"document_type": attached_to_doctype, "document_name": attached_to_name},
		pluck="tag",
	)
	if not frappe_tags:
		return []

	mapping_rows = frappe.get_all(
		"SVAN Paperless Tag Mapping",
		filters={"frappe_tag": ["in", frappe_tags]},
		fields=["frappe_tag", "paperless_tag_id"],
	)
	paperless_tag_id_by_frappe_tag = {
		row.frappe_tag: row.paperless_tag_id for row in mapping_rows if row.paperless_tag_id
	}

	paperless_tag_ids = []
	for tag in frappe_tags:
		paperless_tag_id = paperless_tag_id_by_frappe_tag.get(tag)
		if not paperless_tag_id:
			paperless_tag_id = ensure_paperless_tag_mapping(file_name, tag, settings)
		if paperless_tag_id:
			paperless_tag_ids.append(paperless_tag_id)

	return paperless_tag_ids
