"""
Where this app appears in the Desk. svan_base is optional and never knows this
app - every decision is taken here.

- Apps mode (Frappe >= 16.50): the dock (dock/) mounts on svan_base. Frappe
  ignores the mount while svan_base is not installed; add_to_apps_screen then
  gives the app its own slot and rail, like any other app.
- Desktop Icons grid (Desktop Settings mode "Desktop Icons"): all SVAN apps sit
  in one "SVAN" main entry. With svan_base that is its App icon "SVAN";
  without it, the first SVAN app that brings more than one icon creates a
  Folder "SVAN" (same label and logo), and every other one reuses it. A folder
  never holds a single icon: a one-click detour, so it is dissolved and that
  icon sits at top level. This app brings one icon, so alone it stands at top
  level. The own App icon is never shown there.

Every SVAN app runs the same check whenever any app is installed or
uninstalled (after_app_install/after_app_uninstall) and after every migrate:
create the main entry if needed, then put the own icons below it. That covers
Frappe deleting icons: uninstalling an app deletes its App icon together with
every icon below it (svan_base moves them to top level first, older versions did
not). Before svan_base is installed, a Folder "SVAN" is removed, otherwise its
label would keep Frappe from creating svan_base's App icon "SVAN" (svan_base
does the same itself; this covers older versions).
"""

import frappe
from frappe.desk.doctype.desktop_icon.desktop_icon import (
	clear_every_users_desktop_icons,
	create_desktop_icons,
)
from frappe.desk.doctype.desktop_settings.desktop_settings import is_desktop_icons_page

APP = "svan_paperless_bridge"
PARENT_APP = "svan_base"
MAIN_ICON_LABEL = "SVAN"
DESKTOP_ICON_LABEL = "Paperless NGX"
OWN_DESKTOP_ICONS = (DESKTOP_ICON_LABEL,)
LOGO_URL = "/assets/svan_paperless_bridge/images/svan-logo-icon.svg"
PARENT_LOGO_URL = "/assets/svan_base/images/svan-logo-icon.svg"


def has_parent_app():
	return PARENT_APP in frappe.get_installed_apps()


def show_own_app_entry():
	"""has_permission of add_to_apps_screen, also asked for the own App icon of the grid."""
	return not has_parent_app() and not is_desktop_icons_page()


def logo_url():
	return PARENT_LOGO_URL if has_parent_app() else LOGO_URL


def create_desktop_icon():
	"""Create the main entry and the desktop icons if needed, then place this app's one.

	create_desktop_icons() is Frappe's own idempotent seeding (it also runs on
	every app install and only creates missing icons). If the workspace itself
	does not exist yet (e.g. during "bench install-app"), the next migrate or
	app install makes up for it.
	"""
	if not is_desktop_icons_page():
		return
	create_desktop_icons()
	if len(OWN_DESKTOP_ICONS) > 1:
		ensure_main_icon()
	place_desktop_icon()
	dissolve_single_icon_folder()


def ensure_main_icon():
	if frappe.db.exists("Desktop Icon", MAIN_ICON_LABEL):
		return
	frappe.get_doc(
		{
			"doctype": "Desktop Icon",
			"label": MAIN_ICON_LABEL,
			"icon_type": "Folder",
			"logo_url": LOGO_URL,
		}
	).insert(ignore_permissions=True, ignore_links=True)


def place_desktop_icon():
	"""Put the "Paperless NGX" desktop icon below the main entry, with the matching logo."""
	icon = frappe.db.get_value("Desktop Icon", DESKTOP_ICON_LABEL, ["parent_icon", "logo_url"], as_dict=True)
	if not icon:
		return

	target = {"logo_url": logo_url(), "parent_icon": frappe.db.exists("Desktop Icon", MAIN_ICON_LABEL)}
	changes = {field: value for field, value in target.items() if icon.get(field) != value}
	if changes:
		frappe.db.set_value("Desktop Icon", DESKTOP_ICON_LABEL, changes)
		# set_value bypasses Desktop Icon.on_update, which drops the cached grids
		clear_every_users_desktop_icons()


def before_app_install(app_name):
	"""Make way for svan_base's App icon "SVAN"; after_app_install puts everything below it."""
	if app_name != PARENT_APP or not is_main_folder():
		return
	release_main_folder()


def after_app_install(app_name):
	create_desktop_icon()


def after_app_uninstall(app_name):
	create_desktop_icon()
	# after_app_uninstall runs after remove_app's own commit
	frappe.db.commit()  # nosemgrep


def before_uninstall():
	"""Remove the own icon (Frappe only deletes icons labelled with this app's title and their
	children), and the Folder "SVAN" once it holds a single icon or none."""
	frappe.delete_doc_if_exists("Desktop Icon", DESKTOP_ICON_LABEL)
	dissolve_single_icon_folder()


def dissolve_single_icon_folder():
	if is_main_folder() and frappe.db.count("Desktop Icon", {"parent_icon": MAIN_ICON_LABEL}) <= 1:
		release_main_folder()


def is_main_folder():
	return frappe.db.get_value("Desktop Icon", MAIN_ICON_LABEL, "icon_type") == "Folder"


def release_main_folder():
	for child in frappe.get_all("Desktop Icon", filters={"parent_icon": MAIN_ICON_LABEL}, pluck="name"):
		frappe.db.set_value("Desktop Icon", child, "parent_icon", None)
	frappe.delete_doc("Desktop Icon", MAIN_ICON_LABEL, ignore_permissions=True)
