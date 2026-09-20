"""P1.8 fake-layer tests: application locks (AppLock / fence / heartbeat) against a scripted server, the
unit-of-work retry hooks of ADR 0002 and the request/job retry loops. The live lock behaviour (blocking,
NOWAIT, SKIP LOCKED, duplicate races) is in test_surrealdb_transactions_live.py."""

import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import frappe
import frappe.app  # `_sync_and_retry` lives here; `frappe.app` needs the explicit import
from frappe.database.surrealdb import errors as E
from frappe.database.surrealdb import transactions as T
from frappe.database.surrealdb.transactions import AppLock, LockTimeout
from frappe.tests import UnitTestCase
from frappe.tests.test_surrealdb_driver import FakeServer, make_db, ok, queries

CONFLICT = type("QueryError", (Exception,), {"details": {"kind": "TransactionConflict"}})(
	"Transaction conflict: Resource busy. This transaction can be retried"
)


def granting_responder(locked_keys=()):
	"""CAS polls grant (echo the owner) unless the lock record belongs to one of `locked_keys`."""

	def responder(q, p, t):
		if "owner = $o" in q:
			rid = str(getattr(p.get("r"), "id", p.get("r")))
			if any(k in rid for k in locked_keys):
				return ok([])
			return ok([{"owner": p["o"]}])
		return ok([])

	return responder


def script_responder(ids, rows, cas=None):
	"""Answers the two-statement lock script with the ids statement and the query itself; `cas` answers the
	CAS polls (default: grant everything)."""

	def responder(q, p, t):
		if "SELECT" in q:
			return ok([{"__lk0": i} for i in ids], rows)
		if "owner = $o" in q:
			return (cas or (lambda q_, p_, t_: ok([{"owner": p_["o"]}])))("", p, t)
		return ok([])

	return responder


