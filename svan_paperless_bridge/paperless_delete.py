# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

from contextlib import contextmanager

import frappe
import requests
from frappe import _

from svan_paperless_bridge.archive import archive_file
from svan_paperless_bridge.file_versions import is_version_file, own_version_id
from svan_paperless_bridge.name_sync import lock_files
from svan_paperless_bridge.preview import is_preview_url
from svan_paperless_bridge.utils.maintenance import restore_physical_file_from_paperless
from svan_paperless_bridge.utils.sync_log import log_sync_event, log_sync_event_after_rollback
from svan_paperless_bridge.utils.whitelist import is_whitelisted

# Roles/users for which the Paperless NGX permission check on deletion is
# skipped (see _is_privileged_deletion_context()) - this automatically covers
# background contexts such as scheduled jobs, bench execute and migrations,
# which usually run as "Administrator".
PRIVILEGED_ROLE = "System Manager"

# Terminal sync status (see custom_fields.py) once handle_file_on_trash() has
# successfully deleted the Paperless NGX document (or it was already gone) -
# reconcile_file_classification() explicitly excludes it from being reclassified
# (like "broken").
DELETED_STATUS = "deleted"

# Paperless NGX can delete the original version only together with all versions.
# The client script (public/js/paperless_original_delete.js) asks the user and then
# calls delete_keeping_paperless_original().
KEEP_ORIGINAL_FLAG = "svan_paperless_keep_original_of"
# Set while files are detached (document_relink.py): only the File goes.
KEEP_DOCUMENTS_FLAG = "svan_paperless_keep_documents"

# paperless_import.IMPORT_DOCTYPE; importing it here would be circular.
IMPORT_DOCTYPE = "SVAN Paperless Import"

ACTION_DELETE = "delete"
ACTION_CHECK_ONLY = "check"


class PaperlessOriginalHasVersionsError(frappe.ValidationError):
	pass


@contextmanager
def keeping_paperless_documents():
	"""Delete Files inside this block without touching Paperless NGX."""
	frappe.flags[KEEP_DOCUMENTS_FLAG] = True
	try:
		yield
	finally:
		frappe.flags.pop(KEEP_DOCUMENTS_FLAG, None)


