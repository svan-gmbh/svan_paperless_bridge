# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

"""Only files of whitelisted DocTypes take part in any sync. The files of a DocType
removed from the whitelist stay as they are, in ERPNext and in Paperless NGX.
"""


def whitelisted_doctypes(settings):
	return {row.document_type for row in settings.whitelisted_doctypes if row.document_type}


def is_whitelisted(doctype, settings):
	return bool(doctype) and doctype in whitelisted_doctypes(settings)


def whitelisted_filter(settings):
	"""File filter on attached_to_doctype; matches nothing for an empty whitelist."""
	return ["in", sorted(whitelisted_doctypes(settings)) or [""]]
