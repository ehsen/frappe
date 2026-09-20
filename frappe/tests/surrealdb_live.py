"""Shared helpers for tests that need a real SurrealDB server (skipped unless SURREAL_ENDPOINT / SURREAL_USER /
SURREAL_PASS are set and the SDK is installed). Each test class gets a throw-away namespace that is removed at the end."""

import contextlib
import importlib.util
import os
import uuid
from unittest.mock import patch
from urllib.parse import urlparse

import frappe
from frappe.database import get_db
from frappe.database.surrealdb import setup_db

ENDPOINT = os.environ.get("SURREAL_ENDPOINT")
ROOT_USER = os.environ.get("SURREAL_USER")
ROOT_PASS = os.environ.get("SURREAL_PASS")
LIVE = bool(ENDPOINT and ROOT_USER and ROOT_PASS and importlib.util.find_spec("surrealdb"))
SKIP_REASON = "needs a SurrealDB server (SURREAL_ENDPOINT/USER/PASS) and the SDK"


def uid() -> str:
	return uuid.uuid4().hex[:10]


class LiveSurrealDB:
	"""Mixin for `UnitTestCase` subclasses: provisions sites in a private namespace."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		url = urlparse(ENDPOINT)
		cls.host, cls.port = url.hostname, url.port or 8000
		cls.namespace = f"t_live_{uid()}"
		cls.addClassCleanup(cls._remove_namespace)

	@classmethod
	def _remove_namespace(cls):
		with cls._site_conf("_cleanup", "_cleanup", "x"):
			root = setup_db.get_root_connection()
			root.execute(f"REMOVE NAMESPACE IF EXISTS {cls.namespace}")
			root.close()
			frappe.local.flags.root_connection = None

	@classmethod
	def _site_conf(cls, db_name, db_user, db_password):
		"""Patch the conf/flags a `bench new-site` run would have; restores them on exit."""
		stack = contextlib.ExitStack()
		stack.enter_context(
			patch.dict(
				frappe.local.conf,
				{
					"db_type": "surrealdb",
					"db_namespace": cls.namespace,
					"db_host": cls.host,
					"db_port": cls.port,
					"db_name": db_name,
					"db_user": db_user,
					"db_password": db_password,
				},
			)
		)
		stack.enter_context(patch.dict(frappe.flags, {"root_login": ROOT_USER, "root_password": ROOT_PASS}))
		frappe.local.flags.root_connection = None
		return stack

	def new_site(self):
		name = f"_s{uid()}"
		return name, name, f"pw-{uid()}"

	def provision(self, db_name, db_user, password, force=False):
		with self._site_conf(db_name, db_user, password):
			setup_db.setup_database(force=force, verbose=False)

	def connect(self, db_name, db_user, password):
		with self._site_conf(db_name, db_user, password):
			db = get_db(host=self.host, port=self.port, user=db_user, password=password, cur_db_name=db_name)
			db.connect()
		return db

	def database_names(self):
		with self._site_conf("_x", "_x", "x"):
			root = setup_db.get_root_connection()
			try:
				return set((root.execute("INFO FOR NS")[0].get("databases") or {}).keys())
			finally:
				root.close()
				frappe.local.flags.root_connection = None