def handle_file_on_trash(file_doc):
	"""doc_events["File"]["on_trash"]: deletes the Paperless NGX document linked to
	file_doc (if any) BEFORE the File record itself is deleted in ERPNext.

	Has no effect (no API call) when file_doc.svan_paperless_document_id is empty
	AND svan_paperless_uuid is empty as well (never synced), or when ANOTHER File
	record references the same document and neither is a version (files synced
	before the 1:1 rule can still share one - deleting one of them must not take the
	others' document down with it).

	Versions (see file_versions.py): a version file deletes only its version. The
	original of a document that other files of ERPNext hold versions of cannot be
	deleted in Paperless NGX alone; after the permission check the user is asked
	(PaperlessOriginalHasVersionsError) and on confirmation only the File is deleted
	and the original marked as archived, as the version import would bring it back.
	The last file of a document deletes the whole document.

	Raises (frappe.throw) and thereby prevents the ENTIRE deletion in ERPNext when:
	- svan_paperless_document_id is (still) empty while svan_paperless_uuid is set
	  (status "syncing", or a save_uuid() that failed in between, see the final
	  except branch there) - the upload task in Paperless NGX is still running or
	  has not been polled yet, so the resulting document ID is still unknown. A
	  targeted deletion is impossible then (the task could still create a document
	  after the ERPNext deletion) - the user is deliberately asked to try again
	  later instead of silently accepting the risk of an orphaned Paperless NGX
	  document.
	- enforce_document_permissions is active, the deleting user is not a privileged
	  context (see _is_privileged_deletion_context()) and has no delete permission
	  on the linked document according to Paperless NGX (see
	  _delete_via_user_token()), or
	- Paperless NGX was unreachable during the check/deletion or responded
	  unexpectedly (fail-closed, WITHOUT an exception for privileged contexts: a
	  network error must never leave a Paperless NGX document orphaned unnoticed or
	  lose file content, see _target_exists_in_paperless()/_delete_document()).

	If the physical content had already been offloaded to Paperless NGX (file_url
	points at the internal preview route, see is_preview_url()), it is restored
	locally through restore_physical_file_from_paperless() BEFORE the actual
	deletion (fail-closed on failure, unless the content is gone already) - a
	safeguard against the race with a LATER on_trash hook of another app (see
	Frappe's own "*" hooks as well as potential future apps): if such a hook still prevented the File record from being deleted
	AFTER the Paperless NGX document had already been deleted, the content would
	otherwise be lost irrecoverably (it would exist nowhere any more). With the
	restore in place that case is harmless: the file has its content back locally,
	its sync status is already DELETED_STATUS (see below) and it is therefore not
	touched by any scheduled job again - the user can simply trigger the deletion
	again, which then runs through as usual (without another Paperless access).

	If the Paperless NGX document is deleted successfully (or was already gone),
	svan_paperless_document_id/-uuid are cleared and svan_paperless_sync_status is
	set to DELETED_STATUS - immediately, via db_set+commit, regardless of whether
	the subsequent deletion of the File record itself is aborted for some other
	reason.

	Nothing of the above happens, and nothing is deleted in Paperless NGX, while files
	are detached (keeping_paperless_documents()) or for a DocType not (or no longer)
	whitelisted: only the File goes. While the archive is on (archive_deleted_files),
	the file is only marked as archived in Paperless NGX (archive.py) after the
	checks for a running upload and a shared document.
	"""
	if frappe.flags.get(KEEP_DOCUMENTS_FLAG):
		return
	settings = frappe.get_single("SVAN Paperless Settings")
	if not is_whitelisted(file_doc.attached_to_doctype, settings):
		return

	document_id = file_doc.get("svan_paperless_document_id")
	uuid = file_doc.get("svan_paperless_uuid")

	# A version upload stores the document ID right away; its version ID comes from the task.
	upload_running = uuid and (
		not document_id
		or (file_doc.svan_paperless_sync_status == "syncing" and not file_doc.svan_paperless_version_id)
	)
	if upload_running:
		frappe.throw(
			_(
				"File '{0}' is currently being synced to Paperless NGX - it can only be deleted once "
				"the sync has finished. Please try again shortly."
			).format(file_doc.file_name)
		)
	if not document_id:
		return

	sharing_files = frappe.get_all(
		"File",
		filters={
			"svan_paperless_document_id": document_id,
			"name": ["!=", file_doc.name],
			"svan_paperless_sync_status": ["!=", DELETED_STATUS],
		},
		fields=["svan_paperless_document_id", "svan_paperless_version_id"],
	)
	keeps_original = bool(sharing_files) and not is_version_file(file_doc)
	if keeps_original and not any(is_version_file(f) for f in sharing_files):
		# Files synced before the 1:1 rule share a document - it stays for the others.
		return

	if settings.archive_deleted_files:
		_archive(file_doc, settings, whole_document=not sharing_files)
		return

	use_user_token = settings.enforce_document_permissions and not _is_privileged_deletion_context()

	if keeps_original:
		# Permission first, then the question - the user may not be allowed to delete at all.
		if use_user_token:
			_delete_via_user_token(
				file_doc, f"/documents/{document_id}/versions/{document_id}/", ACTION_CHECK_ONLY, settings
			)
		if frappe.flags.get(KEEP_ORIGINAL_FLAG) != file_doc.name:
			frappe.local.response[KEEP_ORIGINAL_FLAG] = file_doc.name
			frappe.throw(
				_(
					"Paperless NGX cannot delete the original version of '{0}' on its own while other "
					"versions of it are attached here. Delete only the file in ERPNext? The original stays "
					"in Paperless NGX."
				).format(file_doc.file_name),
				PaperlessOriginalHasVersionsError,
				title=_("Original Has Other Versions"),
			)
		# Unmarked, the version import would bring it back.
		_archive(file_doc, settings, whole_document=False)
		return

	if is_preview_url(file_doc.file_url):
		_restore_before_delete(file_doc, settings)

	# The last file of a document takes it down entirely, versions only in Paperless NGX included.
	path = (
		f"/documents/{document_id}/versions/{file_doc.svan_paperless_version_id}/"
		if sharing_files
		else f"/documents/{document_id}/"
	)
	if use_user_token:
		_delete_via_user_token(file_doc, path, ACTION_DELETE, settings)
	else:
		_delete_via_service_token(file_doc, path, settings)

	frappe.db.set_value(
		"File",
		file_doc.name,
		{
			"svan_paperless_document_id": "",
			"svan_paperless_version_id": "",
			"svan_paperless_uuid": "",
			"svan_paperless_sync_status": DELETED_STATUS,
		},
		update_modified=False,
	)
	frappe.db.commit()


