# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

from svan_paperless_bridge.paperless_delete import handle_file_on_trash
from svan_paperless_bridge.paperless_sync import attempt_immediate_sync

DEFAULT_SOURCE = "erpnext"


def set_default_source(doc, method=None):
	"""Set File.svan_source to "erpnext" on insert if the field is still empty.
	A caller that has already set svan_source explicitly before the insert
	(e.g. in future Paperless NGX triggering a file record with "paperless")
	must not be overwritten by this.
	"""
	if not doc.svan_source:
		doc.svan_source = DEFAULT_SOURCE


def sync_immediately(doc, method=None):
	"""Trigger an optimistic first attempt with a shortened timeout right after
	a file was created (see paperless_sync.attempt_immediate_sync()), instead of
	waiting for the next scheduled run (every 5 minutes). If the attempt fails,
	the file stays "pending" and is picked up by the scheduled job as usual -
	creating the file is never prevented by this, and it is never marked as
	"broken".
	"""
	attempt_immediate_sync(doc.name)


def delete_from_paperless(doc, method=None):
	"""Delete the Paperless NGX document linked to doc (if any) BEFORE the File
	record itself is deleted - see paperless_delete.handle_file_on_trash().
	Raises (and thereby prevents the entire deletion in ERPNext) if the deleting
	user is not allowed to do so according to Paperless NGX, or if Paperless NGX
	was unreachable.
	"""
	handle_file_on_trash(doc)
