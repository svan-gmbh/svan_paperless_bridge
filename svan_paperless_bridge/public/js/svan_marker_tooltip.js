// Tooltip naming the app a svan_ element comes from, on every label the SVAN marker (CSS) marks.
// Every SVAN app ships an identical copy: all copies set the same title attribute, so an element
// gets one tooltip however many SVAN apps are installed. The apps building on svan_base compare
// their copy with svan_base's in tests/test_svan_marker.py.
(() => {
	const LABELS = [
		'.form-tabs .nav-link[data-fieldname^="svan_"]',
		'.form-section[data-fieldname^="svan_"] > .section-head',
		'.frappe-control[data-fieldname^="svan_"] .control-label',
		'.frappe-control[data-fieldname^="svan_"] .label-area',
		'.grid-heading-row .grid-static-col[data-fieldname^="svan_"] .static-area',
		'.list-row-head .list-row-col[data-fieldname^="svan_"] > span',
	].join(",");

	// Set on hover: forms, grids and lists render their labels at different times.
	$(document).on("mouseover", LABELS, function () {
		const text = __("Extension from {0}", [origin_app_title(this)]);
		const title = this.getAttribute("title");
		if (!title?.includes(text)) {
			// Frappe's own title (full grid label, "Click to sort by") stays
			this.setAttribute("title", title ? `${title}\n${text}` : text);
		}
	});

	function origin_app_title(label) {
		const df = get_docfield(label, label.closest("[data-fieldname]").dataset.fieldname);
		// A standard field belongs to its DocType's module, a custom field to the module it is set to
		const module = df?.is_custom_field ? df.module : df && frappe.get_meta(df.parent)?.module;
		const app = module && frappe.boot.module_app[frappe.scrub(module)];
		return frappe.boot.app_data.find((data) => data.app_name === app)?.app_title || "SVAN";
	}

	function get_docfield(label, fieldname) {
		const control = label.closest(".frappe-control")?.fieldobj;
		if (control) return control.df;

		const column_df = $(label).closest(".grid-static-col").data("df");
		if (column_df) return column_df;

		const doctype = label.closest(".list-row-head") ? cur_list?.doctype : cur_frm?.doctype;
		return doctype && frappe.meta.get_docfield(doctype, fieldname);
	}
})();