def _archive(file_doc, settings, whole_document):
	"""Mark the file archived instead of deleting it in Paperless NGX. Nothing
	destructive happens, so neither the user's permissions nor the content matter;
	a failure blocks the deletion, as the document would otherwise be imported again.
	"""
	# The webhook fired by the mark waits, then finds the files gone (or the mark taken back).
	lock_files(str(file_doc.svan_paperless_document_id))
	try:
		archived = archive_file(file_doc, settings, whole_document)
	except Exception:
		_block_deletion(
			file_doc,
			settings,
			"Error",
			title="Marking the file as archived in Paperless NGX failed",
			details=f"Paperless NGX document ID: {file_doc.svan_paperless_document_id}\n\n{frappe.get_traceback()}",
			message=_(
				"File '{0}' could not be marked as archived in Paperless NGX - deletion is blocked for now."
			).format(file_doc.file_name),
		)
	if whole_document:
		# An imported document whose mark is removed again must be imported anew.
		frappe.db.delete(IMPORT_DOCTYPE, {"name": str(file_doc.svan_paperless_document_id)})
	log_sync_event(
		file_doc.name,
		settings,
		"Info",
		"handle_file_on_trash",
		title=(
			("Document archived in Paperless NGX" if whole_document else "Version archived in Paperless NGX")
			if archived
			else "Paperless NGX document already gone - deletion is allowed"
		),
		details=(
			f"Paperless NGX document ID: {file_doc.svan_paperless_document_id}\n"
			f"Version ID: {own_version_id(file_doc)}"
		),
	)


def _restore_before_delete(file_doc, settings):
	"""Restore the local original content BEFORE the Paperless NGX document is
	deleted - see the docstring of handle_file_on_trash() for the background.
	Reuses restore_physical_file_from_paperless() from utils/maintenance.py (the
	core of the maintenance script of the same name, which does the same thing at
	scale).

	Fail-closed: if the restore fails, the deletion is prevented entirely through
	frappe.throw() - without a local copy the Paperless NGX document must not be
	deleted. Except for HTTP 404: the service token is a superuser's, so the content
	is gone already and there is nothing left to protect.
	"""
	try:
		restore_physical_file_from_paperless(file_doc, settings)
	except Exception as error:
		response = getattr(error, "response", None)
		if response is None or response.status_code != 404:
			_block_deletion(
				file_doc,
				settings,
				"Error",
				title="Restoring the local original content failed",
				details=frappe.get_traceback(),
				message=_(
					"The original local content of file '{0}' could not be restored from Paperless NGX - "
					"deletion is blocked for now."
				).format(file_doc.file_name),
			)
		log_sync_event(
			file_doc.name,
			settings,
			"Info",
			"handle_file_on_trash",
			title="Content already gone in Paperless NGX - deletion is allowed",
			details=f"Paperless NGX document ID: {file_doc.svan_paperless_document_id}",
		)


def _block_deletion(file_doc, settings, log_type, title, details, message, exc=frappe.ValidationError):
	"""Log why the deletion is blocked, then block it. The throw rolls the log entry
	back together with the deletion, so it is written once more after the rollback.
	"""
	log_sync_event(file_doc.name, settings, log_type, "handle_file_on_trash", title=title, details=details)
	log_sync_event_after_rollback(
		file_doc.name, settings, log_type, "handle_file_on_trash", title=title, details=details
	)
	frappe.throw(message, exc)


