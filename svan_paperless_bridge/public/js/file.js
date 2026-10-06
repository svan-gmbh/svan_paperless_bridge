// Copyright (c) 2026, SVAN GmbH
// License: gpl-3.0

// Must match PREVIEW_METHOD_PATH in preview.py.
const SVAN_PAPERLESS_PREVIEW_URL_PREFIX = "/api/method/svan_paperless_bridge.preview.stream";

frappe.ui.form.on("File", {
	refresh(frm) {
		bind_preview_error_handler(frm);
		guard_paperless_access(frm);
	},
});

function is_paperless_preview_file(frm) {
	// The server checks the Paperless NGX permission only for private files whose
	// file_url points at our preview route (see preview.stream()) - the same
	// condition is mirrored here so that public/local files do not trigger an
	// unnecessary extra request.
	return Boolean(
		frm.doc.is_private &&
			frm.doc.file_url &&
			frm.doc.file_url.startsWith(SVAN_PAPERLESS_PREVIEW_URL_PREFIX)
	);
}

function bind_preview_error_handler(frm) {
	const field = frm.get_field("preview_html");
	if (!field || !field.$wrapper) {
		return;
	}
	const wrapper_el = field.$wrapper.get(0);

	// Bind only once per form instance - refresh() runs again on every
	// save/reload, and preview_file() (core) only replaces the content of
	// wrapper_el, the element itself stays.
	if (wrapper_el._svan_preview_error_handler_bound) {
		return;
	}
	wrapper_el._svan_preview_error_handler_bound = true;

	// "error" events of media elements do not bubble; capture mode catches them on the
	// wrapper for every preview type of preview_file().
	wrapper_el.addEventListener(
		"error",
		(event) => {
			if (event.target === wrapper_el) {
				return;
			}

			// Neutral fallback first - if the permission check below takes
			// longer, fails or does not apply at all (e.g. Paperless NGX itself
			// unreachable rather than a missing permission), a hint is shown
			// immediately instead of a broken icon.
			wrapper_el.innerHTML = `<div class="text-muted">${__("Preview not available.")}</div>`;

			if (!is_paperless_preview_file(frm)) {
				return;
			}
			get_paperless_access_check(frm).then((result) => {
				if (result.denied) {
					wrapper_el.innerHTML = `<div class="text-muted">${frappe.utils.escape_html(
						result.message
					)}</div>`;
				}
			});
		},
		true
	);
}

function guard_paperless_access(frm) {
	if (!is_paperless_preview_file(frm)) {
		return;
	}

	// On opening the page: check proactively instead of only reacting to the
	// failed preview element - so the user sees the message even if no preview
	// type exists for this file type at all (preview_file() in core then shows
	// nothing anyway, so the "error" handler above would never fire).
	get_paperless_access_check(frm).then((result) => {
		if (result.denied) {
			frappe.show_alert({ message: result.message, indicator: "orange" });
		}
	});

	bind_download_guard(frm);
}

function bind_download_guard(frm) {
	// The core "Download" button cannot be intercepted (core's handler runs anyway and a
	// failed <a download> is not observable), so a parallel listener only shows the message.
	//
	// setTimeout: core's refresh() may add the button after ours runs.
	setTimeout(() => {
		const $btn = frm.page.wrapper.find("a.btn, button.btn").filter(function () {
			return $(this).text().trim() === __("Download");
		});
		if (!$btn.length || $btn.data("svanDownloadGuardBound")) {
			return;
		}
		$btn.data("svanDownloadGuardBound", true);

		$btn.on("click", () => {
			get_paperless_access_check(frm).then((result) => {
				if (result.denied) {
					frappe.show_alert({ message: result.message, indicator: "red" });
				}
			});
		});
	}, 0);
}

function get_paperless_access_check(frm) {
	// Cache the result per form instance: the proactive check on opening, the
	// preview error handler and the download button all share the same request
	// instead of each triggering its own fetch.
	if (!frm._svan_paperless_access_check) {
		frm._svan_paperless_access_check = fetch(frm.doc.file_url, {
			credentials: "same-origin",
		})
			.then((response) => {
				if (response.ok) {
					return { denied: false };
				}
				return response
					.json()
					.catch(() => ({}))
					.then((body) => parse_paperless_permission_error(body));
			})
			.catch(() => ({ denied: false }));
	}
	return frm._svan_paperless_access_check;
}

function parse_paperless_permission_error(body) {
	// Frappe's error response carries exc_type directly and the frappe.throw() message
	// double JSON-encoded in _server_messages. Not narrowed to Paperless NGX: any
	// PermissionError on this route brings a fitting message.
	if (body.exc_type !== "PermissionError") {
		return { denied: false };
	}

	let message = __("You do not have permission for this document.");
	try {
		const messages = JSON.parse(body._server_messages || "[]").map((m) => JSON.parse(m));
		if (messages.length && messages[0].message) {
			message = messages[0].message;
		}
	} catch (e) {
		// Unexpected response format - the fallback text stays.
	}
	return { denied: true, message };
}
