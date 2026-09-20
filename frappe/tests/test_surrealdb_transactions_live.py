"""P1.8 live tests: the ADR 0002 lock model against a real SurrealDB server, through the real query engine.

Scenarios measured in P0.4/P0.7, now on the shipped driver:
- a1/a2/a3: blocking `for_update`, NOWAIT (1205) and SKIP LOCKED
- a5: the first-insert race on a UNIQUE index - the loser's commit conflicts (the shape the retry hooks gate on)
- b/c: concurrent increments - every unit either conflicts and retries or commits; the counter never loses an update
- fence: a unit whose snapshot predates the previous lock holder's commit cannot commit silently (P0.7)
- d2b: the ghost-row replay around a failed statement (statement atomicity)
- savepoints through `frappe.db.savepoint` / `rollback(save_point=...)`

Queries are built with `frappe.qb.get_query` and executed through the connection, the same path
`get_value`/`get_values`/`set_value` take once the query is built; the probe table has no DocType
registration, so the meta layer of those helpers (core tables on the site) stays out of scope (P1.11).
A helper thread runs a second connection in its own `frappe.local` so both sides are real concurrent units."""

import contextlib
import threading
import time
import unittest

from pypika import Table

import frappe
from frappe.database.surrealdb import errors as E
from frappe.database.surrealdb import schema as S
from frappe.database.surrealdb.schema import ColumnSpec, create_statements
from frappe.database.surrealdb.translator import render
from frappe.query_builder.surrealdb_builder import SurrealDB
from frappe.tests import UnitTestCase
from frappe.tests.surrealdb_live import LIVE, SKIP_REASON, LiveSurrealDB

