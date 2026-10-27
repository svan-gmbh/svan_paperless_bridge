// Copyright (c) 2026, SVAN GmbH
// License: gpl-3.0

frappe.listview_settings["SVAN Paperless Sync Log"] = {
	onload(listview) {
		// The actions menu is only shown while rows are selected.
		listview.page.add_actions_menu_item(__("Export as JSON"), () => {
			open_url_post(
				"/api/method/svan_paperless_bridge.svan_paperless_ngx_bridge.doctype.svan_paperless_sync_log.svan_paperless_sync_log.export_sync_logs",
				{ names: listview.get_checked_items(true) }
			);
		});
	},
};
