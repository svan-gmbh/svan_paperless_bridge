# Copyright (c) 2026, SVAN GmbH
# License: gpl-3.0

"""Access to the real Paperless NGX instance the tests run against.

Every name the tests or the app create there carries a per-test prefix, so
cleanup() removes exactly that and never touches the instance's own data.
"""

import time

import frappe
import requests

NAME_PREFIX = "svantest-"
ENTITY_ENDPOINTS = ("tags", "correspondents", "document_types", "custom_fields")
TIMEOUT = 30


class LivePaperless:
	def __init__(self, api_url, token):
		self.api_url = api_url.rstrip("/")
		self.token = token
		self.prefix = f"{NAME_PREFIX}{frappe.generate_hash(length=8)}"

	def name(self, label):
		return f"{self.prefix} {label}"

	def get(self, path, **params):
		response = self._request("GET", path, params=params)
		return response.json() if response.status_code == 200 else None

	def find(self, endpoint, name):
		results = self.get(f"/{endpoint}/", name__iexact=name)["results"]
		return results[0] if results else None

	def create(self, endpoint, name, **fields):
		return self._request("POST", f"/{endpoint}/", json={"name": name, **fields}).json()["id"]

	def rename(self, endpoint, entity_id, new_name):
		self._request("PATCH", f"/{endpoint}/{entity_id}/", json={"name": new_name}).raise_for_status()

	def delete(self, endpoint, entity_id):
		self._request("DELETE", f"/{endpoint}/{entity_id}/")

	def document(self, document_id):
		return self.get(f"/documents/{document_id}/")

	def patch_document(self, document_id, **fields):
		self._request("PATCH", f"/documents/{document_id}/", json=fields).raise_for_status()

	def original(self, document_id):
		return self._request(
			"GET", f"/documents/{document_id}/download/", params={"original": "true"}
		).content

	def delete_document(self, document_id):
		"""Delete for good: Paperless NGX first moves a document to the trash."""
		self.trash_document(document_id)
		self._request("POST", "/trash/", json={"action": "empty", "documents": [document_id]})

	def trash_document(self, document_id):
		self._request("DELETE", f"/documents/{document_id}/").raise_for_status()

	def delete_version(self, document_id, version_id):
		self._request("DELETE", f"/documents/{document_id}/versions/{version_id}/").raise_for_status()

	def set_version_label(self, document_id, version_id, label):
		self._request(
			"PATCH", f"/documents/{document_id}/versions/{version_id}/", json={"version_label": label}
		).raise_for_status()

	def version_label(self, document_id, version_id):
		versions = self.document(document_id)["versions"]
		return next(v["version_label"] for v in versions if str(v["id"]) == str(version_id))

	def inventory(self):
		"""Every document of the instance with its versions and metadata, by ID."""
		fields = "id,title,versions,tags,correspondent,document_type,custom_fields,original_file_name"
		return {document["id"]: document for document in self._all("/documents/", fields=fields)}

	def restore(self, document_id):
		"""Restore a document or a version from the trash."""
		self._request(
			"POST", "/trash/", json={"action": "restore", "documents": [document_id]}
		).raise_for_status()

	def upload(self, content, title):
		task_id = self._request(
			"POST",
			"/documents/post_document/",
			data={"title": title},
			files={"document": (f"{title}.pdf", content)},
		).json()
		return self.wait_for_task(task_id)

	def add_version(self, document_id, file_name, content):
		"""Returns the ID of the new version."""
		task_id = self._request(
			"POST", f"/documents/{document_id}/update_version/", files={"document": (file_name, content)}
		).json()
		return self.wait_for_task(task_id)

	def wait_for_task(self, task_id):
		"""The ID the consume task created (a document or a version)."""
		deadline = time.monotonic() + 60
		while time.monotonic() < deadline:
			tasks = self.get("/tasks/", task_id=task_id)["results"]
			if tasks and tasks[0]["status"].lower() == "success":
				return tasks[0]["related_document_ids"][0]
			time.sleep(0.5)
		raise TimeoutError(f"Paperless NGX did not finish task {task_id}")

	def create_user(self):
		"""A non-superuser with model permissions; document access comes from
		object permissions or ownership. Returns (user ID, token).
		"""
		username = f"{self.prefix}-user"
		password = frappe.generate_hash(length=24)
		user = self._request(
			"POST",
			"/users/",
			json={
				"username": username,
				"password": password,
				"user_permissions": ["view_document", "change_document", "delete_document"],
			},
		).json()
		return user["id"], self._fetch_token(username, password)

	def _fetch_token(self, username, password):
		# Paperless NGX throttles /token/ (HTTP 429 with Retry-After).
		for _attempt in range(10):
			response = self._request(
				"POST", "/token/", data={"username": username, "password": password}, auth=False
			)
			if response.status_code != 429:
				return response.json()["token"]
			time.sleep(int(response.headers.get("Retry-After", 1)) + 1)
		raise TimeoutError("Paperless NGX kept throttling /token/")

	def cleanup(self, prefix=None):
		"""Remove everything named with prefix (default: this test's own). The prefix
		is checked here as well, not only via the API filters, as a safety net for the
		instance's own data.
		"""
		prefix = prefix or self.prefix
		# Also by file name: a test may have emptied the title.
		documents = set(self._named("/documents/", "title", prefix, title__istartswith=prefix)) | set(
			self._named("/documents/", "original_file_name", prefix, original_filename__istartswith=prefix)
		)
		for document_id in documents:
			self._request("DELETE", f"/documents/{document_id}/")
		trashed = list(
			set(self._named("/trash/", "title", prefix))
			| set(self._named("/trash/", "original_file_name", prefix))
		)
		if trashed:
			self._request("POST", "/trash/", json={"action": "empty", "documents": trashed})
		for endpoint in ENTITY_ENDPOINTS:
			for entity_id in self._named(f"/{endpoint}/", "name", prefix, name__istartswith=prefix):
				self.delete(endpoint, entity_id)
		for user_id in self._named("/users/", "username", prefix):
			self.delete("users", user_id)

	def _named(self, path, field, prefix, **params):
		return [
			entry["id"] for entry in self._all(path, **params) if (entry.get(field) or "").startswith(prefix)
		]

	def _all(self, path, **params):
		results = []
		url, params = self.api_url + path, {"page_size": 100, **params}
		while url:
			payload = requests.get(url, headers=self._headers(), params=params, timeout=TIMEOUT).json()
			results += payload.get("results", [])
			url, params = payload.get("next"), None
		return results

	def _request(self, method, path, auth=True, **kwargs):
		headers = self._headers() if auth else {}
		return requests.request(method, self.api_url + path, headers=headers, timeout=TIMEOUT, **kwargs)

	def _headers(self):
		return {"Authorization": f"Token {self.token}"}
