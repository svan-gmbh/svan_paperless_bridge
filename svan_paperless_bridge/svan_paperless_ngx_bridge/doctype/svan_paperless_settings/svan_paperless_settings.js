// Copyright (c) 2026, SVAN GmbH
// License: gpl-3.0

frappe.ui.form.on("SVAN Paperless Settings", {
	setup(frm) {
		// The callback's doc is the parent (Settings), so the row comes from locals via cdt/cdn.
		// Autocomplete fields only evaluate "query" and "params" - "filters" is ignored.
		frm.set_query("party_fieldname", "whitelisted_doctypes", (doc, cdt, cdn) => ({
			query: "svan_paperless_bridge.svan_paperless_ngx_bridge.doctype.svan_paperless_whitelisted_doctype.svan_paperless_whitelisted_doctype.get_party_field_options",
			params: { doctype: locals[cdt][cdn].document_type },
		}));
	},

	refresh(frm) {
		frm.add_custom_button(
			__("Transfer New Files"),
			() => {
				throw_if_dirty(frm);
				frappe.call({
					method: "svan_paperless_bridge.paperless_sync.trigger_manual_sync",
					callback: () => {
						frappe.show_alert({
							message: __("The transfer has been started in the background."),
							indicator: "green",
						});
					},
				});
			},
			__("Sync")
		);

		frm.add_custom_button(
			__("Finalize Pending Transfers"),
			() => {
				throw_if_dirty(frm);
				frappe.call({
					method: "svan_paperless_bridge.paperless_sync.trigger_manual_poll",
					callback: () => {
						frappe.show_alert({
							message: __("Finalization has been started in the background."),
							indicator: "green",
						});
					},
				});
			},
			__("Sync")
		);

		frm.add_custom_button(
			__("Update Metadata Now"),
			() => {
				throw_if_dirty(frm);
				frappe.call({
					method: "svan_paperless_bridge.paperless_sync.trigger_manual_metadata_sync",
					callback: () => {
						frappe.show_alert({
							message: __("The metadata update has been started in the background."),
							indicator: "green",
						});
					},
				});
			},
			__("Sync")
		);

		frm.add_custom_button(
			__("Reconcile Global Metadata Changes"),
			() => {
				throw_if_dirty(frm);
				frappe.call({
					method: "svan_paperless_bridge.paperless_sync.trigger_manual_metadata_reconcile",
					callback: () => {
						frappe.show_alert({
							message: __("Reconciliation has been started in the background."),
							indicator: "green",
						});
					},
				});
			},
			__("Sync")
		);

		frm.add_custom_button(
			__("Reset Broken"),
			() => {
				throw_if_dirty(frm);
				frappe.call({
					method: "svan_paperless_bridge.paperless_sync.reset_broken_files",
					callback: (r) => {
						const count = r.message || 0;
						frappe.show_alert({
							message: count
								? __("{0} file(s) reset.", [count])
								: __("No files with status 'broken' found."),
							indicator: count ? "green" : "orange",
						});
					},
				});
			},
			__("Maintenance")
		);

		frm.add_custom_button(
			__("Classify Now"),
			() => {
				throw_if_dirty(frm);
				frappe.call({
					method: "svan_paperless_bridge.paperless_sync.trigger_manual_reconcile",
					callback: () => {
						frappe.show_alert({
							message: __("Classification has been started in the background."),
							indicator: "green",
						});
					},
				});
			},
			__("Maintenance")
		);

		frm.add_custom_button(
			__("Finalize Stuck Deletions"),
			() => {
				throw_if_dirty(frm);
				frappe.call({
					method: "svan_paperless_bridge.paperless_delete.trigger_manual_cleanup",
					callback: () => {
						frappe.show_alert({
							message: __("Deletion has been started in the background."),
							indicator: "green",
						});
					},
				});
			},
			__("Maintenance")
		);

		frm.add_custom_button(
			__("Delete Physical Files Retroactively"),
			() => {
				throw_if_dirty(frm);
				frappe.confirm(
					__(
						"This irreversibly deletes the physical files of all successfully synced files whose content is still stored locally. Continue?"
					),
					() => {
						frappe.call({
							method: "svan_paperless_bridge.paperless_sync.trigger_retroactive_delete_physical_files",
							callback: () => {
								frappe.show_alert({
									message: __(
										"Retroactive deletion has been started in the background."
									),
									indicator: "green",
								});
							},
						});
					}
				);
			},
			__("Maintenance")
		);

		setup_generate_secret_button(frm);
	},
});

function setup_generate_secret_button(frm) {
	// Injected into the input wrapper so the button sits next to the field, like Frappe's
	// password toggle; a Button field would only line up with the label.
	const control = frm.get_field("webhook_secret");
	if (!control) return;

	const $input_wrapper = control.$wrapper.find(".control-input-wrapper");
	if ($input_wrapper.find(".svan-generate-secret-btn").length) {
		// Already injected (refresh() runs more than once, e.g. after
		// reload_doc) - do not attach it twice.
		return;
	}

	$input_wrapper.css("display", "flex");
	$input_wrapper.find(".control-input").css({ flex: "1 1 auto", "min-width": "0" });

	$(
		`<button type="button" class="btn btn-default btn-sm svan-generate-secret-btn" style="margin-left: 8px; white-space: nowrap;">${__(
			"Generate Secret"
		)}</button>`
	)
		.appendTo($input_wrapper)
		.on("click", () => {
			throw_if_dirty(frm);

			const run = () => {
				frappe.call({
					method: "svan_paperless_bridge.webhook_sync.generate_webhook_secret",
					freeze: true,
					callback: () => {
						frm.reload_doc();
					},
				});
			};

			if (frm.doc.webhook_secret) {
				frappe.confirm(
					__("A webhook secret is already configured. Generate a new one anyway?"),
					run
				);
			} else {
				run();
			}
		});
}

frappe.ui.form.on("SVAN Paperless Whitelisted DocType", {
	document_type(frm, cdt, cdn) {
		// A party fieldname selected earlier may no longer fit after a DocType
		// change in the same row - better cleared than left as a silent
		// mismatch.
		frappe.model.set_value(cdt, cdn, "party_fieldname", "");

		// A Paperless document type ID already resolved belongs to the PREVIOUS
		// DocType of this row - cleared after a change, as the next file upload
		// would otherwise transfer a wrong ID (see
		// document_type_sync.get_paperless_document_type_id_for_document()).
		frappe.model.set_value(cdt, cdn, "paperless_document_type_id", "");

		const row = locals[cdt][cdn];
		if (!row.document_type) {
			frappe.model.set_value(cdt, cdn, "paperless_document_type_name", "");
			return;
		}

		// Only a suggestion - never overwrite a name the user adjusted.
		if (row.paperless_document_type_name) {
			return;
		}

		frappe.call({
			method: "svan_paperless_bridge.svan_paperless_ngx_bridge.doctype.svan_paperless_whitelisted_doctype.svan_paperless_whitelisted_doctype.get_document_type_label_suggestion",
			args: { document_type: row.document_type },
			callback: (r) => {
				if (r.message) {
					frappe.model.set_value(cdt, cdn, "paperless_document_type_name", r.message);
				}
			},
		});
	},
});

function throw_if_dirty(frm) {
	// The buttons read the saved state of the settings server-side
	// (frappe.get_single) - with unsaved changes they would silently use the old
	// state without the user noticing. Hence a hard block here instead of just a
	// warning.
	if (frm.is_dirty()) {
		frappe.throw(__("Please save your changes."));
	}
}
