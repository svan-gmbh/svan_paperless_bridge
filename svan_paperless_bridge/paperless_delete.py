# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
import requests
from frappe import _

from svan_paperless_bridge.preview import is_preview_url
from svan_paperless_bridge.utils.document_claim import is_document_id_held_by_other_file
from svan_paperless_bridge.utils.maintenance import restore_physical_file_from_paperless
from svan_paperless_bridge.utils.sync_log import log_sync_event

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


def handle_file_on_trash(file_doc):
	"""doc_events["File"]["on_trash"]: deletes the Paperless NGX document linked to
	file_doc (if any) BEFORE the File record itself is deleted in ERPNext.

	Has no effect (no API call) when file_doc.svan_paperless_document_id is empty
	AND svan_paperless_uuid is empty as well (never synced), or when ANOTHER File
	record references the same svan_paperless_document_id (see
	is_document_id_held_by_other_file() - a document belongs to one File, but files
	synced before that rule can still share one - deleting one of them must not take
	the others' document down with it).

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
	  lose file content, see _document_exists_in_paperless()/_delete_document()).

	If the physical content had already been offloaded to Paperless NGX (file_url
	points at the internal preview route, see is_preview_url()), it is restored
	locally through restore_physical_file_from_paperless() BEFORE the actual
	deletion (fail-closed on failure) - a safeguard against the race with a LATER
	on_trash hook of another app (see Frappe's own "*" hooks as well as potential
	future apps): if such a hook still prevented the File record from being deleted
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
	"""
	document_id = file_doc.get("svan_paperless_document_id")
	uuid = file_doc.get("svan_paperless_uuid")

	if not document_id:
		if uuid:
			frappe.throw(
				_(
					"File '{0}' is currently being synced to Paperless NGX - it can only be deleted once "
					"the sync has finished. Please try again shortly."
				).format(file_doc.file_name)
			)
		return

	if is_document_id_held_by_other_file(document_id, file_doc.name):
		return

	settings = frappe.get_single("SVAN Paperless Settings")

	if is_preview_url(file_doc.file_url):
		_restore_before_delete(file_doc, settings)

	if settings.enforce_document_permissions and not _is_privileged_deletion_context():
		_delete_via_user_token(file_doc, document_id, settings)
	else:
		_delete_via_service_token(file_doc, document_id, settings)

	frappe.db.set_value(
		"File",
		file_doc.name,
		{
			"svan_paperless_document_id": "",
			"svan_paperless_uuid": "",
			"svan_paperless_sync_status": DELETED_STATUS,
		},
		update_modified=False,
	)
	frappe.db.commit()


def _restore_before_delete(file_doc, settings):
	"""Restore the local original content BEFORE the Paperless NGX document is
	deleted - see the docstring of handle_file_on_trash() for the background.
	Reuses restore_physical_file_from_paperless() from utils/maintenance.py (the
	core of the maintenance script of the same name, which does the same thing at
	scale).

	Fail-closed: if the restore fails, the deletion is prevented entirely through
	frappe.throw() - without a local copy the Paperless NGX document must not be
	deleted.
	"""
	try:
		restore_physical_file_from_paperless(file_doc, settings)
	except Exception:
		log_sync_event(
			file_doc.name,
			settings,
			"Error",
			"handle_file_on_trash",
			title="Restoring the local original content failed",
			details=frappe.get_traceback(),
		)
		frappe.throw(
			_(
				"The original local content of file '{0}' could not be restored from Paperless NGX - "
				"deletion is blocked for now."
			).format(file_doc.file_name)
		)


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


def _delete_via_service_token(file_doc, document_id, settings):
	"""Delete the Paperless NGX document with the service token (full access) - the
	path for privileged deletion contexts as well as for the default case
	(enforce_document_permissions disabled).
	"""
	token = settings.get_password("api_token", raise_exception=False)
	_delete_document(file_doc, document_id, token, settings, permission_denied_message=None)


