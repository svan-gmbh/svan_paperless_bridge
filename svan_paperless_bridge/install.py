from svan_paperless_bridge.custom_fields import create_svan_paperless_custom_fields
from svan_paperless_bridge.desktop_icon import set_desktop_icon_logo
from svan_paperless_bridge.utils.default_file_types import populate_default_whitelist
from svan_paperless_bridge.utils.patch_helpers import tracert


def after_install():
	"""Establish the full target state when the app is installed (custom fields
	on File/User, file type whitelist, desktop icon logo). Every step is
	idempotent.
	"""
	tracert(f"START after_install {__file__}")
	create_svan_paperless_custom_fields()
	populate_default_whitelist()
	set_desktop_icon_logo()
	tracert(f"END after_install {__file__}")
