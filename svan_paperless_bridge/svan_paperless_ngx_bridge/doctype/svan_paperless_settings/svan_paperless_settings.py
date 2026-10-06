# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
import requests
from frappe import _
from frappe.model.document import Document

from svan_paperless_bridge.svan_paperless_ngx_bridge.doctype.svan_paperless_whitelisted_doctype.svan_paperless_whitelisted_doctype import (
	set_default_document_type_label,
	update_party_rename_supported,
	validate_party_fieldname,
)
from svan_paperless_bridge.utils.sync_log import log_sync_event


class SVANPaperlessSettings(Document):
	def validate(self):
		"""Test the connection to Paperless NGX before saving.

		Runs in validate() rather than on_update so that the correspondent ID resolved
		along the way becomes part of the same save (no additional self.save()). Does
		not prevent saving - errors are only shown as a message.
		"""
		self.strip_default_correspondent()
		self.validate_api_call_timeout()
		self.validate_whitelisted_doctypes()
		self.test_paperless_connection()
		self.warn_if_api_token_restricted()
		self.bump_sync_config_version_if_whitelists_changed()
		self.warn_if_max_runtime_too_high()

	def on_update(self):
		"""Discard the cached watched-DocType map of change_watch.py.

		Unconditionally (not only on an actual whitelist change): a change to
		party_fieldname alone within an existing whitelist row changes the map as well
		but does NOT trigger a sync_config_version bump (see
		bump_sync_config_version_if_whitelists_changed(), which only reacts to the
		DocType/file type SETS) - rebuilding it on the next access is cheap, and saving
		the settings is a rare operation. Imported lazily, like in
		create_missing_default_correspondent(), to avoid a circular import.
		"""
		from svan_paperless_bridge.change_watch import invalidate_watched_doctype_map_cache

		invalidate_watched_doctype_map_cache()

	def validate_whitelisted_doctypes(self):
		"""Validate party_fieldname of every whitelist row, fill in
		paperless_document_type_name if still empty, update party_rename_supported and
		make sure each DocType appears only ONCE in the whitelist.

		Frappe does not call validate() of a child table row automatically, so it is
		called explicitly per row here. A DocType whitelisted twice would mean two
		potentially contradicting configurations (different party_fieldname/document
		type name) for the same DocType, without it being clear which row applies to an
		upload (get_configured_party_fieldname() returns only the FIRST row found via
		frappe.db.get_value() anyway).
		"""
		seen_document_types = set()
		for row in self.whitelisted_doctypes:
			validate_party_fieldname(row)
			set_default_document_type_label(row)
			update_party_rename_supported(row)

			if row.document_type:
				if row.document_type in seen_document_types:
					frappe.throw(
						_(
							'"{0}" is already listed in the DocType whitelist - each DocType may appear only '
							"once."
						).format(row.document_type)
					)
				seen_document_types.add(row.document_type)

	def strip_default_correspondent(self):
		"""Strip leading/trailing whitespace from default_correspondent.

		Must run before test_paperless_connection(), because " Trolloveski" for example
		would otherwise not be found by the name comparison and would wrongly be
		created anew.
		"""
		if self.default_correspondent:
			self.default_correspondent = self.default_correspondent.strip()

	def validate_api_call_timeout(self):
		"""Prevent saving api_call_timeout <= 0.

		Unlike max_files_per_run (where 0 = "no sync" is valid), a timeout of 0 makes
		no sense for requests.post().
		"""
		if self.api_call_timeout is not None and self.api_call_timeout <= 0:
			frappe.throw(_("'API Timeout (Seconds)' must be greater than 0."))

	def warn_if_max_runtime_too_high(self):
		"""Warn (without preventing the save) when max_runtime_seconds comes too close
		to Frappe's hard 300 second scheduled job timeout.
		"""
		if self.max_runtime_seconds and self.max_runtime_seconds > 280:
			frappe.msgprint(
				_(
					"Warning: Frappe forcibly terminates the scheduled job after 300 seconds. 'Maximum "
					"Runtime (Seconds)' should stay well below that (currently: {0}s)."
				).format(self.max_runtime_seconds)
			)

	def bump_sync_config_version_if_whitelists_changed(self):
		"""Increment sync_config_version when the DocType or file type whitelist
		changed in substance (other fields deliberately trigger no bump). Determines
		which files reconcile_file_classification() re-evaluates on its next run.
		"""
		doc_before_save = self.get_doc_before_save()
		if not doc_before_save:
			return

		old_doctypes = {row.document_type for row in doc_before_save.whitelisted_doctypes}
		new_doctypes = {row.document_type for row in self.whitelisted_doctypes}

		old_file_types = {row.file_type for row in doc_before_save.whitelisted_file_types}
		new_file_types = {row.file_type for row in self.whitelisted_file_types}

		if old_doctypes != new_doctypes or old_file_types != new_file_types:
			self.sync_config_version = (self.sync_config_version or 0) + 1

	def create_missing_default_correspondent(self):
		"""Create the default correspondent in Paperless NGX if it does not exist
		there yet.

		Imported lazily, because correspondent_sync.py in turn imports
		validate_settings() from this module (circular import otherwise).
		"""
		from svan_paperless_bridge.correspondent_sync import create_missing_correspondent

		return create_missing_correspondent(self.default_correspondent, self)

	def update_default_correspondent_id(self, new_id: str):
		"""Set svan_default_correspondent_id and report an actual change, so that a
		silent change of value stays visible to the user.
		"""
		old_id = self.svan_default_correspondent_id
		if old_id and old_id != new_id:
			frappe.msgprint(
				_("Warning: the correspondent ID has changed (was: {0}, now: {1}).").format(old_id, new_id),
				indicator="orange",
				alert=True,
			)
		self.svan_default_correspondent_id = new_id

	def resolve_default_correspondent_id(self):
		"""Resolve the Paperless NGX correspondent ID for default_correspondent by
		name (case insensitive) and create it if needed - the core of
		test_paperless_connection(), extracted here so that
		correspondent_sync.repair_stale_default_correspondent() can reuse it when
		repairing a default correspondent deleted directly in Paperless NGX (see
		there), without duplicating the name matching logic.

		Deliberately sets NOTHING on self - the caller decides whether and how the new
		ID is applied (update_default_correspondent_id() for the interactive save
		including a change notice, db_set() for the silent background repair). Raises
		on a connection, HTTP or JSON error - the caller is responsible for reporting
		and logging.

		Returns (new_id, was_created), or (None, False) if the token, API URL or
		default_correspondent are (still) missing.
		"""
		token = self.get_password("api_token", raise_exception=False)
		if not token or not self.api_url or not self.default_correspondent:
			return None, False

		url = self.api_url.rstrip("/") + "/correspondents/"
		headers = {"Authorization": f"Token {token}"}
		params = {"name": self.default_correspondent}

		response = requests.get(url, headers=headers, params=params, timeout=5.0, verify=True)
		response.raise_for_status()
		data = response.json()

		for entry in data.get("results", []):
			entry_name = (entry.get("name") or "").lower()
			if entry_name == self.default_correspondent.lower() and entry.get("id") is not None:
				return str(entry["id"]), False

		return str(self.create_missing_default_correspondent()), True

	def test_paperless_connection(self):
		"""Look up the default correspondent in Paperless NGX and create it
		automatically if needed.

		If no correspondent exists under default_correspondent (case insensitive), it
		is created through create_missing_default_correspondent() - unlike for the
		party/mapping based correspondents (see correspondent_sync.py) there is
		deliberately NO settings flag for this: through validate_settings() the default
		correspondent is a mandatory prerequisite for any sync, so a missing value
		would leave the settings permanently incomplete.
		"""
		try:
			# On the first save validate() runs before the insert, so the token exists only in the
			# document, not yet in __Auth; get_password() checks the document first.
			if not self.get_password("api_token", raise_exception=False):
				frappe.msgprint(_("Error: no API token configured."))
				return

			if not self.api_url or not self.default_correspondent:
				# Should not happen thanks to reqd=1, checked anyway to be safe
				return

			new_id, created = self.resolve_default_correspondent_id()
			if new_id is None:
				return

			self.update_default_correspondent_id(new_id)

			if created:
				frappe.msgprint(
					_(
						"Correspondent '{0}' was not found in Paperless NGX and has been created "
						"automatically (ID {1})."
					).format(self.default_correspondent, new_id),
					indicator="green",
					alert=True,
				)
			else:
				frappe.msgprint(
					_("Connection successful. Correspondent '{0}' found.").format(self.default_correspondent),
					indicator="green",
					alert=True,
				)

		except Exception as e:
			frappe.log_error(
				title="SVAN Paperless Settings: connection test failed",
				message=frappe.get_traceback(),
			)
			frappe.msgprint(_("Error: {0} - {1}").format(type(e).__name__, str(e)))

	def warn_if_api_token_restricted(self):
		"""Warn (without preventing the save) when the service API token does NOT
		belong to an unrestricted (superuser) account in Paperless NGX.

		Necessary because paperless_delete.py relies in several places on the service
		token being guaranteed full access to EVERY document - first and foremost
		_document_exists_in_paperless(): a 404 response WITH THIS TOKEN counts as
		"document no longer exists" there (see the docstring there on the ambiguity of
		404). If the service token itself were restricted, a 404 could instead mean
		"this token may not see THIS document" - a document that still exists would
		then wrongly be waved through as already deleted and the corresponding deletion
		in ERPNext wrongly allowed, although it should have been blocked.

		is_superuser (rather than a list of individually required permissions such as
		view_document/change_document/delete_document/...) is the more robust check: in
		Paperless NGX, as in any Django system, a superuser bypasses EVERY object and
		model permission - a hand-maintained list of individual rights would have to be
		updated manually for every future Paperless NGX feature this service token is
		meant to serve.

		Nothing is reported for a genuine network/connection error on purpose - real
		availability problems are already covered by test_paperless_connection(), and
		this additional check should never disturb saving on top of that. An HTTP
		response itself (including 403) does count as a meaningful result, see below.
		"""
		token = self.get_password("api_token", raise_exception=False)
		if not token or not self.api_url:
			return

		try:
			response = requests.get(
				self.api_url.rstrip("/") + "/ui_settings/",
				headers={"Authorization": f"Token {token}"},
				timeout=5.0,
				verify=True,
			)
		except Exception:
			return

		if response.status_code == 200:
			# Verified live: /api/ui_settings/ returns the full superuser status
			# only if the token is staff/superuser at all - a restricted token
			# gets HTTP 403 instead (see below), NEVER a 200 with
			# is_superuser: false.
			is_superuser = response.json().get("user", {}).get("is_superuser")
		elif response.status_code == 403:
			# Verified live: exactly the response a restricted, non-staff token
			# gets from this endpoint - proof in itself of "not unrestricted".
			is_superuser = False
		else:
			# Unexpected status (e.g. 401 wrong token, 5xx) - no reliable
			# conclusion possible, do not warn.
			return

		if not is_superuser:
			frappe.msgprint(
				_(
					"Warning: the API token does not belong to an unrestricted (superuser) account in "
					'Paperless NGX. For reliable deletion (see option "Enforce Document Permissions On '
					'Delete") this service token MUST have unrestricted access to ALL documents - '
					"otherwise a document that still exists but is invisible to this token may wrongly "
					"be treated as already deleted."
				),
				indicator="orange",
				alert=True,
			)


