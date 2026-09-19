"""Live tests against a real SurrealDB server (skipped unless SURREAL_ENDPOINT/SURREAL_USER/SURREAL_PASS are set
and the SDK is installed). Each run uses its own throw-away namespace, which is removed at the end."""

import unittest
from unittest.mock import patch

import frappe
from frappe.database.surrealdb import errors as E
from frappe.database.surrealdb import setup_db
from frappe.tests import UnitTestCase
from frappe.tests.surrealdb_live import LIVE, ROOT_PASS, ROOT_USER, SKIP_REASON, LiveSurrealDB
from frappe.tests.surrealdb_live import uid as _uid


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestSurrealDBLive(LiveSurrealDB, UnitTestCase):
	# --- P1.2 provisioning ------------------------------------------------------------------------------------
	def test_provision_use_and_drop(self):
		db_name, db_user, password = self.new_site()
		self.provision(db_name, db_user, password)
		self.assertIn(db_name, self.database_names())

		db = self.connect(db_name, db_user, password)
		db.sql("DEFINE TABLE tabProbe SCHEMAFULL")
		db.sql("DEFINE FIELD name ON tabProbe TYPE string")
		db.sql("CREATE tabProbe:a SET name = $n", {"n": "a"})
		self.assertEqual(db.sql("SELECT name FROM tabProbe"), (("a",),))
		db.commit()
		db.close()

		with self._site_conf(db_name, db_user, password):
			setup_db.drop_user_and_database(db_name, db_user)
		self.assertNotIn(db_name, self.database_names())
		with self.assertRaises(E.SurrealDBAuthError):  # the user is gone with the database
			self.connect(db_name, db_user, password)
		with self._site_conf("_x", "_x", "x"):  # the namespace itself survives a site drop
			root = setup_db.get_root_connection()
			self.assertIn(self.namespace, root.execute("INFO FOR ROOT")[0]["namespaces"])
			root.close()
			frappe.local.flags.root_connection = None

	def test_existing_database_needs_force_and_force_recreates_it(self):
		db_name, db_user, password = self.new_site()
		self.provision(db_name, db_user, password)
		db = self.connect(db_name, db_user, password)
		db.sql("DEFINE TABLE tabKeep SCHEMALESS")
		db.sql("CREATE tabKeep:1 SET v = 1")
		db.commit()
		db.close()

		with self.assertRaises(SystemExit) as cm:  # MariaDB parity: message + exit(1), nothing dropped
			self.provision(db_name, db_user, password, force=False)
		self.assertEqual(cm.exception.code, 1)
		db = self.connect(db_name, db_user, password)
		self.assertEqual(db.sql("SELECT v FROM tabKeep"), ((1,),))
		db.close()

		new_password = "pw-" + _uid()
		self.provision(db_name, db_user, new_password, force=True)
		with self.assertRaises(E.SurrealDBAuthError):
			self.connect(db_name, db_user, password)  # old password no longer works
		db = self.connect(db_name, db_user, new_password)
		with self.assertRaises(E.SurrealDBProgrammingError) as caught:  # data is gone
			db.sql("SELECT v FROM tabKeep")
		self.assertTrue(db.is_table_missing(caught.exception))
		db.close()

	def test_site_users_are_isolated_from_each_other_and_from_the_server(self):
		a, user_a, pw_a = self.new_site()
		b, user_b, pw_b = self.new_site()
		self.provision(a, user_a, pw_a)
		self.provision(b, user_b, pw_b)
		db_b = self.connect(b, user_b, pw_b)
		db_b.sql("DEFINE TABLE tabSecret SCHEMALESS")
		db_b.sql("CREATE tabSecret:1 SET v = 'hidden'")
		db_b.commit()

		db_a = self.connect(a, user_a, pw_a)
		db_a._conn.select_db(b)  # the SDK allows use(); permissions must still hide everything
		self.assertEqual(db_a.sql("SELECT v FROM tabSecret"), ())
		db_a.sql("CREATE tabSecret:2 SET v = 'intruder'")
		db_a.commit()
		self.assertEqual(
			db_b.sql("SELECT v FROM tabSecret"), (("hidden",),), "the other site's data is untouched"
		)
		for statement in (f"REMOVE DATABASE {b}", "DEFINE NAMESPACE hacked"):
			with self.assertRaises(E.SurrealDBAuthError):
				db_a.sql(statement)
		db_a.close()
		db_b.close()
		for name, user in ((a, user_a), (b, user_b)):
			with self._site_conf(name, user, "x"):
				setup_db.drop_user_and_database(name, user)

	def test_invalid_identifiers_are_rejected_before_any_statement_runs(self):
		for bad in ("a b", "a;REMOVE NAMESPACE x", "a`b", "", "x" * 65, "ünï"):
			with self.subTest(name=bad):
				with self._site_conf(bad, "ok_user", "pw"), self.assertRaises(E.SurrealDBProgrammingError):
					setup_db.setup_database(force=True, verbose=False)
				with self._site_conf("ok_db", bad, "pw"), self.assertRaises(E.SurrealDBProgrammingError):
					setup_db.setup_database(force=True, verbose=False)
				with self.assertRaises(E.SurrealDBProgrammingError):
					setup_db.drop_user_and_database(bad, "ok_user")

	def test_wrong_root_credentials(self):
		db_name, db_user, password = self.new_site()
		with (
			self._site_conf(db_name, db_user, password),
			patch.dict(frappe.flags, {"root_password": "wrong"}),
		):
			with self.assertRaises(E.SurrealDBAuthError):
				setup_db.setup_database(force=False, verbose=False)
		self.assertNotIn(db_name, self.database_names())

	# --- P1.3 driver on live data -----------------------------------------------------------------------------
	def make_site_db(self):
		db_name, db_user, password = self.new_site()
		self.provision(db_name, db_user, password)
		self.addCleanup(self._drop_site, db_name, db_user, password)
		db = self.connect(db_name, db_user, password)
		self.addCleanup(db.close)
		db.sql("DEFINE TABLE tabT SCHEMAFULL")
		db.sql("DEFINE FIELD name ON tabT TYPE string")
		db.sql("DEFINE FIELD n ON tabT TYPE int | null")
		db.sql("DEFINE INDEX uq_name ON tabT FIELDS name UNIQUE")
		db.commit()
		return db, (db_name, db_user, password)

	def _drop_site(self, db_name, db_user, password):
		with self._site_conf(db_name, db_user, password):
			setup_db.drop_user_and_database(db_name, db_user)

	def test_duplicate_and_missing_table_errors_use_frappe_visible_shapes(self):
		db, _ = self.make_site_db()
		db.sql("CREATE tabT:a SET name = $n, n = 1", {"n": "a"})
		db.commit()
		with self.assertRaises(E.SurrealDBIntegrityError) as dup:
			db.sql("CREATE tabT:b SET name = $n, n = 1", {"n": "a"})
		self.assertTrue(db.is_duplicate_entry(dup.exception))
		self.assertEqual(dup.exception.args, (1062, "Duplicate entry 'a' for key 'uq_name'"))
		db.rollback()
		with self.assertRaises(E.SurrealDBProgrammingError) as missing:
			db.sql("SELECT * FROM tabNope")
		self.assertTrue(db.is_table_missing(missing.exception))
		db.rollback()

	def test_null_round_trip_and_named_parameters(self):
		db, _ = self.make_site_db()
		db.sql("CREATE tabT:a SET name = $name, n = $n", {"name": "a", "n": None})
		db.commit()
		sql = "SELECT name, n FROM tabT WHERE name = $name /*cols:name,n*/"
		self.assertEqual(db.sql(sql, {"name": "a"}), (("a", None),))
		# the server returns keys alphabetically; the hint restores projection order
		self.assertEqual(db.sql("SELECT name, n FROM tabT /*cols:n,name*/"), ((None, "a"),))
		self.assertEqual(db.sql("SELECT name, n FROM tabT", as_dict=True)[0].n, None)

	def test_concurrent_writers_conflict_at_commit_as_query_deadlock_error(self):
		db1, creds = self.make_site_db()
		db1.sql("CREATE tabT:cc SET name = 'cc', n = 0")
		db1.commit()
		db2 = self.connect(*creds)
		self.addCleanup(db2.close)
		db1.sql("UPDATE tabT:cc SET n = n + 1")
		db2.sql("UPDATE tabT:cc SET n = n + 1")
		db2.commit()  # first committer wins
		with self.assertRaises(frappe.QueryDeadlockError):  # the loser is retryable, as ADR 0002 requires
			db1.commit()
		db1.rollback()
		self.assertEqual(db1.sql("SELECT n FROM tabT:cc"), ((1,),))

	def test_failed_statement_write_is_never_committed(self):
		db, creds = self.make_site_db()
		db.sql("CREATE tabT:a SET name = 'a', n = 1")
		db.commit()
		db.sql("CREATE tabT:z SET name = 'z', n = 9")
		with self.assertRaises(E.SurrealDBIntegrityError):
			db.sql("CREATE tabT:dup SET name = 'a', n = 2")  # the write SurrealDB 3.2.4 would keep and commit
		with self.assertRaises(E.SurrealDBTransactionTainted):
			db.commit()
		db.rollback()
		other = self.connect(*creds)
		self.addCleanup(other.close)
		self.assertEqual(other.sql("SELECT name FROM tabT ORDER BY name", pluck=True), ["a"])

	def test_connection_survives_reuse_after_commit_and_rollback(self):
		db, _ = self.make_site_db()
		for i in range(3):
			db.sql("CREATE tabT SET name = $n, n = $i", {"n": f"row{i}", "i": i})
			db.commit() if i % 2 == 0 else db.rollback()
		self.assertEqual(db.sql("SELECT name FROM tabT ORDER BY name", pluck=True), ["row0", "row2"])
