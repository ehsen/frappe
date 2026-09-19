from unittest.mock import patch

import frappe
from frappe.database import bootstrap_database, get_command, get_db
from frappe.database.surrealdb.database import SurrealDBDatabase
from frappe.database.surrealdb.errors import SurrealDBNotImplementedError
from frappe.query_builder.surrealdb_builder import SurrealDB
from frappe.query_builder.utils import db_type_is, get_query_builder
from frappe.tests import UnitTestCase


def _conf(db_type):
	return patch.dict(frappe.local.conf, {"db_type": db_type})


class TestSurrealDBDispatch(UnitTestCase):
	"""P1.1: the surrealdb dispatch is explicit, and never falls through into another engine."""

	def test_get_db_returns_surrealdb_database(self):
		with _conf("surrealdb"):
			db = get_db(cur_db_name="scratch")
		self.assertIsInstance(db, SurrealDBDatabase)
		self.assertEqual(db.db_type, "surrealdb")
		self.assertEqual(db.default_port, 8000)

	def test_other_engines_still_route_to_their_own_backend(self):
		with _conf("mariadb"), patch.dict(frappe.local.conf, {"use_mysqlclient": 1}):
			self.assertEqual(type(get_db(cur_db_name="x")).__module__, "frappe.database.mariadb.mysqlclient")
		with _conf("mariadb"), patch.dict(frappe.local.conf, {"use_mysqlclient": 0}):
			self.assertEqual(type(get_db(cur_db_name="x")).__module__, "frappe.database.mariadb.database")
		with _conf("sqlite"):
			self.assertEqual(type(get_db(cur_db_name="x")).__module__, "frappe.database.sqlite.database")

	def test_unimplemented_backend_paths_fail_closed(self):
		with _conf("surrealdb"):
			db = get_db(cur_db_name="scratch")
			for call in (
				lambda: db.get_tables(),
				lambda: db.has_index("tabToDo", "x"),
				lambda: db.add_index("ToDo", ["name"]),
				lambda: db.type_map.get("Data", ("varchar",)),
				lambda: db.type_map["Data"],
				lambda: bootstrap_database(),
				lambda: get_command(),
			):
				with self.assertRaises(SurrealDBNotImplementedError):
					call()

	def test_error_names_the_owning_chunk_and_is_not_implemented_error(self):
		with _conf("surrealdb"), self.assertRaises(NotImplementedError) as cm:
			get_db(cur_db_name="scratch").get_tables()
		self.assertIn("P1.4", str(cm.exception))
		self.assertIn("does not fall back", str(cm.exception))

	def test_unclassified_errors_are_not_mistaken_for_mariadb_errors(self):
		with _conf("surrealdb"):
			db = get_db(cur_db_name="scratch")
		e = Exception("Duplicate entry 'x' for key 'PRIMARY'")
		self.assertFalse(db.is_duplicate_entry(e))
		self.assertFalse(db.is_deadlocked(e))

	def test_query_builder_is_registered_and_fails_closed(self):
		self.assertIs(get_query_builder("surrealdb"), SurrealDB)
		self.assertIs(db_type_is("surrealdb"), db_type_is.SURREALDB)
		with self.assertRaises(SurrealDBNotImplementedError):
			SurrealDB.from_("ToDo").select("name").get_sql()

	def test_query_functions_without_a_surrealdb_mapping_fail_closed(self):
		from frappe.query_builder.functions import GroupConcat

		with _conf("surrealdb"), self.assertRaises(SurrealDBNotImplementedError):
			GroupConcat("name")

	def test_default_port_and_health_probe_do_not_use_another_engines_port(self):
		from frappe.utils import connections

		with patch.object(connections, "get_conf", return_value=frappe._dict(db_type="surrealdb")):
			with patch.object(connections, "is_open", return_value=True) as is_open:
				self.assertEqual(connections.check_database(), {"surrealdb": True})
		is_open.assert_called_once_with("surrealdb", "127.0.0.1", 8000, None)
