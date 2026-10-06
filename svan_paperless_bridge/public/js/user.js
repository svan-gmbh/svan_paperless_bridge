// Copyright (c) 2026, SVAN GmbH
// License: gpl-3.0

frappe.ui.form.on("User", {
	refresh(frm) {
		// An API token is not something you make up - the password strength check
		// (the "symbols, numbers, capital letters" hint) is out of place here.
		const field = frm.get_field("svan_paperless_api_token");
		if (field && field.disable_password_checks) {
			field.disable_password_checks();
		}
	},
});
