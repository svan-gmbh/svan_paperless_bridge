"""Add a database index on File.svan_paperless_document_id: the webhook, the
comparison and the version handling look files up by it, which read the whole
File table per document without one.
"""

from svan_paperless_bridge.custom_fields import create_svan_paperless_custom_fields
from svan_paperless_bridge.utils.patch_helpers import tracert


def execute():
	tracert(f"START {__file__}")

	create_svan_paperless_custom_fields()
	tracert("Custom fields created/updated")

	tracert(f"END {__file__}")
