// Copyright (c) 2026, SVAN GmbH
// License: gpl-3.0

// "Duplicates" section in the sidebar of the File form, built like the attachments
// section: files with the same content hash, found on the server. Frappe has no
// hook for own sidebar sections, so the markup is added to the existing sidebar -
// if its structure changes, nothing is shown.

// Same file as SVAN_LOGO_URL in desktop_icon.py.
const SVAN_LOGO_URL = "/assets/svan_base/images/svan-logo-icon.svg";
const DUPLICATES_PAGE_LENGTH = 10;

frappe.ui.form.on("File", {
	refresh(frm) {
		render_duplicates(frm);
	},
});

function render_duplicates(frm) {
	remove_duplicates_section(frm);
	if (frm.is_new() || frm.doc.is_folder) {
		return;
	}

	const file_name = frm.doc.name;
	frappe.call({
		method: "svan_paperless_bridge.file_duplicates.get_duplicate_files",
		args: { file_name },
		callback: (r) => {
			// The form may have moved on to another file while the request ran.
			if (file_name !== frm.docname) {
				return;
			}
			show_duplicates_section(frm, r.message || []);
		},
	});
}

function remove_duplicates_section(frm) {
	frm.sidebar?.sidebar?.find(".svan-paperless-duplicates").remove();
}

function show_duplicates_section(frm, duplicates) {
	remove_duplicates_section(frm);
	const anchor = frm.sidebar?.sidebar?.find(".form-tags");
	if (!duplicates.length || !anchor || !anchor.length) {
		return;
	}

	const section = $(`
		<div class="sidebar-section svan-paperless-duplicates">
			<div class="duplicates-actions">
				<span class="form-sidebar-items">
					<span>
						<span class="pill-label ellipsis form-sidebar-label">
							<img src="${SVAN_LOGO_URL}" class="icon icon-sm" alt="" />
							${__("Duplicates")}
						</span>
					</span>
				</span>
			</div>
			<a class="show-all-btn hidden" href="">
				<span class="pill-label ellipsis">${__("Show All")}</span>
			</a>
		</div>
	`).insertAfter(anchor);

	const rows = duplicates.map((duplicate) => build_duplicate_row(duplicate));
	rows.forEach((row, index) => {
		row.toggleClass("hidden", index >= DUPLICATES_PAGE_LENGTH);
		row.insertBefore(section.find(".show-all-btn"));
	});

	if (rows.length > DUPLICATES_PAGE_LENGTH) {
		section
			.find(".show-all-btn")
			.removeClass("hidden")
			.click((event) => {
				event.preventDefault();
				rows.forEach((row) => row.removeClass("hidden"));
				$(event.currentTarget).addClass("hidden");
			});
	}
}

function build_duplicate_row(duplicate) {
	const name = duplicate.file_name || duplicate.name;
	const file_name = frappe.utils.escape_html(name);
	const attached_to = duplicate.attached_to_doctype
		? ` (${duplicate.attached_to_doctype} ${duplicate.attached_to_name})`
		: "";
	const title = frappe.utils.escape_html(name + attached_to);

	const label = `
		<a href="/desk/file/${encodeURIComponent(duplicate.name)}" title="${title}"
			class="ellipsis attachment-file-label ellipsis-width"
		>
			<span>${file_name}</span>
		</a>`;
	const icon = `<a href="/desk/file/${encodeURIComponent(
		duplicate.name
	)}" class="attachment-icon">
			${frappe.utils.icon(duplicate.is_private ? "es-line-lock" : "es-line-unlock", "sm ml-0")}
		</a>`;

	return $(`<div class="attachment-row"></div>`).append(
		frappe.get_data_pill(label, duplicate.name, null, icon)
	);
}
