# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import unittest
from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.boot import get_app_data
from frappe.desk.doctype.desktop_icon.desktop_icon import create_desktop_icons
from frappe.desk.doctype.desktop_settings.desktop_settings import is_desktop_icons_page
from frappe.desk.doctype.dock.dock import get_app_entry_set, mounted_apps
from frappe.tests import IntegrationTestCase
from frappe.utils.install import delete_desktop_icons_for_app

from svan_paperless_bridge import navigation
from svan_paperless_bridge.navigation import (
	APP,
	DESKTOP_ICON_LABEL,
	LOGO_URL,
	MAIN_ICON_LABEL,
	PARENT_APP,
	PARENT_LOGO_URL,
	has_parent_app,
)

MODULE = "SVAN Paperless NGX Bridge"
SIDEBAR = "Paperless NGX"


class TestNavigation(IntegrationTestCase):
	"""Pins how Frappe finds this app's place and logo in the Desk, with and without svan_base.
	Frappe has changed that within v16 several times; a failure (or ImportError) after an update
	means the SVAN logo or the app's entry is gone again."""

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def rail_host(self):
		return PARENT_APP if has_parent_app() else APP

	def test_mounted_on_svan_base_rail_only_if_installed(self):
		self.assertEqual(mounted_apps().get(APP), PARENT_APP if has_parent_app() else None)

	def test_own_apps_screen_entry_only_standalone_in_apps_mode(self):
		app = next(app for app in get_app_data() if app["app_name"] == APP)
		self.assertEqual(app["on_apps_screen"], not has_parent_app() and not is_desktop_icons_page())
		self.assertEqual(app["app_logo_url"], LOGO_URL)

	def test_dock_entry_opens_own_sidebar(self):
		items = frappe.get_doc("Dock", APP).items
		self.assertEqual([(item.link_type, item.link_to) for item in items], [("Sidebar", SIDEBAR)])
		self.assertIn(SIDEBAR, [entry.get("link_to") for entry in get_app_entry_set(self.rail_host())])

	def test_entry_hidden_without_module_access(self):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": "svantest-navigation@example.com",
				"first_name": "Navigation",
				"send_welcome_email": 0,
				"roles": [{"role": "System Manager"}],
				"block_modules": [{"module": MODULE}],
			}
		).insert(ignore_permissions=True)
		frappe.set_user(user.name)

		self.assertNotIn(SIDEBAR, [entry.get("link_to") for entry in get_app_entry_set(self.rail_host())])

	@unittest.skipUnless(PARENT_APP in frappe.get_all_apps(), "needs svan_base's code on the bench")
	def test_desktop_icon_follows_svan_main_entry(self):
		"""The whole Desktop Icons lifecycle, with svan_base's install state faked."""
		with self.desktop_icons_mode():
			frappe.db.delete("Desktop Icon", {"name": ["in", [MAIN_ICON_LABEL, DESKTOP_ICON_LABEL]]})
			frappe.db.delete("Desktop Icon", {"app": PARENT_APP})

			with self.svan_base_installed(False):
				navigation.create_desktop_icon()
				self.assertPlacement(None, LOGO_URL)

				# another SVAN app's folder is joined, and dissolved again once it holds only ours
				navigation.ensure_main_icon()
				navigation.create_desktop_icon()
				self.assertPlacement(None, LOGO_URL)

			navigation.before_app_install(PARENT_APP)
			with self.svan_base_installed(True):
				create_desktop_icons()
				navigation.after_app_install(PARENT_APP)
				self.assertPlacement("App", PARENT_LOGO_URL)
				self.assertFalse(navigation.show_own_app_entry())

			delete_desktop_icons_for_app(PARENT_APP)
			with self.svan_base_installed(False):
				navigation.after_app_uninstall(PARENT_APP)
				self.assertPlacement(None, LOGO_URL)

				navigation.before_uninstall()
			self.assertFalse(frappe.db.exists("Desktop Icon", DESKTOP_ICON_LABEL))

	@unittest.skipIf(has_parent_app(), "svan_base provides the main entry")
	def test_folder_with_other_icons_is_joined(self):
		with self.desktop_icons_mode():
			frappe.db.delete("Desktop Icon", {"name": ["in", [MAIN_ICON_LABEL, "svantest-other"]]})
			navigation.ensure_main_icon()
			frappe.get_doc(
				{
					"doctype": "Desktop Icon",
					"label": "svantest-other",
					"icon_type": "Link",
					"parent_icon": MAIN_ICON_LABEL,
				}
			).insert(ignore_links=True)

			navigation.create_desktop_icon()
			self.assertPlacement("Folder", LOGO_URL)

			navigation.before_uninstall()
			self.assertFalse(frappe.db.exists("Desktop Icon", MAIN_ICON_LABEL))

	def assertPlacement(self, main_icon_type, logo_url):
		"""main_icon_type None: no main entry, the icon sits at top level."""
		self.assertEqual(frappe.db.get_value("Desktop Icon", MAIN_ICON_LABEL, "icon_type"), main_icon_type)
		icon = frappe.db.get_value(
			"Desktop Icon", DESKTOP_ICON_LABEL, ["parent_icon", "logo_url"], as_dict=True
		)
		parent = MAIN_ICON_LABEL if main_icon_type else None
		self.assertEqual((icon.parent_icon, icon.logo_url), (parent, logo_url))

	def desktop_icons_mode(self):
		stack = ExitStack()
		for module in ("svan_paperless_bridge.navigation", "frappe.desk.doctype.desktop_icon.desktop_icon"):
			stack.enter_context(patch(f"{module}.is_desktop_icons_page", return_value=True))
		# after_app_uninstall commits, the test must stay rollbackable
		stack.enter_context(patch("frappe.db.commit"))
		return stack

	def svan_base_installed(self, installed):
		apps = [app for app in frappe.get_installed_apps() if app != PARENT_APP]
		return patch("frappe.get_installed_apps", return_value=[*apps, PARENT_APP] if installed else apps)