TABLE = Table("tabTxProbe")
SEED = (("a", 1), ("b", 2), ("c", 3), ("counter", 0))


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestSurrealDBTransactionsLive(LiveSurrealDB, UnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if frappe.db.db_type != "mariadb":
			raise unittest.SkipTest("needs the MariaDB reference site (p17_ref) as the request site")
		cls.site = frappe.local.site
		cls.sites_path = frappe.local.sites_path
		inst = cls.__new__(cls)  # the LiveSurrealDB helpers only use instance state on frappe.local
		name, user, password = inst.new_site()
		inst.provision(name, user, password)
		cls.sdb = inst.connect(name, user, password)
		cls._db_creds = (name, user, password)
		with cls.on_surreal():
			cls.sdb._conn._ensure_lock_tables()  # the lock/fence tables exist before any test reseeds
			S.clear_schema_cache()
			# The engine loads `frappe.get_meta` for dict filters; an existing-but-empty tabDocType makes
			# it raise the DoesNotExistError it catches (a missing TABLE would be an unhandled 1146).
			# The full core install is P1.11; TxProbe intentionally stays unregistered.
			for stmt in create_statements("tabDocType", []):
				frappe.db.sql_ddl(stmt)
			for stmt in create_statements(
				"tabTxProbe",
				[
					ColumnSpec("value", "int", nullable=False, default=0),
					ColumnSpec("code", "int", nullable=True, unique=True),
				],
			):
				frappe.db.sql_ddl(stmt)
			S.clear_schema_cache()
		cls.addClassCleanup(cls._teardown_db)

	@classmethod
	def _teardown_db(cls):
		with contextlib.suppress(Exception):
			cls.sdb.close()
		with contextlib.suppress(Exception):
			with cls._site_conf(*cls._db_creds):
				from frappe.database.surrealdb import setup_db

				setup_db.drop_user_and_database(cls._db_creds[0], cls._db_creds[1])

	# --- engine switch / helpers -----------------------------------------------------------------------------
	@classmethod
	@contextlib.contextmanager
	def on_surreal(cls):
		saved = (frappe.local.db, frappe.local.qb, frappe.local.conf.get("db_type"))
		frappe.local.db, frappe.local.qb = cls.sdb, SurrealDB
		frappe.local.conf["db_type"] = "surrealdb"
		try:
			yield
		finally:
			frappe.local.db, frappe.local.qb = saved[0], saved[1]
			frappe.local.conf["db_type"] = saved[2]

	@classmethod
	def _get(cls, filters, *, fields=("name",), for_update=False, wait=True, skip_locked=False,
		pluck=False, limit=None):
		"""The engine path `get_values` takes: build with frappe.qb.get_query, run through the connection."""
		kwargs = {"limit": limit} if limit is not None else {}
		q = frappe.qb.get_query(
			table="TxProbe",
			filters=filters,
			order_by="creation",
			for_update=for_update,
			skip_locked=skip_locked,
			wait=wait,
			fields=list(fields),
			**kwargs,
		)
		return q.run(as_dict=True, pluck=pluck)

	@classmethod
	def _update(cls, filters, **values):
		"""The engine path `set_value` takes."""
		q = frappe.qb.get_query(table="TxProbe", filters=filters, update=True)
		for column, value in values.items():
			q = q.set(column, value)
		q.run()

	@classmethod
	def _insert_rows(cls, rows, columns=("name", "value", "code")):
		"""INSERT through the real qb/render path. Uses `frappe.local.db`, so it is safe from worker
		threads too (each worker has its own connection in its own `frappe.local`)."""
		q = SurrealDB.into(TABLE).columns(*columns)
		for row in rows:
			q = q.insert(*(row.get(c) for c in columns))
		sql, params = render(q, None, lambda n: S.table_schema(n, db=frappe.local.db))
		frappe.local.db.sql(sql, params.values)

	@classmethod
	def _reseed(cls):
		with cls.on_surreal():
			cls.sdb.rollback()  # discard any stray unit the previous test left open (also releases its locks)
			cls.sdb.sql("DELETE FROM `tabTxProbe`")
			cls.sdb.sql("DELETE FROM `__lock`")  # a failed test may leave a held lock behind
			cls.sdb.commit()
			cls._insert_rows([{"name": n, "value": v} for n, v in SEED])
			cls.sdb.commit()

	def setUp(self):
		self._reseed()

	# --- a second real connection in its own thread ------------------------------------------------------------
	def worker(self, fn):
		"""Run `fn(db, out)` on a second connection (its own frappe.local), collecting errors."""
		errors, out = [], {}

		def run():
			db = None
			try:
				frappe.init(self.site, sites_path=self.sites_path)
				db = self.connect(*self._db_creds)
				frappe.local.db, frappe.local.qb = db, SurrealDB
				fn(db, out)
			except Exception as e:
				errors.append(f"{type(e).__name__}: {e}")
				with contextlib.suppress(Exception):
					if db is not None:
						db.rollback()
			finally:
				if db is not None:
					with contextlib.suppress(Exception):
						db.close()
				with contextlib.suppress(Exception):
					frappe.destroy()

		t = threading.Thread(target=run, daemon=True)
		t.start()
		return t, errors, out

	def holder(self, record="a", seconds=0.8):
		"""A worker that takes `for_update` on `record`, holds it, then commits. Returns
		((thread, errors, out), lock_taken_event)."""

		def fn(db, out):
			self._get({"name": record}, fields=["value"], for_update=True)
			out["taken"].set()
			time.sleep(seconds)
			db.commit()

		t, errors, out = self.worker(fn)
		out["taken"] = threading.Event()
		return (t, errors, out), out["taken"]

	# --- a1/a2/a3: FOR UPDATE modes --------------------------------------------------------------------------------
	def test_a1_for_update_blocks_until_the_holder_commits(self):
		(t, errors, _), taken = self.holder(seconds=0.8)
		self.assertTrue(taken.wait(15), "the holder never took the lock")
		with self.on_surreal():
			metrics = frappe.db._conn.metrics
			m0 = dict(metrics)
			t0 = time.monotonic()
			rows = self._get({"name": "a"}, fields=["value"], for_update=True)
			elapsed = time.monotonic() - t0
			frappe.db.rollback()  # release the lock again
		t.join(timeout=15)
		self.assertEqual(errors, [])
		self.assertGreaterEqual(elapsed, 0.4, "the read must wait for the holder's commit")
		self.assertLess(elapsed, 5.0)
		self.assertEqual(rows, [{"value": 1}], "the blocking read sees the committed state")
		self.assertEqual(metrics["lock_waits"] - m0["lock_waits"], 1)
		self.assertGreaterEqual(metrics["lock_wait_ms"] - m0["lock_wait_ms"], 400)

	def test_a2_nowait_raises_1205_immediately(self):
		(t, errors, _), taken = self.holder(seconds=0.8)
		self.assertTrue(taken.wait(15))
		with self.on_surreal():
			t0 = time.monotonic()
			with self.assertRaises(frappe.QueryTimeoutError) as cm:
				self._get({"name": "a"}, fields=["value"], for_update=True, wait=False)
			elapsed = time.monotonic() - t0
			frappe.db.rollback()
		t.join(timeout=15)
		self.assertEqual(errors, [])
		self.assertLess(elapsed, 0.5, "NOWAIT must not wait")
		# MariaDB shape: QueryTimeoutError(OperationalError(1205, ...))
		self.assertEqual(cm.exception.args[0].args[0], 1205)

	def test_a3_skip_locked_claims_only_unlocked_rows(self):
		(t, errors, _), taken = self.holder(record="a", seconds=0.8)
		self.assertTrue(taken.wait(15))
		with self.on_surreal():
			rows = self._get({"value": [">", 0]}, fields=["name"], for_update=True, skip_locked=True, pluck=True)
			frappe.db.rollback()
		t.join(timeout=15)
		self.assertEqual(errors, [])
		self.assertEqual(sorted(rows), ["b", "c"], "row a is locked by the other unit and is skipped")

	# --- a5: the first-insert race ---------------------------------------------------------------------------------
	def test_a5_first_insert_race_conflicts_the_loser_at_commit(self):
		inserted = threading.Event()
		outcome = {}

		def loser(db, out):
			self._insert_rows([{"name": "race-1", "value": 1, "code": 42}])
			inserted.set()
			time.sleep(0.8)  # the winner inserts + commits meanwhile
			try:
				db.commit()
				outcome["loser"] = "committed"
			except frappe.QueryDeadlockError:
				db.rollback()
				outcome["loser"] = "conflict"

		t, errors, _ = self.worker(loser)
		self.assertTrue(inserted.wait(15))
		with self.on_surreal():
			self._insert_rows([{"name": "race-2", "value": 2, "code": 42}])
			frappe.db.commit()  # the winner commits while the loser's unit is still open
		t.join(timeout=15)
		self.assertEqual(errors, [])
		self.assertEqual(outcome.get("loser"), "conflict", "P0.4 a5: the loser's commit raises the conflict")
		with self.on_surreal():
			self.assertEqual(
				self._get({"code": 42}, fields=["name"], pluck=True), ["race-2"], "exactly one row has code 42"
			)

	# --- the set_value-shaped update path persists through a commit ---------------------------------------------
	def test_set_value_shaped_update_persists(self):
		with self.on_surreal():
			q = frappe.qb.get_query(table="TxProbe", filters={"name": "counter"}, update=True)
			q = q.set("value", 5)
			sql, params = render(q, None, lambda n: S.table_schema(n, db=frappe.local.db))
			frappe.db.sql(sql, params.values)
			frappe.db.commit()
			rows = self._get({"name": "counter"}, fields=["value"])
			self.assertEqual(rows, [{"value": 5}], f"SQL was: {sql!r} params: {params.values!r}")

	# --- a unit opened by commit's trailing begin() reads fresh data (MariaDB: no snapshot at BEGIN) -------------
	def test_commit_started_unit_reads_fresh_data(self):
		outcome = {}

		def fn(db, out):
			outcome["before"] = self._get({"name": "counter"}, fields=["value"])
			self._update({"name": "counter"}, value=9)
			outcome["after_update"] = self._get({"name": "counter"}, fields=["value"])  # sees its own write
			db.commit()
			outcome["after_commit"] = self._get({"name": "counter"}, fields=["value"])

		t, errors, _ = self.worker(fn)
		t.join(timeout=15)
		self.assertEqual(errors, [])
		self.assertEqual(outcome["after_update"], [{"value": 9}], "the unit must see its own write")
		with self.on_surreal():
			self.assertEqual(self._get({"name": "counter"}, fields=["value"]), [{"value": 9}])

	# --- b/c: concurrent increments --------------------------------------------------------------------------------
	def test_bc_concurrent_increments_never_lose_an_update(self):
		N, M = 4, 5
		threads, outs = [], []

		for _ in range(N):
			def job(db, out):
				for _ in range(M):
					for attempt in range(25):
						try:
							value = self._get({"name": "counter"}, fields=["value"])[0]["value"]
							self._update({"name": "counter"}, value=value + 1)
							db.commit()
							break
						except frappe.QueryDeadlockError:
							db.rollback()
							time.sleep(0.01 * (attempt + 1))
					else:
						out["gave_up"] = out.get("gave_up", 0) + 1

			t, errors, out = self.worker(job)
			threads.append((t, errors))
			outs.append(out)

		for t, _ in threads:
			t.join(timeout=60)
		for _, errors in threads:
			self.assertEqual(errors, [])
		for out in outs:
			self.assertEqual(out.get("gave_up", 0), 0, "a worker exhausted its retries")
		with self.on_surreal():
			self.assertEqual(
				self._get({"name": "counter"}, fields=["value"])[0]["value"], N * M, "no lost update"
			)

	# --- the fence: a stale snapshot cannot commit silently -----------------------------------------------------------
	def test_fence_conflicts_a_unit_that_waited_for_a_committed_lock_holder(self):
		(t, errors, _), taken = self.holder(record="c", seconds=0.8)
		self.assertTrue(taken.wait(15))
		outcome = {}

		def second(db, out):
			self._update({"name": "b"}, value=111)  # opens the unit: the snapshot is pinned
			# the holder still owns c; the CAS below polls until the holder's commit releases it, so the
			# grant lands on a stale snapshot. The fence write inside this unit must make the commit
			# conflict (P0.7) instead of silently committing on top of the holder's state.
			self._get({"name": "c"}, fields=["value"], for_update=True)
			try:
				frappe.db.commit()
				outcome["second"] = "committed"
			except frappe.QueryDeadlockError:
				frappe.db.rollback()
				outcome["second"] = "conflict"

		t2, errors2, _ = self.worker(second)
		t.join(timeout=15)
		t2.join(timeout=15)
		self.assertEqual(errors, [])
		self.assertEqual(errors2, [])
		self.assertEqual(outcome.get("second"), "conflict", "P0.7: the fence catches the stale snapshot")
		with self.on_surreal():  # recovery: a fresh unit sees the committed state
			self.assertEqual(self._get({"name": "c"}, fields=["value"])[0]["value"], 3)

	# --- d2b: ghost-row replay around a failed statement --------------------------------------------------------------
	def test_d2b_failed_unique_write_is_replayed_without_the_ghost_row(self):
		with self.on_surreal():
			self._insert_rows([{"name": "uq-1", "value": 1}])  # committed baseline (code stays NULL)
			frappe.db.commit()
			replays = frappe.db._conn.metrics["replays"]
			# one statement, two rows with the same unique value: the pre-check only compares against
			# committed data, so the statement reaches the write, SurrealDB fails it mid-statement and
			# leaves row one behind as a ghost (P0.4 d2b). The replay must undo the failed statement.
			with self.assertRaises(E.SurrealDBIntegrityError) as cm:
				self._insert_rows(
					[{"name": "uq-2a", "value": 2, "code": 8}, {"name": "uq-2b", "value": 3, "code": 8}]
				)
			self.assertEqual(cm.exception.args[0], 1062)
			frappe.db.commit()  # the unit commits without the failed statement
			self.assertEqual(frappe.db._conn.metrics["replays"], replays + 1)
			self.assertEqual(self._get({"name": "uq-1"}), [{"name": "uq-1"}])
			self.assertEqual(self._get({"name": "uq-2a"}), [], "the ghost row is gone")
			self.assertEqual(self._get({"name": "uq-2b"}), [], "the unattempted row is gone")

	# --- savepoints ----------------------------------------------------------------------------------------------------
	def test_savepoints_roll_back_to_the_mark(self):
		with self.on_surreal():
			self._insert_rows([{"name": "sp-1", "value": 1}])
			frappe.db.savepoint("sp1")
			self._insert_rows([{"name": "sp-2", "value": 2}])
			frappe.db.rollback(save_point="sp1")
			self._insert_rows([{"name": "sp-3", "value": 3}])
			frappe.db.commit()
			self.assertEqual(self._get({"name": "sp-1"}), [{"name": "sp-1"}])
			self.assertEqual(self._get({"name": "sp-2"}), [])
			self.assertEqual(self._get({"name": "sp-3"}), [{"name": "sp-3"}])

	def test_missing_savepoint_raises_1305(self):
		with self.on_surreal():
			with self.assertRaises(E.SurrealDBProgrammingError) as cm:
				frappe.db.rollback(save_point="missing-sp")
			self.assertEqual(cm.exception.args[0], 1305)
