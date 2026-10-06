// Copyright (c) 2026, SVAN GmbH
// License: gpl-3.0

frappe.listview_settings["SVAN Paperless Custom Field Mapping"] = {
	// A hidden field, which the list view would not load otherwise
	add_fields: ["paperless_custom_field_id"],
	get_indicator(doc) {
		if (doc.paperless_custom_field_id) {
			return [__("Created in Paperless"), "green", "paperless_custom_field_id,!=,0"];
		}
		return [__("Not created yet"), "orange", "paperless_custom_field_id,=,0"];
	},
};
