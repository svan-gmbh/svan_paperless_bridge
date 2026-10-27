// Copyright (c) 2026, SVAN GmbH
// License: gpl-3.0

frappe.ui.form.on("SVAN Paperless Custom Field Mapping", {
	setup(frm) {
		// Metadata sync only considers business documents (enforced server-side too).
		frm.set_query("source_doctype", "field_sources", () => ({
			filters: { name: ["!=", "File"] },
		}));

		// cdt/cdn and "params": see "party_fieldname" in svan_paperless_settings.js.
		frm.set_query("source_fieldname", "field_sources", (doc, cdt, cdn) => ({
			query: "svan_paperless_bridge.svan_paperless_ngx_bridge.doctype.svan_paperless_custom_field_source.svan_paperless_custom_field_source.get_source_field_options",
			params: { doctype: locals[cdt][cdn].source_doctype },
		}));
	},

	before_save(frm) {
		return svan_paperless.confirm_reference_change(frm);
	},

	async refresh(frm) {
		// The reference of the file import is never synced back (enforced on save too).
		const reference = await frappe.db.get_single_value(
			"SVAN Paperless Settings",
			"import_reference_mapping"
		);
		const is_reference = !frm.is_new() && reference === frm.doc.name;
		frm.set_df_property("bidirectional", "read_only", is_reference ? 1 : 0);
	},
});

frappe.ui.form.on("SVAN Paperless Custom Field Source", {
	source_doctype(frm, cdt, cdn) {
		// A source fieldname selected earlier may no longer fit after a DocType
		// change in the same row - better cleared than left as a silent mismatch
		// (analogous to "document_type" in svan_paperless_settings.js).
		frappe.model.set_value(cdt, cdn, "source_fieldname", "");
	},
});
