# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

import os
import unittest

import frappe
from frappe.tests import IntegrationTestCase

from svan_paperless_bridge.custom_fields import get_custom_fields

APP = "svan_paperless_bridge"
PARENT_APP = "svan_base"
TOOLTIP = os.path.join("public", "js", "svan_marker_tooltip.js")


def marker_rules(app, file_name):
	"""The rules after the :root block, which only sets the app's own logo URL."""
	with open(os.path.join(frappe.get_app_path(app), "public", "css", file_name)) as f:
		css = f.read()
	return css[css.index("}", css.index(":root")) + 1 :].strip()


class TestSvanMarker(IntegrationTestCase):
	@unittest.skipUnless(PARENT_APP in frappe.get_all_apps(), "needs svan_base's code on the bench")
	def test_rules_match_svan_base(self):
		"""Differing selectors or pseudo-elements would mark an element twice."""
		self.assertEqual(
			marker_rules(APP, "svan_paperless_marker.bundle.css"),
			marker_rules(PARENT_APP, "svan_marker.bundle.css"),
		)

	def test_tooltip_matches_svan_base(self):
		"""A differing copy could give an element two tooltips or name another app."""
		parent_copy = PARENT_APP in frappe.get_all_apps() and os.path.join(
			frappe.get_app_path(PARENT_APP), TOOLTIP
		)
		if not parent_copy or not os.path.exists(parent_copy):
			self.skipTest("svan_base ships no tooltip copy on this bench")
		with open(os.path.join(frappe.get_app_path(APP), TOOLTIP)) as own, open(parent_copy) as parent:
			self.assertEqual(own.read(), parent.read())

	def test_custom_fields_name_this_app(self):
		"""The tooltip finds the app through the custom field's module."""
		for doctype, fields in get_custom_fields().items():
			for field in fields:
				module = frappe.db.get_value(
					"Custom Field", {"dt": doctype, "fieldname": field["fieldname"]}, "module"
				)
				self.assertEqual(
					frappe.local.module_app.get(frappe.scrub(module or "")), APP, field["fieldname"]
				)