def cleanup_deleted_files():
	"""Scheduled task: retries the ERPNext-side deletion for all files with sync
	status DELETED_STATUS.

	Catches exactly the case the restore in _restore_before_delete() guards against
	(see the docstring of handle_file_on_trash()): an earlier deletion attempt was
	prevented by another on_trash hook (or for some other reason) AFTER the
	Paperless deletion had already succeeded, leaving the file behind with restored
	local content and status DELETED_STATUS.

	handle_file_on_trash() itself runs again through frappe.delete_doc(), but for
	such a file it is a pure no-op (svan_paperless_document_id/-uuid are already
	empty, see the early return there) - the deletion then runs through as usual,
	WITHOUT another Paperless NGX access.

	A repeated failure (e.g. the same blocking hook of another app as on the first
	attempt) is logged and does not abort the run for the remaining files - the
	file stays at DELETED_STATUS and is retried on the next scheduled run.
	"""
	settings = frappe.get_single("SVAN Paperless Settings")

	file_names = frappe.get_all(
		"File",
		filters={"svan_paperless_sync_status": DELETED_STATUS},
		pluck="name",
	)

	for file_name in file_names:
		try:
			frappe.delete_doc("File", file_name, ignore_permissions=True)
		except Exception:
			log_sync_event(
				file_name,
				settings,
				"Warn",
				"cleanup_deleted_files (CronJob)",
				title="Final deletion failed - will be retried on the next run",
				details=frappe.get_traceback(),
			)
			frappe.db.rollback()
			continue

		frappe.db.commit()


@frappe.whitelist()
def trigger_manual_cleanup():
	"""Manual ad-hoc trigger for the "Finalize Stuck Deletions" button in SVAN
	Paperless Settings.

	Runs as a background job, analogous to the trigger_manual_* functions in
	paperless_sync.py: every stuck file means a regular frappe.delete_doc() call,
	which should not block the request.
	"""
	frappe.only_for("System Manager")
	frappe.enqueue(
		"svan_paperless_bridge.paperless_delete.cleanup_deleted_files",
		queue="long",
		job_name="svan_paperless_manual_cleanup",
	)


@frappe.whitelist(methods=["POST", "DELETE"])
def delete_keeping_paperless_original(file_name):
	"""Delete the file after the user agreed to keep its original version in
	Paperless NGX (see PaperlessOriginalHasVersionsError). Permissions are checked by
	frappe.delete_doc() and handle_file_on_trash() as for any deletion.
	"""
	frappe.flags[KEEP_ORIGINAL_FLAG] = file_name
	try:
		frappe.delete_doc("File", file_name)
	finally:
		frappe.flags.pop(KEEP_ORIGINAL_FLAG, None)


def _is_privileged_deletion_context():
	"""True if frappe.session.user is exempt from the Paperless NGX permission
	check on deletion (Administrator or PRIVILEGED_ROLE) - see the docstring of
	handle_file_on_trash().

	This covers background contexts that usually run as "Administrator" (scheduled
	jobs, bench execute, migrations, cascading deletion through a parent document)
	without special handling per caller.
	"""
	user = frappe.session.user
	return user == "Administrator" or PRIVILEGED_ROLE in frappe.get_roles(user)


def _delete_via_service_token(file_doc, path, settings):
	"""Delete with the service token (full access) - the path for privileged
	deletion contexts as well as for the default case (enforce_document_permissions
	disabled).
	"""
	token = settings.get_password("api_token", raise_exception=False)
	_delete_document(file_doc, path, token, settings, ACTION_DELETE, permission_denied_message=None)


def _delete_via_user_token(file_doc, path, action, settings):
	"""Send the request with the PERSONAL token of the deleting user, so that
	Paperless NGX' own permission system applies directly to the real delete
	request - instead of building a separate check request beforehand.

	If no token is present, the deletion is blocked fail-closed: without a token,
	delete permission cannot be established. If the document (or the version) no
	longer exists according to the service token, the deletion is allowed WITHOUT
	even attempting the user token request - the reason: for a document that
	genuinely no longer exists, Paperless NGX answers with the same HTTP 404 as for a
	document that still exists but which the token owner has no access to at all
	(see document_access.has_paperless_document_access()) - without the preceding
	service token check, a user WITHOUT ANY permission could wrongly wave through a
	document that still exists but is invisible to them as "already deleted".
	"""
	user_token = frappe.get_doc("User", frappe.session.user).get_password(
		"svan_paperless_api_token", raise_exception=False
	)
	if not user_token:
		frappe.throw(
			_(
				"No personal Paperless NGX API token configured - deletion of file '{0}' cannot be "
				"verified and is therefore blocked."
			).format(file_doc.file_name)
		)

	service_token = settings.get_password("api_token", raise_exception=False)
	if not _target_exists_in_paperless(file_doc, path, service_token, settings):
		log_sync_event(
			file_doc.name,
			settings,
			"Info",
			"handle_file_on_trash",
			title="Paperless NGX document already gone - deletion is allowed",
			details=f"Target: {path}",
		)
		return

	permission_denied_message = _(
		"You are not allowed to delete this document in Paperless NGX - deletion of file '{0}' is blocked."
	).format(file_doc.file_name)
	_delete_document(file_doc, path, user_token, settings, action, permission_denied_message)


