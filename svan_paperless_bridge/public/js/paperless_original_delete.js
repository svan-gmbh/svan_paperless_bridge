// Copyright (c) 2026, SVAN GmbH
// License: gpl-3.0

// Global, because a file is deleted from many places (attachment sidebar, File form, list).
frappe.request.on_error("PaperlessOriginalHasVersionsError", (r) => {
	const file_name = r.svan_paperless_keep_original_of;
	const [message] = JSON.parse(r._server_messages || "[]").map((m) => JSON.parse(m).message);
	if (!file_name) return;

	frappe.confirm(message, () => {
		frappe.call({
			method: "svan_paperless_bridge.paperless_delete.delete_keeping_paperless_original",
			type: "POST",
			args: { file_name },
			freeze: true,
			callback: () => refresh_after_delete(file_name),
		});
	});
});

function refresh_after_delete(file_name) {
	const [view, doctype, name] = frappe.get_route();
	if (view === "Form" && doctype === "File" && name === file_name) {
		frappe.set_route("List", "File");
	} else if (view === "Form" && cur_frm) {
		cur_frm.reload_doc();
	} else if (view === "List" && cur_list) {
		cur_list.refresh();
	}
}
