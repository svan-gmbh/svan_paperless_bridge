"""
Sets the SVAN logo as the icon image for this app's own desktop icon
"Paperless NGX" on the Frappe home screen (nested below the "SVAN" main icon
of svan_base, via "parent_page" in the workspace JSON).

svan_paperless_bridge takes care of this itself instead of svan_base having to
know this app's label - svan_base only provides the logo file
(/assets/svan_base/images/svan-logo-icon.svg).
Called from install.after_install() (best effort - the desktop icon may not
exist yet at that point, see the docstring there).
"""

import frappe
from frappe.desk.doctype.desktop_icon.desktop_icon import create_desktop_icons

DESKTOP_ICON_LABEL = "Paperless NGX"
SVAN_LOGO_URL = "/assets/svan_base/images/svan-logo-icon.svg"


def set_desktop_icon_logo():
	"""Set logo_url on the "Paperless NGX" desktop icon, if it exists.

	create_desktop_icons() is called first to make sure the desktop icon
	document (from this app's workspace) already exists - each step in there is
	guarded by an existence check, so calling it again is harmless. If the
	workspace itself does not exist yet (e.g. during "bench install-app", see
	AGENTS.md), nothing happens at all - a later manual call makes up for it.
	"""
	create_desktop_icons()

	if frappe.db.exists("Desktop Icon", DESKTOP_ICON_LABEL):
		frappe.db.set_value("Desktop Icon", DESKTOP_ICON_LABEL, "logo_url", SVAN_LOGO_URL)
