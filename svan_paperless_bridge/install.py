from svan_paperless_bridge.custom_fields import create_svan_paperless_custom_fields
from svan_paperless_bridge.navigation import create_desktop_icon
from svan_paperless_bridge.utils.default_file_types import populate_default_whitelist
from svan_paperless_bridge.utils.patch_helpers import tracert


def after_install():
	"""Establish the full target state when the app is installed (custom fields
	on File/User, file type whitelist, desktop icon). Every step is
	idempotent.
	"""
	tracert(f"START after_install {__file__}")
	create_svan_paperless_custom_fields()
	populate_default_whitelist()
	create_desktop_icon()
	tracert(f"END after_install {__file__}")