def validate_settings(settings: "SVANPaperlessSettings") -> None:
	"""Global validation function for the SVAN Paperless Settings.

	Checks whether all settings required for syncing with Paperless NGX are present
	(including a non-empty DocType AND file type whitelist - without both there is
	nothing to sync). Logs an incomplete configuration as an error in the sync log
	and then raises (frappe.throw) - the caller (currently
	paperless_sync.sync_pending_files()) does not have to do any additional logging
	itself.
	"""
	missing = []

	if not settings.api_url:
		missing.append(_("API URL"))

	if not settings.get_password("api_token", raise_exception=False):
		missing.append(_("API Token"))

	if not settings.default_correspondent:
		missing.append(_("Default Correspondent"))

	if not settings.svan_default_correspondent_id:
		missing.append(_("Correspondent ID"))

	if not settings.whitelisted_doctypes:
		missing.append(_("Allowed DocTypes (whitelist)"))

	if not settings.whitelisted_file_types:
		missing.append(_("Allowed File Types (whitelist)"))

	if missing:
		log_sync_event(
			None,
			settings,
			"Error",
			"validate_settings",
			title="Settings incompletely configured",
			details=_("Missing fields: {0}").format(", ".join(missing)),
		)
		frappe.throw(
			_("SVAN Paperless Settings are incompletely configured. Missing fields: {0}").format(
				", ".join(missing)
			)
		)