def _delete_via_user_token(file_doc, document_id, settings):
	"""Delete the Paperless NGX document with the PERSONAL token of the deleting
	user, so that Paperless NGX' own permission system applies directly to the real
	delete request - instead of building a separate check request beforehand.

	If no token is present, the deletion is blocked fail-closed: without a token,
	delete permission cannot be established. If the document no longer exists
	according to the service token, the deletion is allowed WITHOUT even attempting
	the user token request - the reason: for a document that genuinely no longer
	exists, Paperless NGX answers GET/DELETE /api/documents/<id>/ with the same HTTP
	404 as for a document that still exists but which the token owner has no access
	to at all (see document_access.has_paperless_document_access(), already verified
	live for GET there) - without the preceding service token check, a user WITHOUT
	ANY permission could wrongly wave through a document that still exists but is
	invisible to them as "already deleted".
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
	if not _document_exists_in_paperless(file_doc, document_id, service_token, settings):
		log_sync_event(
			file_doc.name,
			settings,
			"Info",
			"handle_file_on_trash",
			title="Paperless NGX document already gone - deletion is allowed",
			details=f"Document ID: {document_id}",
		)
		return

	permission_denied_message = _(
		"You are not allowed to delete this document in Paperless NGX - deletion of file '{0}' is blocked."
	).format(file_doc.file_name)
	_delete_document(file_doc, document_id, user_token, settings, permission_denied_message)


def _document_exists_in_paperless(file_doc, document_id, service_token, settings):
	"""Check WITH THE SERVICE TOKEN (full access, see the docstring of
	_delete_via_user_token()) whether the Paperless NGX document still exists.

	Fail-closed on a network error/unexpected response: aborts through
	frappe.throw() instead of treating an unclear result as "does not exist" - a
	network error must never cause a document that is in fact still present to be
	overlooked and the deletion to be wrongly allowed.
	"""
	url = settings.api_url.rstrip("/") + f"/documents/{document_id}/"

	try:
		response = requests.get(
			url,
			headers={"Authorization": f"Token {service_token}"},
			timeout=settings.api_call_timeout or 30,
			verify=True,
		)
	except Exception:
		log_sync_event(
			file_doc.name,
			settings,
			"Error",
			"handle_file_on_trash",
			title="Error during the existence check in Paperless NGX",
			details=frappe.get_traceback(),
		)
		frappe.throw(_("Paperless NGX is unreachable - deletion is blocked for now."))

	if response.status_code == 404:
		return False
	if response.status_code == 200:
		return True

	log_sync_event(
		file_doc.name,
		settings,
		"Error",
		"handle_file_on_trash",
		title=f"Unexpected response during the existence check in Paperless NGX: HTTP {response.status_code}",
		details=response.text,
	)
	frappe.throw(
		_("Paperless NGX responded unexpectedly (HTTP {0}) - deletion is blocked for now.").format(
			response.status_code
		)
	)


def _delete_document(file_doc, document_id, token, settings, permission_denied_message):
	"""Perform the actual DELETE request against Paperless NGX (ad hoc, no
	task/polling - unlike the upload through post_document/).

	permission_denied_message is None for the service token path (where "permission
	denied" makes no conceptual sense, so a failure is always an
	unexpected/transient error) and set for the user token path (see
	_delete_via_user_token()) - it only controls the message shown to the user; the
	fail-closed logic itself is identical for both paths: a failure ALWAYS blocks
	the deletion in ERPNext.
	"""
	url = settings.api_url.rstrip("/") + f"/documents/{document_id}/"

	try:
		response = requests.delete(
			url,
			headers={"Authorization": f"Token {token}"},
			timeout=settings.api_call_timeout or 30,
			verify=True,
		)
	except Exception:
		log_sync_event(
			file_doc.name,
			settings,
			"Error",
			"handle_file_on_trash",
			title="Error while deleting in Paperless NGX",
			details=frappe.get_traceback(),
		)
		frappe.throw(_("Paperless NGX is unreachable - deletion is blocked for now."))

	if response.status_code in (204, 404):
		log_sync_event(
			file_doc.name,
			settings,
			"Info",
			"handle_file_on_trash",
			title=(
				"Paperless NGX document deleted"
				if response.status_code == 204
				else "Paperless NGX document already gone"
			),
			details=f"Document ID: {document_id}",
		)
		return

	log_sync_event(
		file_doc.name,
		settings,
		"Warn" if permission_denied_message else "Error",
		"handle_file_on_trash",
		title=f"Deletion in Paperless NGX failed: HTTP {response.status_code}",
		details=response.text,
	)

	if permission_denied_message:
		frappe.throw(permission_denied_message, frappe.PermissionError)

	frappe.throw(
		_("Deletion in Paperless NGX failed (HTTP {0}) - deletion is blocked for now.").format(
			response.status_code
		)
	)
