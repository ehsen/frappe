import decimal
import subprocess
import sys
from unittest.mock import patch

import frappe
from frappe.database import get_db
from frappe.database.surrealdb import connection as C
from frappe.database.surrealdb import errors as E
from frappe.tests import UnitTestCase


class RecordID:
	"""Stand-in with the SDK's class name (the driver recognises it by name so the SDK stays optional)."""

	def __init__(self, table, id):
		self.table_name, self.id = table, id


class FakeServer:
	"""Scripted stand-in for the SDK client. `responder(query, params, txn)` returns a query_raw-style dict."""

	def __init__(self, responder=None):
		self.responder = responder or (lambda q, p, t: {"result": [{"status": "OK", "result": []}]})
		self.calls = []
		self.txn_counter = 0
		self.fail_next_transport = 0
		self.clients = 0

	def factory(self, url):
		self.clients += 1
		return FakeClient(self, url)


class FakeClient:
	def __init__(self, server, url):
		self.server, self.url, self.closed = server, url, False

	def signin(self, creds):
		self.server.calls.append(("signin", dict(creds)))

	def use(self, ns, db):
		self.server.calls.append(("use", ns, db))

	def begin(self):
		self.server.txn_counter += 1
		self.server.calls.append(("begin",))
		return f"txn-{self.server.txn_counter}"

	def commit(self, txn):
		self.server.calls.append(("commit", txn))
		if error := getattr(self.server, "commit_error", None):
			raise error

	def cancel(self, txn):
		self.server.calls.append(("cancel", txn))

	def close(self):
		self.closed = True

	def query_raw(self, query, params, txn_id=None):
		self.server.calls.append(("query", query, params, txn_id))
		if self.server.fail_next_transport:
			self.server.fail_next_transport -= 1
			raise ConnectionResetError("socket closed")
		return self.server.responder(query, params, txn_id)


def ok(*results):
	return {"result": [{"status": "OK", "result": r} for r in results]}


def make_db(server):
	"""A SurrealDBDatabase wired to a fake server (as Database.connect() would do)."""
	with patch.dict(frappe.local.conf, {"db_type": "surrealdb"}):
		db = get_db(host="h", port=8000, user="u", password="p", cur_db_name="site_db")
	params = C.ConnectionParams(
		url="ws://h:8000", namespace="frappe", database="site_db", username="u", password="p"
	)
	db._conn = C.SurrealConnection(params, client_factory=server.factory).open()
	db._cursor = db._conn.cursor()
	return db


def queries(server):
	return [c for c in server.calls if c[0] == "query"]


