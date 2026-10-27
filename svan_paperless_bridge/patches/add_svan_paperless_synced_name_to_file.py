"""Add File.svan_paperless_synced_name, -_synced_title and -_synced_label for
matching file names with Paperless NGX. They stay empty on files linked already:
the first match only takes the current values (see name_sync.py), so nothing is
renamed on either side.
"""

from svan_paperless_bridge.custom_fields import create_svan_paperless_custom_fields
from svan_paperless_bridge.utils.patch_helpers import tracert


def execute():
	tracert(f"START {__file__}")

	create_svan_paperless_custom_fields()
	tracert("Custom fields created/updated")

	tracert(f"END {__file__}")
