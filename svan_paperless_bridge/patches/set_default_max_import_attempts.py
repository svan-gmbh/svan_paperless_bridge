"""Set SVAN Paperless Settings.max_import_attempts to its default: a single DocType
does not receive the default of a field added later, the field would show 0.
"""

import frappe

from svan_paperless_bridge.svan_paperless_ngx_bridge.doctype.svan_paperless_settings.svan_paperless_settings import (
	DEFAULT_MAX_IMPORT_ATTEMPTS,
)
from svan_paperless_bridge.utils.patch_helpers import tracert

SETTINGS = "SVAN Paperless Settings"


def execute():
	tracert(f"START {__file__}")

	if frappe.db.get_single_value(SETTINGS, "max_import_attempts"):
		tracert("max_import_attempts already set - nothing to do")
	else:
		frappe.db.set_single_value(SETTINGS, "max_import_attempts", DEFAULT_MAX_IMPORT_ATTEMPTS)
		tracert(f"max_import_attempts set to {DEFAULT_MAX_IMPORT_ATTEMPTS}")

	tracert(f"END {__file__}")