class TestSurrealDBDriver(UnitTestCase):
	def test_sdk_is_not_imported_by_the_backend_package(self):
		code = (
			"import sys, frappe.database.surrealdb.database, frappe.database.surrealdb.setup_db, "
			"frappe.database.surrealdb.connection; assert 'surrealdb' not in sys.modules, 'SDK imported eagerly'"
		)
		result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
		self.assertEqual(result.returncode, 0, result.stderr[-500:])

	def test_signin_and_use(self):
		server = FakeServer()
		make_db(server)
		self.assertEqual(
			server.calls[0],
			("signin", {"username": "u", "password": "p", "namespace": "frappe", "database": "site_db"}),
		)
		self.assertEqual(server.calls[1], ("use", "frappe", "site_db"))

	def test_named_params_only_and_null_encoding(self):
		server = FakeServer()
		db = make_db(server)
		db.sql("SELECT * FROM t WHERE a = $a", {"a": None, "b": [1, None], "c": "x"})
		params = queries(server)[-1][2]
		self.assertIs(params["a"], C.SNULL)
		self.assertIs(params["b"][1], C.SNULL)
		self.assertEqual(params["c"], "x")
		with self.assertRaises(E.SurrealDBProgrammingError):
			db.sql("SELECT * FROM t WHERE a = %s", (1,))

	def test_temporal_values_are_refused(self):
		import datetime

		server = FakeServer()
		db = make_db(server)
		for value in (
			datetime.datetime(2024, 1, 1),
			datetime.date(2024, 1, 1),
			datetime.time(1, 2),
			datetime.timedelta(1),
		):
			with self.subTest(type=type(value).__name__), self.assertRaises(E.SurrealDBProgrammingError):
				db.sql("SELECT * FROM t WHERE a = $a", {"a": value})
		self.assertEqual(queries(server), [], "nothing may reach the server")

	def test_result_shaping_for_frappe_sql(self):
		rows = [
			{"id": RecordID("tabToDo", "a"), "name": "a", "n": decimal.Decimal("1.5")},
			{"id": RecordID("tabToDo", "b"), "name": "b", "n": None, "extra": 1},
		]
		server = FakeServer(lambda q, p, t: ok(rows))
		db = make_db(server)
		# positional read needs the translator's column-order hint; missing fields become None
		hinted = "SELECT * FROM tabToDo /*cols:name,n,extra*/"
		self.assertEqual(db.sql(hinted), (("a", 1.5, None), ("b", None, 1)))
		self.assertEqual(db.sql("SELECT * FROM tabToDo /*cols:extra,name*/"), ((None, "a"), (1, "b")))
		with self.assertRaises(E.SurrealDBProgrammingError) as cm:  # silent scrambling is not allowed
			db.sql("SELECT * FROM tabToDo")
		self.assertIn("/*cols:", str(cm.exception.message))
		as_dict = db.sql("SELECT * FROM tabToDo", as_dict=True)
		self.assertEqual(
			[dict(r) for r in as_dict],
			[{"name": "a", "n": 1.5, "extra": None}, {"name": "b", "n": None, "extra": 1}],
		)
		self.assertEqual(db.sql("SELECT name FROM tabToDo /*cols:name*/", pluck=True), ["a", "b"])

	def test_scalar_and_empty_results(self):
		server = FakeServer(lambda q, p, t: ok(["x", "y"]) if "VALUE" in q else ok([]))
		db = make_db(server)
		self.assertEqual(db.sql("SELECT VALUE name FROM tabToDo", pluck=True), ["x", "y"])
		self.assertEqual(db.sql("SELECT * FROM tabToDo"), ())
		self.assertEqual(db.sql("SELECT * FROM tabToDo", as_dict=True), [])

	def test_writes_return_no_result_set(self):
		server = FakeServer(lambda q, p, t: ok([{"id": RecordID("t", "1"), "name": "1"}]))
		db = make_db(server)
		self.assertEqual(db.sql("CREATE t:1 SET name = $n", {"n": "1"}), ())
		self.assertEqual(db._cursor.rowcount, 1)
		# a script whose LAST statement is a select still returns rows
		server.responder = lambda q, p, t: ok(None, [{"name": "1"}])
		self.assertEqual(db.sql("LET $x = 1; SELECT name FROM t"), (("1",),))

	def test_every_statement_status_is_checked(self):
		def responder(q, p, t):
			return {
				"result": [
					{"status": "OK", "result": [{"name": "a"}]},
					{
						"status": "ERR",
						"result": "Database index `uq_name` already contains 'a', with record `t:a`",
					},
					{"status": "OK", "result": [{"name": "a"}]},
				]
			}

		db = make_db(FakeServer(responder))
		with self.assertRaises(E.SurrealDBIntegrityError) as cm:
			db.sql("SELECT * FROM t; CREATE t:g SET name = 'a'; SELECT * FROM t")
		self.assertTrue(db.is_duplicate_entry(cm.exception))
		self.assertEqual(cm.exception.args, (1062, "Duplicate entry 'a' for key 'uq_name'"))

	def test_rpc_level_error(self):
		server = FakeServer(
			lambda q, p, t: {"error": {"kind": "Validation", "code": -32000, "message": "Parse error: x"}}
		)
		db = make_db(server)
		with self.assertRaises(E.SurrealDBProgrammingError) as cm:
			db.sql("SELEC 1")
		self.assertTrue(db.is_syntax_error(cm.exception))

	def test_implicit_transaction_commit_and_begin(self):
		server = FakeServer()
		db = make_db(server)
		db.sql("SELECT 1")
		db.sql("SELECT 2")
		self.assertEqual(
			[c for c in server.calls if c[0] == "begin"], [("begin",)], "one implicit transaction"
		)
		self.assertTrue(all(c[3] == "txn-1" for c in queries(server)))
		db.commit()  # Database.commit = `commit` then `START TRANSACTION`
		self.assertIn(("commit", "txn-1"), server.calls)
		# MariaDB: START TRANSACTION takes no snapshot - the transaction opens with the first real
		# statement, so the unit started by the trailing begin() reads fresh data (P1.8 live finding)
		self.assertNotIn(("begin",), server.calls[server.calls.index(("commit", "txn-1")) :])
		db.sql("SELECT 3")
		self.assertEqual(queries(server)[-1][3], "txn-2")
		self.assertEqual([c for c in server.calls if c[0] == "begin"], [("begin",), ("begin",)])

	def test_rollback_cancels(self):
		server = FakeServer()
		db = make_db(server)
		db.sql("SELECT 1")
		db.rollback()
		self.assertIn(("cancel", "txn-1"), server.calls)

	def test_failed_first_statement_does_not_taint_the_unit(self):
		state = {"fail": True}

		def responder(q, p, t):
			if state["fail"]:
				return {"result": [{"status": "ERR", "result": "Database record `t:a` already exists"}]}
			return ok([])

		server = FakeServer(responder)
		db = make_db(server)
		with self.assertRaises(E.SurrealDBIntegrityError):
			db.sql("CREATE t:a SET name = 'a'")  # failed before writing anything (P1.8: no replay needed)
		state["fail"] = False
		db.sql("SELECT 1")  # the transaction is still usable...
		db.commit()  # ...and commits: the failed statement wrote nothing
		self.assertTrue(any(c[0] == "commit" for c in server.calls))

	def test_failed_post_write_statement_is_replayed_without_the_ghost_row(self):
		state = {"fail": True}

		def responder(q, p, t):
			if state["fail"] and "CREATE t:b" in q:
				return {
					"result": [
						{"status": "ERR", "result": "Database index `uq` already contains 'b', with record `t:b`"}
					]
				}
			return ok([])

		server = FakeServer(responder)
		db = make_db(server)
		db.sql("CREATE t:a SET name = 'a'")  # logged
		with self.assertRaises(E.SurrealDBIntegrityError):
			db.sql("CREATE t:b SET name = 'b'")  # wrote a ghost row, then failed -> replay drops it
		state["fail"] = False
		db.sql("SELECT 1")  # the transaction is still usable...
		db.commit()  # ...and commits
		self.assertTrue(any(c[0] == "commit" for c in server.calls))
		# the unit was replayed on a fresh transaction: prior writes survive, the failed statement
		# is dropped and was attempted exactly once
		self.assertIn(("cancel", "txn-1"), server.calls)
		txn2 = [c for c in queries(server) if c[3] == "txn-2"]
		self.assertEqual([c[1] for c in txn2 if "CREATE" in c[1]], ["CREATE t:a SET name = 'a'"])
		self.assertEqual(len([c for c in queries(server) if "CREATE t:b" in c[1]]), 1)

	def test_commit_conflict_becomes_query_deadlock_error(self):
		server = FakeServer()
		server.commit_error = type("QueryError", (Exception,), {"details": {"kind": "TransactionConflict"}})(
			"Transaction conflict: Resource busy. This transaction can be retried"
		)
		db = make_db(server)
		db.sql("SELECT 1")
		with self.assertRaises(frappe.QueryDeadlockError):
			db.commit()

	def test_read_only_query_is_retried_once_after_connection_loss(self):
		server = FakeServer(lambda q, p, t: ok([{"name": "a"}]))
		db = make_db(server)
		db._conn.commit()  # no transaction open, nothing pending
		server.fail_next_transport = 1
		self.assertEqual(db.sql("SELECT name FROM t"), (("a",),))
		self.assertEqual(server.clients, 2, "reconnected exactly once")

	def test_write_is_never_retried_after_connection_loss(self):
		server = FakeServer()
		db = make_db(server)
		server.fail_next_transport = 1
		with self.assertRaises(E.SurrealDBConnectionError):
			db.sql("CREATE t:1 SET name = 'a'")
		self.assertEqual(server.clients, 1)
		self.assertEqual(len(queries(server)), 1, "the write was attempted exactly once")
		with self.assertRaises(
			E.SurrealDBConnectionError
		):  # and the connection stays unusable until reconnected
			db.sql("SELECT 1")

	def test_connection_loss_inside_a_transaction_is_not_retried(self):
		server = FakeServer()
		db = make_db(server)
		db.sql("SELECT 1")  # opens the implicit transaction
		server.fail_next_transport = 1
		with self.assertRaises(E.SurrealDBConnectionError):
			db.sql("SELECT 2")
		self.assertEqual(server.clients, 1)

	def test_savepoint_rolls_the_log_back_to_the_mark(self):
		server = FakeServer()
		db = make_db(server)
		db.sql("CREATE t:a SET name = 'a'")
		db.savepoint("sp1")  # driver-emulated: a log mark, no server round-trip
		db.sql("CREATE t:b SET name = 'b'")
		db.rollback(save_point="sp1")  # -> `rollback to savepoint sp1`
		db.commit()
		# replayed on a fresh transaction: writes before the mark survive, writes after it are gone
		self.assertIn(("cancel", "txn-1"), server.calls)
		txn2 = [c[1] for c in queries(server) if c[3] == "txn-2"]
		self.assertEqual([s for s in txn2 if "CREATE" in s], ["CREATE t:a SET name = 'a'"])
		self.assertIn(("commit", "txn-2"), server.calls)

	def test_savepoints_nest_and_rollback_to_releases_later_marks(self):
		server = FakeServer()
		db = make_db(server)
		db.sql("CREATE t:a SET name = 'a'")
		db.savepoint("sp1")
		db.sql("CREATE t:b SET name = 'b'")
		db.savepoint("sp2")
		db.sql("CREATE t:c SET name = 'c'")
		db.rollback(save_point="sp1")  # MariaDB: ROLLBACK TO releases savepoints taken after it
		with self.assertRaises(E.SurrealDBProgrammingError) as cm:
			db.rollback(save_point="sp2")
		self.assertEqual(cm.exception.args, (1305, "SAVEPOINT sp2 does not exist"))
		db.commit()
		txn2 = [c[1] for c in queries(server) if c[3] == "txn-2"]
		self.assertEqual([s for s in txn2 if "CREATE" in s], ["CREATE t:a SET name = 'a'"])

	def test_release_savepoint_keeps_the_writes_but_drops_the_mark(self):
		server = FakeServer()
		db = make_db(server)
		db.savepoint("sp1")
		db.release_savepoint("sp1")
		with self.assertRaises(E.SurrealDBProgrammingError) as cm:
			db.rollback(save_point="sp1")
		self.assertEqual(cm.exception.args[0], 1305)
		db.sql("CREATE t:a SET name = 'a'")
		db.commit()
		self.assertTrue(any(c[0] == "commit" for c in server.calls))

	def test_signin_failure_is_classified(self):
		class BadClient(FakeClient):
			def signin(self, creds):
				raise type("NotAllowedError", (Exception,), {})("There was a problem with authentication")

		params = C.ConnectionParams(url="ws://h:8000", database="d", username="u", password="bad")
		with self.assertRaises(E.SurrealDBAuthError):
			C.SurrealConnection(params, client_factory=lambda url: BadClient(FakeServer(), url)).open()

	def test_last_statement_word(self):
		self.assertEqual(C.last_statement_word("SELECT 1"), "select")
		self.assertEqual(C.last_statement_word("LET $x = 'a;b'; CREATE t:1;"), "create")
		self.assertEqual(C.last_statement_word("UPDATE t SET a = 'x;SELECT'; select 1"), "select")
