app_name = "svan_paperless_bridge"
app_title = "SVAN Paperless NGX Bridge"
app_publisher = "SVAN GmbH"
app_description = "SVAN Paperless NGX Bridge"
app_email = "post@svan.gmbh"
app_license = "gpl-3.0"

# Apps
# ------------------

# svan_base is optional: installed, the app joins its "SVAN" entry; without it, the app stands
# on its own. See navigation.py.
# required_apps = []

# Declared here, not set as data: Frappe re-reads hooks on every boot, while records set once by
# a patch are lost whenever Frappe regenerates or replaces its navigation records.
app_logo_url = "/assets/svan_paperless_bridge/images/svan-logo-icon.svg"

# The own entry only shows in Apps mode while svan_base is not installed (has_permission). The workspace JSON
# says "parent_page": "SVAN" and "app": "svan_base" for Frappe < 16.50, where it nests below
# svan_base's entry; JSON allows no comments - do not "correct" the values. Frappe 16.52 ignores
# both and the dock (dock/) mounts on svan_base instead.
add_to_apps_screen = [
	{
		"name": app_name,
		"logo": app_logo_url,
		"title": app_title,
		"route": "/desk/svan-paperless-settings",
		"has_permission": "svan_paperless_bridge.navigation.show_own_app_entry",
	}
]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# Own bundle name: Frappe's asset manifest is keyed by bundle name across all apps, and svan_base
# ships svan_marker.bundle.css.
app_include_css = "svan_paperless_marker.bundle.css"
app_include_js = [
	"/assets/svan_paperless_bridge/js/paperless_original_delete.js",
	"/assets/svan_paperless_bridge/js/reference_change_confirm.js",
	"/assets/svan_paperless_bridge/js/svan_marker_tooltip.js",
]

# include js, css files in header of web template
# web_include_css = "/assets/svan_paperless_bridge/css/svan_paperless_bridge.css"
# web_include_js = "/assets/svan_paperless_bridge/js/svan_paperless_bridge.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "svan_paperless_bridge/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
doctype_js = {
	"File": ["public/js/file.js", "public/js/file_duplicates.js"],
	"User": "public/js/user.js",
}
doctype_list_js = {
	"SVAN Paperless Custom Field Mapping": "public/js/svan_paperless_custom_field_mapping_list.js",
	"SVAN Paperless Sync Log": "public/js/svan_paperless_sync_log_list.js",
}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "svan_paperless_bridge/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "svan_paperless_bridge.utils.jinja_methods",
# 	"filters": "svan_paperless_bridge.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "svan_paperless_bridge.install.before_install"
after_install = "svan_paperless_bridge.install.after_install"
after_migrate = "svan_paperless_bridge.navigation.create_desktop_icon"

# Uninstallation
# ------------

before_uninstall = "svan_paperless_bridge.navigation.before_uninstall"
# after_uninstall = "svan_paperless_bridge.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

before_app_install = "svan_paperless_bridge.navigation.before_app_install"
after_app_install = "svan_paperless_bridge.navigation.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "svan_paperless_bridge.utils.before_app_uninstall"
after_app_uninstall = "svan_paperless_bridge.navigation.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "svan_paperless_bridge.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "svan_paperless_bridge.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"File": {
		"before_insert": "svan_paperless_bridge.file_events.set_default_source",
		"validate": [
			"svan_paperless_bridge.file_events.keep_content_hash",
			"svan_paperless_bridge.file_events.flag_name_change",
		],
		"after_insert": "svan_paperless_bridge.file_events.sync_immediately",
		"on_trash": "svan_paperless_bridge.file_events.delete_from_paperless",
	},
	# on_change, not on_update: on_update does not fire for allow_on_submit changes
	# on submitted documents.
	"*": {
		"on_change": "svan_paperless_bridge.change_watch.handle_watched_doctype_on_change",
		"after_rename": "svan_paperless_bridge.change_watch.handle_watched_doctype_rename",
	},
	"Tag Link": {
		"after_insert": "svan_paperless_bridge.change_watch.handle_tag_link_change",
		"on_trash": "svan_paperless_bridge.change_watch.handle_tag_link_change",
	},
	# Renaming a Tag rewrites "Tag Link.tag" by raw SQL; no Tag Link event fires.
	"Tag": {
		"after_rename": "svan_paperless_bridge.change_watch.handle_tag_rename",
	},
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"cron": {
		"*/5 * * * *": [
			"svan_paperless_bridge.paperless_sync.sync_pending_files",
			"svan_paperless_bridge.paperless_sync.poll_syncing_files",
			"svan_paperless_bridge.paperless_delete.cleanup_deleted_files",
			# Paperless NGX sends no webhook for a deleted document or a restore from the trash.
			"svan_paperless_bridge.paperless_compare.compare_with_paperless",
			"svan_paperless_bridge.paperless_sync.sync_metadata_updates",
			# Paperless NGX sends no webhook for deleted/renamed tags and custom fields or
			# renamed correspondents.
			"svan_paperless_bridge.paperless_sync.reconcile_metadata_changes",
		]
	},
	# Only a safety net: the webhook imports at once, the button on demand.
	"daily": [
		"svan_paperless_bridge.paperless_import.import_from_paperless",
	],
}

# Testing
# -------

# before_tests = "svan_paperless_bridge.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "svan_paperless_bridge.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# The Desk removes a tag by deleting its Tag Link via raw SQL, so
# doc_events["Tag Link"]["on_trash"] never fires.
override_whitelisted_methods = {
	"frappe.desk.doctype.tag.tag.remove_tag": "svan_paperless_bridge.change_watch.remove_tag_override",
}
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "svan_paperless_bridge.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# Log and import queue must never block deleting a File (or a business document):
# Frappe's link check runs only after on_trash has already deleted the Paperless NGX document.
ignore_links_on_delete = ["SVAN Paperless Sync Log", "SVAN Paperless Import"]

# Request Events
# ----------------
# before_request = ["svan_paperless_bridge.utils.before_request"]
# after_request = ["svan_paperless_bridge.utils.after_request"]

# Job Events
# ----------
# before_job = ["svan_paperless_bridge.utils.before_job"]
# after_job = ["svan_paperless_bridge.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"svan_paperless_bridge.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

default_log_clearing_doctypes = {"SVAN Paperless Sync Log": 90}

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
