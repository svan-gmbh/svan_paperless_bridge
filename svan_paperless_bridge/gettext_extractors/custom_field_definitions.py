"""Babel extractor for the custom field definitions in ``custom_fields.py``.

Frappe's built-in ``custom_field`` extractor only reads fixture files
(``fixtures/custom_field.json``). This app defines its custom fields in Python
instead, so the labels and descriptions shown in the UI would never reach
``locale/main.pot`` and could not be translated.

The definitions are read from the syntax tree rather than by importing the
module, because extractors must run on source files alone - without a database
connection or a running server.
"""

import ast

TRANSLATABLE_KEYS = ("label", "description")


def extract(fileobj, *args, **kwargs):
	"""Yield the translatable strings of the custom field definitions.

	:param fileobj: the file-like object the messages should be extracted from
	:rtype: `iterator`
	"""
	from frappe.gettext.extractors.python import extract as extract_python

	source = fileobj.read()
	tree = ast.parse(source)

	for node in ast.walk(tree):
		if not isinstance(node, ast.Dict):
			continue
		for key, value in zip(node.keys, node.values, strict=True):
			if not isinstance(key, ast.Constant) or key.value not in TRANSLATABLE_KEYS:
				continue
			# Implicitly concatenated literals are a single Constant in the tree.
			if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.strip():
				yield value.lineno, "_", value.value, ["Custom field definition"]

	# The file is claimed by this extractor, so run the regular one as well -
	# otherwise plain _() calls in it would be lost.
	fileobj.seek(0)
	yield from extract_python(fileobj, *args, **kwargs)