class TestSurrealDBTransactions(UnitTestCase):
	# --- AppLock: the CAS building block ------------------------------------------------------------------
	def test_applock_acquire_records_the_cas(self):
		server = FakeServer(granting_responder())
		db = make_db(server)
		waited = AppLock(db._conn, "tabT:a", ttl=2.0).acquire(1.0)
		self.assertLess(waited, 0.5)
		cas = [c for c in queries(server) if "owner = $o" in c[1]]
		self.assertEqual(len(cas), 1)
		self.assertIsNone(cas[0][3], "the CAS runs in autocommit, outside the unit of work")
		from frappe.database.surrealdb import collation

		self.assertEqual(str(cas[0][2]["r"].id), collation.record_id("tabT:a"))

	def test_record_ids_survive_a_missing_sdk(self):
		# the ledger benches run without the SDK; `lock_record_id`/`fence_record_id` must fall back to
		# the name-compatible stand-in instead of erroring (the P1.8 ledger run hit exactly this)
		from frappe.database.surrealdb import collation
		from frappe.database.surrealdb.transactions import fence_record_id, lock_record_id

		with patch.dict("sys.modules", {"surrealdb": None}):
			rid = lock_record_id("tabT:a")
			fid = fence_record_id("tabT:a")
		self.assertEqual(type(rid).__name__, "RecordID")
		self.assertEqual((rid.table_name, str(rid.id)), ("__lock", collation.record_id("tabT:a")))
		self.assertEqual((fid.table_name, str(fid.id)), ("__fence", collation.record_id("tabT:a")))

	def test_applock_waits_for_the_holder_then_times_out(self):
		state = {"n": 0}

		def grant_third(q, p, t):
			if "owner = $o" in q:
				if "k" != str(getattr(p.get("r"), "id", p.get("r"))).split("/")[-1]:
					return ok([])  # `k2` is held elsewhere and never granted
				state["n"] += 1
				if state["n"] >= 3:
					return ok([{"owner": p["o"]}])
				return ok([])
			return ok([])

		db = make_db(FakeServer(grant_third))
		waited = AppLock(db._conn, "k", ttl=2.0).acquire(2.0)
		self.assertGreaterEqual(state["n"], 3)
		self.assertGreater(waited, 0.0)
		with self.assertRaises(LockTimeout):
			AppLock(db._conn, "k2", ttl=2.0).acquire(0.05)

	def test_applock_refresh_and_release(self):
		server = FakeServer(granting_responder())
		db = make_db(server)
		lock = AppLock(db._conn, "k", ttl=60.0)
		lock.acquire(1.0)
		self.assertTrue(lock.refresh())
		lock.release()
		self.assertTrue(any("owner = NONE" in c[1] for c in queries(server)))

	def test_applock_survives_conflicts_on_the_lock_record(self):
		state = {"cas": 0, "rel": 0}

		def flaky(q, p, t):
			if "RETURN AFTER" in q:  # the CAS (the release text has no RETURN AFTER)
				state["cas"] += 1
				if state["cas"] == 1:
					raise E.SurrealDBTransactionConflict(1213, "conflict")
				return ok([{"owner": p["o"]}])
			if "owner = NONE" in q:  # the release
				state["rel"] += 1
				if state["rel"] == 1:
					raise E.SurrealDBTransactionConflict(1213, "conflict")
				return ok([])
			return ok([])

		db = make_db(FakeServer(flaky))
		lock = AppLock(db._conn, "k", ttl=2.0)
		self.assertGreater(lock.acquire(2.0), 0.0)  # the contended CAS is just a lost poll
		lock.release()  # the contended release is retried, never raised
		self.assertEqual(state["rel"], 2)

	# --- connection.lock: define-once, fence, snapshot drop, release ---------------------------------------
	def test_lock_defines_tables_once_and_writes_the_fence(self):
		server = FakeServer(granting_responder())
		conn = make_db(server)._conn
		self.assertTrue(conn.lock("tabT:a", timeout=0.5, ttl=60.0))
		self.assertEqual(
			len([c[1] for c in queries(server) if "DEFINE TABLE IF NOT EXISTS __lock" in c[1]]), 1
		)
		self.assertFalse(conn.lock("tabT:a", timeout=0.5), "already held: not newly granted")
		self.assertEqual(len([c for c in queries(server) if "owner = $o" in c[1]]), 1)
		self.assertTrue(conn.holds("tabT:a"))
		self.assertFalse(conn.holds("tabT:b"))
		# the fence write runs inside the unit of work and is logged for replay like any other write
		self.assertEqual(len(conn._log), 1)
		self.assertEqual(conn._log[0][1]["r"].table_name, T.FENCE_TABLE)

	def test_lock_drops_a_read_only_snapshot_for_a_fresh_one(self):
		server = FakeServer(granting_responder())
		conn = make_db(server)._conn
		conn.execute("SELECT 1")  # a transaction that only read so far (lazy begin: no read, no txn)
		conn.lock("tabT:a", timeout=0.5)
		self.assertIn(("cancel", "txn-1"), server.calls)
		txn2 = [c for c in queries(server) if c[3] == "txn-2"]
		self.assertTrue(
			any(getattr(c[2].get("r"), "table_name", "") == T.FENCE_TABLE for c in txn2),
			"the fence write opens the fresh snapshot",
		)

	def test_commit_and_rollback_release_the_locks(self):
		server = FakeServer(granting_responder())
		conn = make_db(server)._conn
		conn.lock("tabT:a", timeout=0.5)
		conn.commit()
		self.assertTrue(any("owner = NONE" in c[1] for c in queries(server)))
		self.assertEqual(conn._locks, [])
		self.assertEqual(conn._log, [])
		self.assertEqual(conn.units_committed, 1)

		server = FakeServer(granting_responder())
		conn = make_db(server)._conn
		conn.lock("tabT:a", timeout=0.5)
		conn.rollback()
		self.assertTrue(any("owner = NONE" in c[1] for c in queries(server)))
		self.assertEqual(conn._locks, [])

	def test_commit_conflict_releases_and_reports(self):
		server = FakeServer(granting_responder())
		server.commit_error = CONFLICT
		conn = make_db(server)._conn
		conn.lock("tabT:a", timeout=0.5)
		with self.assertRaises(E.SurrealDBTransactionConflict):
			conn.commit()
		self.assertTrue(any("owner = NONE" in c[1] for c in queries(server)))
		self.assertEqual(conn.metrics["conflicts"], 1)
		self.assertEqual(conn._log, [])
		self.assertEqual(conn.units_committed, 0)

	# --- heartbeat ------------------------------------------------------------------------------------------
	def test_heartbeat_refreshes_only_after_the_interval(self):
		server = FakeServer(granting_responder())
		conn = make_db(server)._conn
		conn.lock("tabT:a", timeout=0.5, ttl=60.0)
		conn.execute("SELECT 1")  # just after the grant: no refresh needed
		self.assertEqual(len([c for c in queries(server) if "owner = $o" in c[1]]), 1)
		conn._last_beat = time.monotonic() - 30  # ttl/3 = 20 s -> overdue
		conn._maybe_heartbeat()
		self.assertEqual(len([c for c in queries(server) if "owner = $o" in c[1]]), 2)

	def test_heartbeat_loss_is_counted_never_raised(self):
		def dying(q, p, t):
			if "owner = $o" in q:
				raise E.SurrealDBTransactionConflict(1213, "conflict")
			return ok([])

		server = FakeServer(granting_responder())
		conn = make_db(server)._conn
		conn.lock("tabT:a", timeout=0.5, ttl=60.0)
		server.responder = dying
		conn._last_beat = time.monotonic() - 30
		conn._maybe_heartbeat()  # must not raise
		self.assertEqual(conn.metrics["lock_lost"], 1)

	# --- cursor lock paths ----------------------------------------------------------------------------------
	def test_cursor_key_lock_runs_before_the_read(self):
		def responder(q, p, t):
			if "owner = $o" in q:
				return ok([{"owner": p["o"]}])
			if "SELECT name FROM tabT" in q:
				return ok([{"name": "a"}])
			return ok([])

		server = FakeServer(responder)
		db = make_db(server)
		self.assertEqual(db.sql("SELECT name FROM tabT WHERE name = 'a' /*cols:name*/ /*lock:l:k:tabT:a*/"), (("a",),))
		qs = [c[1] for c in queries(server)]
		fence = next(i for i, s in enumerate(qs) if "__fence" in s)
		select = next(i for i, s in enumerate(qs) if "SELECT name FROM tabT" in s)
		self.assertLess(fence, select, "the record is locked (and fenced) before it is read")
		cas = next(c for c in queries(server) if "owner = $o" in c[1])
		from frappe.database.surrealdb import collation

		self.assertEqual(str(cas[2]["r"].id), collation.record_id("tabT:a"))

	def test_skip_locked_claims_only_unlocked_rows(self):
		state = {"n": 0}

		def cas(q, p, t):
			state["n"] += 1
			if state["n"] == 1:
				return ok([])  # row `a` is locked by someone else
			return ok([{"owner": p["o"]}])

		db = make_db(FakeServer(script_responder(["a", "b", "c"], [{"__lk0": i, "name": i} for i in "abc"], cas)))
		rows = db.sql("SELECT name FROM tabT WHERE value > 0 /*cols:name*/ /*lock:s:t:tabT:*/")
		self.assertEqual(rows, (("b",), ("c",)), "the locked row is dropped, the claimed rows remain")

	def test_skip_locked_counts_the_units_own_locks_as_claimed(self):
		state = {"n": 0}

		def cas(q, p, t):
			state["n"] += 1
			if state["n"] == 1:
				return ok([])  # someone else holds it - but this unit locked it earlier...
			return ok([{"owner": p["o"]}])

		db = make_db(FakeServer(script_responder(["a"], [{"__lk0": "a", "name": "a"}], cas)))
		conn = db._conn
		conn._locks.append(
			SimpleNamespace(key="tabT:a", ttl=60_000_000, owner="x", refresh=lambda: True, release=lambda: None)
		)  # the unit already holds this row's lock
		rows = db.sql("SELECT name FROM tabT WHERE value > 0 /*cols:name*/ /*lock:s:t:tabT:*/")
		self.assertEqual(rows, (("a",),), "MariaDB's SKIP LOCKED does not skip a transaction's own locks")

	def test_nowait_row_lock_raises_1205(self):
		def cas(q, p, t):
			return ok([])  # held elsewhere, never granted

		db = make_db(FakeServer(script_responder(["a"], [{"__lk0": "a", "name": "a"}], cas)))
		# Database.sql maps the driver's 1205 to frappe's timeout error, wrapping the driver error
		# exactly like MariaDB's: QueryTimeoutError(OperationalError(1205, ...))
		with self.assertRaises(frappe.QueryTimeoutError) as cm:
			db.sql("SELECT name FROM tabT WHERE value > 0 /*cols:name*/ /*lock:n:t:tabT:*/")
		self.assertEqual(cm.exception.args[0].args[0], 1205)

	def test_skip_locked_stops_after_the_query_limit(self):
		state = {"n": 0}

		def cas(q, p, t):
			state["n"] += 1
			self.assertLessEqual(state["n"], 2, "only `want` rows may be claimed")
			return ok([{"owner": p["o"]}])

		db = make_db(FakeServer(script_responder(["a", "b", "c"], [{"__lk0": "a", "name": "a"}], cas)))
		rows = db.sql("SELECT name FROM tabT WHERE value > 0 /*cols:name*/ /*lock:s:t:tabT:2*/")
		self.assertEqual(rows, (("a",),))  # LIMIT 2: b and c are claimed but filtered out of the result

	# --- unique pre-check (ADR 0002) --------------------------------------------------------------------------
	def test_txn_read_returns_the_rows_not_the_statement_wrapper(self):
		server = FakeServer(lambda q, p, t: ok(None, [{"id": "t:1"}]))
		conn = make_db(server)._conn
		conn.begin()
		self.assertEqual(conn._txn_read("SELECT VALUE id FROM t", {}), [{"id": "t:1"}])
		server.responder = lambda q, p, t: ok([])  # no match: an empty rows list, never [[]]
		self.assertEqual(conn._txn_read("SELECT VALUE id FROM t", {}), [])

	def test_unique_precheck_skips_null_values(self):
		# MariaDB: NULL never violates UNIQUE - bound params arrive encoded (None became SNULL)
		from types import SimpleNamespace

		from frappe.database.surrealdb.connection import SNULL

		server = FakeServer(granting_responder())
		conn = make_db(server)._conn
		conn._schema_loader = lambda table: SimpleNamespace(
			indexes={"code": {"name": "code", "fields": ["code"], "unique": True, "kind": "UNIQUE"}}
		)
		conn._precheck_insert("INSERT INTO `tabT` $p /*uq:tabT:p*/", {"p": [{"code": SNULL}]})
		self.assertEqual(len([c for c in queries(server) if "SELECT VALUE id" in c[1]]), 0)

	def test_unique_precheck_raises_on_a_real_duplicate(self):
		from types import SimpleNamespace

		server = FakeServer(granting_responder())
		conn = make_db(server)._conn
		conn._schema_loader = lambda table: SimpleNamespace(
			indexes={"code": {"name": "code", "fields": ["code"], "unique": True, "kind": "UNIQUE"}}
		)
		conn._txn_read = lambda q, p: ["t:1"]
		with self.assertRaises(E.SurrealDBIntegrityError) as cm:
			conn._precheck_insert("INSERT INTO `tabT` $p /*uq:tabT:p*/", {"p": [{"code": 42}]})
		self.assertEqual(cm.exception.args, (1062, "Duplicate entry '42' for key 'code'"))

	# --- retry hooks (ADR 0002) -----------------------------------------------------------------------------
	@contextmanager
	def _local_db(self, db):
		saved = getattr(frappe.local, "db", None)
		frappe.local.db = db
		try:
			yield db
		finally:
			frappe.local.db = saved

	@staticmethod
	def _req():
		"""A minimal WSGI request: `_exception_response` reads `request.environ`."""
		return SimpleNamespace(environ={})

	@staticmethod
	def _fake_db(units_committed=0, lost=False, rollbacks=None):
		conn = SimpleNamespace(units_committed=units_committed, _lost=lost, metrics={"retries": 0})
		rollbacks = [] if rollbacks is None else rollbacks
		return SimpleNamespace(
			db_type="surrealdb",
			_conn=conn,
			value_cache=SimpleNamespace(clear=lambda: None),
			InternalError=E.SurrealDBError,  # `execute_job` reads `frappe.db.InternalError`
			rollback=lambda chain=False: rollbacks.append(chain),
			commit=lambda chain=False: None,
			_rollbacks=rollbacks,
		)

	def test_retry_request_commit_gates(self):
		deadlock = frappe.QueryDeadlockError(Exception("Transaction conflict: can be retried"))
		timeout = frappe.QueryTimeoutError(Exception("Lock wait timeout exceeded"))

		# the MariaDB request path is never touched
		self.assertFalse(T.retry_request_commit(deadlock, 0))
		with self._local_db(self._fake_db()):
			self.assertFalse(T.retry_request_commit(timeout, 0), "timeouts are logged, not retried")
			self.assertFalse(T.retry_request_commit(deadlock, T.MAX_REQUEST_RETRIES))
			with self._local_db(self._fake_db(units_committed=1)):
				self.assertFalse(
					T.retry_request_commit(deadlock, 0), "an explicit commit disables auto-retry"
				)
			with self._local_db(self._fake_db(lost=True)):
				self.assertFalse(T.retry_request_commit(deadlock, 0), "a lost connection is never retried")

	def test_retry_request_commit_retries_rollback_and_sleeps(self):
		deadlock = frappe.QueryDeadlockError(Exception("conflict"))
		rollbacks = []
		db = self._fake_db(rollbacks=rollbacks)
		with patch.object(T.time, "sleep", lambda s: None), self._local_db(db):
			self.assertTrue(T.retry_request_commit(deadlock, 0))
		self.assertEqual(rollbacks, [True])
		self.assertEqual(db._conn.metrics["retries"], 1)

	def test_retry_job_conflict_gates(self):
		deadlock = frappe.QueryDeadlockError(Exception("conflict"))
		with self._local_db(self._fake_db()):
			self.assertFalse(T.retry_job_conflict(frappe.QueryTimeoutError(Exception("x")), 0))
			self.assertFalse(T.retry_job_conflict(deadlock, T.MAX_JOB_RETRIES))
			self.assertTrue(T.retry_job_conflict(deadlock, 0))
		self.assertFalse(T.retry_job_conflict(deadlock, 0))  # the site's own db is MariaDB here

	def test_reset_request_state_starts_a_clean_attempt(self):
		frappe.local.response["docs"].append("stale")
		frappe.local.message_log.append("stale")
		frappe.db.value_cache["stale"] = 1
		T.reset_request_state()
		self.assertEqual(list(frappe.local.response), ["docs"])
		self.assertEqual(frappe.local.response["docs"], [])
		self.assertEqual(frappe.local.message_log, [])
		self.assertNotIn("stale", frappe.db.value_cache)

	def test_sync_and_retry_returns_the_redispatched_response(self):
		state = {"syncs": 0}

		def sync():
			state["syncs"] += 1
			if state["syncs"] in (1, 4, 5):  # the first sync of each scenario conflicts
				raise frappe.QueryDeadlockError(Exception("conflict"))

		def dispatch(request):
			return "resp-2"

		with patch.multiple(
			frappe.app, sync_database=sync, _dispatch=dispatch,
			_exception_response=lambda e, env: "err-response",
		), patch.object(T.time, "sleep", lambda s: None), self._local_db(self._fake_db()):
			# attempt 0: the commit conflicts -> the request is re-dispatched; attempt 1 commits
			self.assertEqual(frappe.app._sync_and_retry(self._req()), "resp-2")
			self.assertEqual(state["syncs"], 2)
			# no conflict: the caller keeps its own response
			self.assertIsNone(frappe.app._sync_and_retry(self._req()))
			# an explicit commit disables the auto-retry: the error propagates untouched
			with self._local_db(self._fake_db(units_committed=1)):
				with self.assertRaises(frappe.QueryDeadlockError):
					frappe.app._sync_and_retry(self._req())
			self.assertEqual(state["syncs"], 4)

		# a failing re-dispatch is answered like a failed dispatch: response + rollback
		rollbacks = []
		db = self._fake_db(rollbacks=rollbacks)

		def dispatch_fails(request):
			raise frappe.ValidationError("boom")

		with patch.multiple(
			frappe.app, sync_database=sync, _dispatch=dispatch_fails,
			_exception_response=lambda e, env: "err-response",
		), patch.object(T.time, "sleep", lambda s: None), self._local_db(db):
			self.assertEqual(frappe.app._sync_and_retry(self._req()), "err-response")
		# one rollback in the retry hook (the conflicted attempt), one for the failed re-dispatch
		self.assertEqual(rollbacks, [True, True])
		self.assertEqual(state["syncs"], 5, "the failed dispatch is answered without a second sync")

	def test_execute_job_retries_the_whole_job(self):
		from frappe.utils.background_jobs import execute_job

		state = {"calls": 0, "destroyed": 0}

		def job():
			state["calls"] += 1
			if state["calls"] == 1:
				frappe.local.db = self._fake_db()  # this attempt's unit is SurrealDB's and will conflict
				raise frappe.QueryDeadlockError(Exception("conflict"))
			return "ok"

		saved_db = frappe.local.db
		try:
			with patch.object(
				frappe, "destroy", lambda: state.__setitem__("destroyed", state["destroyed"] + 1)
			), patch.object(T.time, "sleep", lambda s: None):
				retval = execute_job(
					site=frappe.local.site,
					method=job,
					event="test_event",
					job_name="test_job",
					kwargs={},
					is_async=False,
					retry=0,
				)
		finally:
			frappe.local.db = saved_db
		self.assertEqual(retval, "ok")
		self.assertEqual(state["calls"], 2)
		self.assertEqual(state["destroyed"], 1)