def _target_exists_in_paperless(file_doc, path, service_token, settings):
	"""Check WITH THE SERVICE TOKEN (full access, see the docstring of
	_delete_via_user_token()) whether the document of path - and for a version path
	that version - still exists.

	Fail-closed on a network error/unexpected response: aborts through
	frappe.throw() instead of treating an unclear result as "does not exist" - a
	network error must never cause a document that is in fact still present to be
	overlooked and the deletion to be wrongly allowed.
	"""
	document_id, _sep, version_id = path.strip("/").removeprefix("documents/").partition("/versions/")
	url = settings.api_url.rstrip("/") + f"/documents/{document_id}/"

	try:
		response = requests.get(
			url,
			headers={"Authorization": f"Token {service_token}"},
			timeout=settings.api_call_timeout or 30,
			verify=True,
		)
	except Exception:
		_block_deletion(
			file_doc,
			settings,
			"Error",
			title="Error during the existence check in Paperless NGX",
			details=frappe.get_traceback(),
			message=_("Paperless NGX is unreachable - deletion is blocked for now."),
		)

	if response.status_code == 404:
		return False
	if response.status_code == 200:
		return not version_id or any(
			str(v.get("id")) == version_id for v in response.json().get("versions") or []
		)

	_block_deletion(
		file_doc,
		settings,
		"Error",
		title=f"Unexpected response during the existence check in Paperless NGX: HTTP {response.status_code}",
		details=response.text,
		message=_("Paperless NGX responded unexpectedly (HTTP {0}) - deletion is blocked for now.").format(
			response.status_code
		),
	)


def _delete_document(file_doc, path, token, settings, action, permission_denied_message):
	"""Send the DELETE request for path to Paperless NGX (ad hoc, no task/polling -
	unlike the upload through post_document/).

	ACTION_CHECK_ONLY targets the original version, which Paperless NGX refuses to
	delete (HTTP 400) - but only after its permission check: 400 therefore means
	"allowed", nothing is deleted.

	permission_denied_message is None for the service token path (where "permission
	denied" makes no conceptual sense, so a failure is always an
	unexpected/transient error) and set for the user token path (see
	_delete_via_user_token()) - it only controls the message shown to the user; the
	fail-closed logic itself is identical for both paths: a failure ALWAYS blocks
	the deletion in ERPNext.
	"""
	url = settings.api_url.rstrip("/") + path

	try:
		response = requests.delete(
			url,
			headers={"Authorization": f"Token {token}"},
			timeout=settings.api_call_timeout or 30,
			verify=True,
		)
	except Exception:
		_block_deletion(
			file_doc,
			settings,
			"Error",
			title="Error while deleting in Paperless NGX",
			details=frappe.get_traceback(),
			message=_("Paperless NGX is unreachable - deletion is blocked for now."),
		)

	if action == ACTION_CHECK_ONLY and response.status_code == 400:
		log_sync_event(
			file_doc.name,
			settings,
			"Info",
			"handle_file_on_trash",
			title="Original kept in Paperless NGX - only the file is deleted",
			details=f"Target: {path}",
		)
		return

	if action == ACTION_DELETE and response.status_code in (200, 204, 404):
		log_sync_event(
			file_doc.name,
			settings,
			"Info",
			"handle_file_on_trash",
			title=(
				"Paperless NGX document already gone"
				if response.status_code == 404
				else "Paperless NGX document deleted"
			),
			details=f"Target: {path}",
		)
		return

	if permission_denied_message:
		message, exc = permission_denied_message, frappe.PermissionError
	else:
		message = _("Deletion in Paperless NGX failed (HTTP {0}) - deletion is blocked for now.").format(
			response.status_code
		)
		exc = frappe.ValidationError
	_block_deletion(
		file_doc,
		settings,
		"Warn" if permission_denied_message else "Error",
		title=f"Deletion in Paperless NGX failed: HTTP {response.status_code}",
		details=f"Target: {path}\n\n{response.text}",
		message=message,
		exc=exc,
	)
