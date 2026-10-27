// Copyright (c) 2026, SVAN GmbH
// License: gpl-3.0

// Shared by the Settings and the Custom Field Mapping form: before saving, warn
// about whitelisted DocTypes without a reference source and about the backfill a
// new reference or changed sources start. Closing the dialog cancels the save.
frappe.provide("svan_paperless");

svan_paperless.confirm_reference_change = function (frm) {
	return frappe
		.call({
			method: "svan_paperless_bridge.reference_config.check_reference_change",
			args: { doc: frm.doc },
		})
		.then((r) => {
			const message = reference_change_message(r.message || {});
			if (!message) return;
			return new Promise((resolve) => {
				frappe.confirm(
					message,
					() => resolve(),
					() => {
						frappe.validated = false;
						resolve();
					},
					__("Save Anyway"),
					__("Cancel")
				);
			});
		});
};

function reference_change_message({ gaps, backfill }) {
	const parts = [];
	if (gaps && gaps.length) {
		const doctypes = gaps.map((doctype) => __(doctype)).join(", ");
		parts.push(
			__(
				"The reference for the import has no source field for: {0}. New files of these DocTypes are not transferred to Paperless NGX until it has one.",
				[doctypes]
			)
		);
	}
	if (backfill !== null && backfill !== undefined) {
		parts.push(
			__(
				"The reference values of {0} linked file(s) are transferred to Paperless NGX again with their next metadata update (button 'Update Metadata Now'). Until then they are neither moved nor detached.",
				[backfill]
			)
		);
	}
	return parts.join("<br><br>");
}
