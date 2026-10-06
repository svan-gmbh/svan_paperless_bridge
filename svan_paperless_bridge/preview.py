# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import re
from urllib.parse import quote

import frappe
import requests
from frappe import _

from svan_paperless_bridge.document_access import has_paperless_document_access
from svan_paperless_bridge.utils.sync_log import log_sync_event

PREVIEW_METHOD_PATH = "svan_paperless_bridge.preview.stream"

# Formats that Frappe's own preview (file.js) renders natively - for those the
# response must be the original, not Paperless NGX' archived PDF version.
NATIVELY_PREVIEWABLE_RE = re.compile(
	r"\.(gif|jpg|jpeg|tiff|png|svg|mov|mp4|mkv|webm|mp3|pdf)$", re.IGNORECASE
)


def get_preview_url(file_doc):
	"""Build the internal preview URL that replaces file_url once the physical
	content has been offloaded to Paperless NGX.

	The prefix "/api/method/" makes Frappe treat the URL as a remote file
	automatically (see URL_PREFIXES in file.py). The file name is appended as an
	additional path segment (ignored by Frappe's routing) so that Frappe's purely
	string-based is_image_file()/is_video_file() check (it only looks at the
	extension before "?") recognizes the file as an image/video at all - otherwise
	the embedded preview would stay empty.
	"""
	display_name = quote(file_doc.file_name or file_doc.name)
	return f"/api/method/{PREVIEW_METHOD_PATH}/{display_name}?file_name={quote(file_doc.name)}"


def is_preview_url(file_url):
	"""True, wenn file_url bereits auf diese interne Vorschau-Route zeigt -
	also der physische Original-Inhalt bereits entfernt wurde.
	"""
	return bool(file_url) and file_url.startswith(f"/api/method/{PREVIEW_METHOD_PATH}")


@frappe.whitelist()
def stream(file_name):
	"""Serve the document content from Paperless NGX for the inline preview in the
	Frappe Desk, without the browser talking to Paperless NGX directly.

	A direct Paperless NGX share link in file_url was deliberately NOT chosen:
	Paperless NGX sets "X-Frame-Options: SAMEORIGIN" and sends no
	Access-Control-Allow-Origin header, so it blocks both embedding (iframe) and
	cross-origin fetch(). This route fetches the bytes server-side with the API
	token instead and serves them from Frappe's own origin.

	For natively renderable formats (NATIVELY_PREVIEWABLE_RE) the original is
	requested explicitly (?original=true) instead of the archived PDF conversion -
	otherwise an <img> tag would receive PDF bytes, for example.

	For private files, Paperless NGX' own document permission system is checked as
	well; the content download itself always runs with the service token, never
	with the user's.

	An HTTP 404 on the content download (document deleted in Paperless NGX in the
	meantime) marks the file as "broken" - by project decision the service token is
	unrestricted (superuser), so a 404 here really does mean "no longer exists" and
	not merely "no permission" (unlike in has_paperless_document_access() above,
	which deliberately treats the same status code as "no access").
	"""
	file_doc = frappe.get_doc("File", file_name)
	frappe.has_permission("File", "read", doc=file_doc, throw=True)

	document_id = file_doc.get("svan_paperless_document_id")
	if not document_id:
		frappe.throw(_("This file is not linked to Paperless NGX."))

	settings = frappe.get_cached_doc("SVAN Paperless Settings")

	if file_doc.is_private and not has_paperless_document_access(file_doc, settings):
		frappe.throw(
			_("You do not have permission for this document, or the document no longer exists."),
			frappe.PermissionError,
		)

	token = settings.get_password("api_token", raise_exception=False)
	url = settings.api_url.rstrip("/") + f"/documents/{document_id}/preview/"
	if NATIVELY_PREVIEWABLE_RE.search(file_doc.file_name or ""):
		url += "?original=true"

	try:
		response = requests.get(
			url,
			headers={"Authorization": f"Token {token}"},
			timeout=settings.api_call_timeout or 30,
			verify=True,
		)
	except Exception:
		frappe.log_error(
			title="Paperless NGX preview: request failed",
			message=frappe.get_traceback(),
		)
		frappe.throw(_("The document could not be loaded from Paperless NGX."))

	if response.status_code == 404:
		file_doc.db_set("svan_paperless_sync_status", "broken", update_modified=False)
		log_sync_event(
			file_name,
			settings,
			"Warn",
			"preview.stream",
			title="Response: HTTP 404 - document no longer exists, marked as 'broken'",
			details=response.text,
		)
		frappe.throw(_("This document no longer exists in Paperless NGX."))

	if response.status_code != 200:
		frappe.log_error(
			title=f"Paperless NGX preview: HTTP {response.status_code}",
			message=response.text,
		)
		frappe.throw(_("Paperless NGX responded with status {0}.").format(response.status_code))

	frappe.response["type"] = "download"
	frappe.response["filename"] = file_doc.file_name or file_name
	frappe.response["filecontent"] = response.content
	frappe.response["content_type"] = response.headers.get("Content-Type", "application/pdf")
	frappe.response["display_content_as"] = "inline"
