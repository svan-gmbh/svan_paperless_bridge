# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import frappe
import requests
from frappe.utils.password import remove_encrypted_password, set_encrypted_password


@frappe.whitelist()
def set_my_paperless_api_token(token=None):
	"""Set (token given) or clear (token empty/None) the personal Paperless NGX
	API token of the CURRENTLY logged-in user.

	No longer strictly required for normal use: the field
	svan_paperless_api_token deliberately carries NO permlevel (see
	custom_fields.py) and can therefore be entered directly in the User form and
	saved with the regular save button - Frappe lets every user read and save
	their OWN User document (verified live: frappe.has_permission("User",
	"write", doc=<own user>) returns True), and a regular doc.save() encrypts a
	password field automatically via BaseDocument._save_passwords() anyway.

	Kept as a direct route of its own nonetheless (e.g. for automation/scripts
	without a form) - the self-service counterpart to Frappe's own
	switch_theme() (frappe/core/doctype/user/user.py): it ALWAYS acts on
	frappe.session.user only and never takes a target user.

	Deliberately uses set_encrypted_password()/remove_encrypted_password()
	(frappe/utils/password.py, the same module that
	reset_settings_to_fresh_install() in utils/maintenance.py already uses for
	the service API token) instead of frappe.db.set_value(): a password field is
	only encrypted into the __Auth table automatically during a regular
	Document.save() via BaseDocument._save_passwords() - a raw
	frappe.db.set_value() would instead write the token in PLAIN TEXT into the
	tabUser column (actively verified: the same generic mechanism applies to
	every password custom field, regardless of the DocType).
	"""
	if token:
		set_encrypted_password("User", frappe.session.user, token, "svan_paperless_api_token")
	else:
		remove_encrypted_password("User", frappe.session.user, "svan_paperless_api_token")


def has_paperless_document_access(file_doc, settings):
	"""Check whether the CURRENTLY logged-in user (frappe.session.user) has
	access to the Paperless NGX document linked to file_doc
	(file_doc.svan_paperless_document_id) according to Paperless NGX's own
	document permission system (owner + view users/groups per document).

	Only relevant for PRIVATE files (File.is_private) - the caller
	(preview.stream()) checks that BEFORE this call, so that public files
	(is_private == 0) need no personal token at all and behave exactly as before.

	Sequence:
	1. If SVAN Paperless Settings.enforce_document_permissions is disabled
	   (enabled by default), the check is skipped - True.
	2. If the logged-in user has no Paperless NGX API token of their own, access
	   is denied - False.
	3. If a token exists, the document detail endpoint is queried EXPLICITLY WITH
	   THAT TOKEN (not the service token from the settings, which has full access
	   to ALL documents): Paperless NGX answers GET /api/documents/<id>/ with HTTP
	   200 only if the token owner has access to EXACTLY this document according
	   to its own object permission system - any other response (including 404,
	   even for a document that genuinely does not exist - Paperless NGX
	   deliberately does not distinguish these two cases; verified live against
	   the test instance) counts as "no access". The JSON body is deliberately
	   not evaluated - the status code alone answers the question completely.

	Fail-CLOSED on a request error/timeout during this check (exception ->
	False): this is a SECURITY function - when in doubt, access is denied rather
	than granted. It costs no additional availability compared to the status quo
	either: if Paperless NGX is unreachable, the actual content fetch in
	preview.stream() fails immediately afterwards anyway (the try/except around
	requests.get with the service token there) - the user would get no preview in
	either case, only with a more precise error message here.
	"""
	if not settings.enforce_document_permissions:
		return True

	user = frappe.session.user
	user_token = frappe.get_doc("User", user).get_password("svan_paperless_api_token", raise_exception=False)

	if not user_token:
		return False

	document_id = file_doc.get("svan_paperless_document_id")
	url = settings.api_url.rstrip("/") + f"/documents/{document_id}/"

	try:
		response = requests.get(
			url,
			headers={"Authorization": f"Token {user_token}"},
			timeout=settings.api_call_timeout or 30,
			verify=True,
		)
	except Exception:
		frappe.log_error(
			title="Paperless NGX permission check: request failed",
			message=frappe.get_traceback(),
		)
		return False

	return response.status_code == 200
