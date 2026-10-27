"""Set the app's module on its custom fields: the tooltip of the SVAN marker names the app
through it, and Frappe deletes the fields together with the app.
"""

from svan_paperless_bridge.custom_fields import create_svan_paperless_custom_fields
from svan_paperless_bridge.utils.patch_helpers import tracert


def execute():
	tracert(f"START {__file__}")

	create_svan_paperless_custom_fields()
	tracert("Custom fields created/updated")

	tracert(f"END {__file__}")
