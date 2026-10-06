"""
Default whitelist of the file types Paperless NGX supports without extra
configuration, used by the initial installation (install.after_install). It was
derived from the source of the built-in parsers RasterisedDocumentParser/
tesseract and TextDocumentParser/text in paperless-ngx@3.1.1,
src/paperless/parsers/, PLUS the seven office formats added on request - DOC,
DOCX, ODT, RTF, ODS, XLS and XLSX (TikaDocumentParser) - as well as e-mails
(EML, MailDocumentParser). The remaining office formats (ppt, pptx, ppsx, odp,
odg) are left out.

All eight added formats AND the remaining, unlisted office formats require a
separately enabled and configured Tika/Gotenberg (PAPERLESS_TIKA_ENABLED,
default: false according to docs.paperless-ngx.com/configuration) - unlike
PDF/image/text types they are therefore NOT file types that work without
additional server configuration. If Tika/Gotenberg is not enabled on the target
instance, the existing self-healing behaviour kicks in on the first affected
upload (see paperless_sync.remove_unsupported_file_type_from_whitelist()):
Paperless NGX rejects it with "File type ... not supported", the file is marked
"broken" and the file type is removed from the whitelist automatically.

The extension per file type deliberately matches Frappe's own normalization
(frappe.core.doctype.file.file.File.set_file_type(): guess the MIME type, then
apply mimetypes.guess_extension() to it) rather than the extension actually
used in the file name - image/jpeg, for example, is always normalized to "JPG"
by Frappe (never "JPEG"), image/tiff always to "TIFF" (never "TIF"). Deviating
from this would mean reconcile_file_classification() in paperless_sync.py never
matches (File.file_type will never be ".JPEG").

MD (Markdown) was added on top: Paperless NGX does not determine the MIME type
on upload from the file extension but by content sniffing (magic.from_buffer(),
see PostDocumentSerializer.validate_document() in src/documents/serialisers.py)
- a Markdown file is ordinary UTF-8 text without a signature of its own and is
therefore recognized by libmagic as "text/plain", so it is processed by the
built-in text parser without extra configuration, exactly like TXT/CSV. For the
MIME type comparison in reconcile_file_classification() "text/markdown" is
stored here nonetheless (Python's mimetypes module maps ".md" to that MIME
type, not to "text/plain") - the condition that actually matters is the
comparison by the extension "MD" anyway.
"""

import frappe

from svan_paperless_bridge.utils.patch_helpers import tracert

# (extension as normalized by Frappe's File.set_file_type(), MIME type)
DEFAULT_WHITELISTED_FILE_TYPES = [
	("PDF", "application/pdf"),
	("JPG", "image/jpeg"),
	("PNG", "image/png"),
	("TIFF", "image/tiff"),
	("GIF", "image/gif"),
	("BMP", "image/bmp"),
	("WEBP", "image/webp"),
	("HEIC", "image/heic"),
	("TXT", "text/plain"),
	("CSV", "text/csv"),
	("MD", "text/markdown"),
	("DOC", "application/msword"),
	("DOCX", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
	("ODT", "application/vnd.oasis.opendocument.text"),
	("RTF", "application/rtf"),
	("ODS", "application/vnd.oasis.opendocument.spreadsheet"),
	("XLS", "application/vnd.ms-excel"),
	("XLSX", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
	("EML", "message/rfc822"),
]


def populate_default_whitelist():
	"""Fill settings.whitelisted_file_types with DEFAULT_WHITELISTED_FILE_TYPES
	unless the respective extension is already listed there - idempotent, so
	that a repeated run (e.g. after a failed first attempt) does not create
	duplicate rows.
	"""
	settings = frappe.get_single("SVAN Paperless Settings")
	already_whitelisted = {row.file_type for row in settings.whitelisted_file_types}

	added = []
	for extension, mime_type in DEFAULT_WHITELISTED_FILE_TYPES:
		file_type_name = frappe.db.exists("SVAN Paperless File Type", extension)
		if file_type_name:
			if not frappe.db.get_value("SVAN Paperless File Type", file_type_name, "mime_type"):
				frappe.db.set_value(
					"SVAN Paperless File Type", file_type_name, "mime_type", mime_type, update_modified=False
				)
		else:
			file_type_doc = frappe.new_doc("SVAN Paperless File Type")
			file_type_doc.file_extension = extension
			file_type_doc.mime_type = mime_type
			# A concurrent run may have created the same extension in the meantime;
			# the row is named after the extension, so the existing one is equivalent.
			file_type_doc.insert(ignore_permissions=True, ignore_if_duplicate=True)
			file_type_name = file_type_doc.name

		if file_type_name in already_whitelisted:
			continue

		settings.append("whitelisted_file_types", {"file_type": file_type_name})
		added.append(extension)

	if not added:
		tracert("File type whitelist was already complete - no change")
		return

	# ignore_validate: validate() would run a live connection test, which install/migrate
	# must not depend on. ignore_mandatory: the mandatory check runs anyway, and the
	# connection fields are still empty on a fresh install.
	settings.sync_config_version = (settings.sync_config_version or 0) + 1
	settings.flags.ignore_validate = True
	settings.flags.ignore_mandatory = True
	settings.save(ignore_permissions=True)
	tracert(f"File type whitelist extended by {len(added)} default type(s): {', '.join(added)}")
