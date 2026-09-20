"""Transaction layer of the SurrealDB backend: application locks, fences, retry helpers (chunk P1.8, ADR 0002).

SurrealDB 3.2.4 (RocksDB) is optimistic and snapshot-isolation only: writers never wait, a write-write
overlap fails at COMMIT (`Transaction conflict ... can be retried`), reads are not tracked, and there is no
SAVEPOINT (all measured in P0.4). Frappe's MariaDB observable behaviour - blocking `for_update`, NOWAIT,
SKIP LOCKED, statement atomicity, savepoints, lost-update protection - is reproduced here:

* `SurrealConnection` (connection.py) keeps the unit of work alive: lazy begin, a log of successful write
  statements, replay for statement atomicity / savepoints, error classification, conflict cleanup.
* `AppLock` is the pessimistic building block for `for_update`/NOWAIT/SKIP LOCKED: an atomic compare-and-set
  on a `__lock` record in its own autocommit transaction (owner + expiry + jittered poll). Taking a lock also
  bumps a `__fence` record inside the unit of work, so a stale snapshot after waiting for the lock still
  conflicts at commit and the unit is retried (P0.7 prototype, `spike/P0.7-lockmodel/lockmodel.py`).
* `retry_request_commit` / `retry_job_conflict` are the two retry hooks (app.py / background_jobs.py). They
  are SurrealDB-gated: the MariaDB branch keeps its existing control flow, commit placement, retry policy
  and exception behaviour. Auto-retry is allowed only when the unit made no explicit `frappe.db.commit()`
  (the driver counts successful commits on the connection) and the server confirmed a retryable conflict -
  a connection loss during COMMIT is never retried (ambiguous outcome, P0.4 h3).

Mapping: commit/statement conflict -> `frappe.QueryDeadlockError` (1213), lock wait exceeded / NOWAIT ->
`frappe.QueryTimeoutError` (1205), duplicate -> `SurrealDBIntegrityError` (1062).
"""

import random
import time
from typing import TYPE_CHECKING

from frappe.database.surrealdb.errors import (
	ER_DEADLOCK,
	ER_LOCK_WAIT_TIMEOUT,
	SurrealDBTimeoutError,
	SurrealDBTransactionConflict,
)

if TYPE_CHECKING:  # pragma: no cover
	from frappe.database.surrealdb.connection import SurrealConnection

LOCK_TABLE = "__lock"
FENCE_TABLE = "__fence"

# Atomic compare-and-set: grant when free, ours, or expired. RETURN AFTER gives the post-state, so the
# caller sees `owner` and can detect theft of an expired lock. Autocommit: its own transaction.
CAS = (
	"UPSERT $r SET owner = $o, expires = time::micros(time::now()) + $ttl, n = (n ?? 0) + 1 "
	"WHERE owner = NONE OR owner = $o OR expires < time::micros(time::now()) RETURN AFTER"
)
RELEASE = "UPDATE $r SET owner = NONE WHERE owner = $o"
# Fenced write inside the unit of work: write-write conflict at commit if another unit held the lock and
# committed after this unit's snapshot began (P0.4 a1/a6).
FENCE = "UPSERT $r SET ver = (ver ?? 0) + 1"
LOCK_TABLE_STATEMENTS = (
	# `OVERWRITE` on fields (the codebase's idempotent-DEFINE pattern): safe to run concurrently, and
	# `option<string>` for owner because the release sets it to NONE, which a required field would reject
	f"DEFINE TABLE IF NOT EXISTS {LOCK_TABLE} SCHEMAFULL",
	f"DEFINE FIELD OVERWRITE owner ON {LOCK_TABLE} TYPE option<string>",
	f"DEFINE FIELD OVERWRITE expires ON {LOCK_TABLE} TYPE int",
	f"DEFINE FIELD OVERWRITE n ON {LOCK_TABLE} TYPE int",
	f"DEFINE TABLE IF NOT EXISTS {FENCE_TABLE} SCHEMAFULL",
	f"DEFINE FIELD OVERWRITE ver ON {FENCE_TABLE} TYPE int",
)

DEFAULT_LOCK_TTL = 60.0  # seconds a granted lock stays valid without a heartbeat refresh
DEFAULT_LOCK_TIMEOUT = 50.0  # MariaDB `innodb_lock_wait_timeout` in this setup: 1205 after 50 s
POLL_CAP = 0.005  # max sleep between CAS polls while waiting for a lock
MAX_REQUEST_RETRIES = 3  # bounded unit-of-work retries per HTTP request (ADR 0002)
MAX_JOB_RETRIES = 5  # background jobs keep Frappe's existing bound


class LockTimeout(Exception):
	"""The lock was not granted within the timeout (blocking) or was held (NOWAIT). -> QueryTimeoutError."""


class RecordID:
	"""Stand-in with the SDK's class name and shape (`table_name`, `id`), used only when the SDK is
	not installed — the driver keys on the class name, so fake-server runs work without the SDK."""

	def __init__(self, table_name, id):
		self.table_name, self.id = table_name, id


def _record_id_cls():
	"""The SDK's `RecordID` when importable, else the stand-in above. The SDK import stays lazy:
	the backend package must not import the SDK (`test_surrealdb_driver`)."""
	try:
		from surrealdb import RecordID as sdk_record_id
	except ImportError:
		return RecordID
	return sdk_record_id


def lock_record_id(key: str):
	"""`__lock` record id for a lock key. `collation.record_id` maps every character to a valid record id."""
	from frappe.database.surrealdb.collation import record_id

	return _record_id_cls()(LOCK_TABLE, record_id(key))


def fence_record_id(key: str):
	from frappe.database.surrealdb.collation import record_id

	return _record_id_cls()(FENCE_TABLE, record_id(key))


def jittered_backoff(attempt: int) -> float:
	"""Jittered exponential backoff in seconds (mandatory: immediate retry measured 1.5-2x worse, P0.4 b)."""
	return random.random() * min(0.05, 0.001 * (2 ** min(attempt, 6)))


class AppLock:
	"""One held application lock: a `__lock` record compare-and-set in autocommit."""

	def __init__(self, conn: "SurrealConnection", key: str, ttl: float = DEFAULT_LOCK_TTL):
		self.conn = conn
		self.key = key
		self.ttl = int(ttl * 1_000_000)  # microseconds
		self.rid = lock_record_id(key)
		self.owner = None  # set on grant; also serves as the "held" marker
		self.expires_at = 0.0

	def acquire(self, timeout: float) -> float:
		"""Poll the CAS until granted. Returns the wait time in seconds. Raises LockTimeout."""
		from uuid import uuid4

		self.owner = uuid4().hex[:12]
		t0 = time.monotonic()
		delay = 0.0005
		while True:
			try:
				results = self.conn._autocommit_raw(CAS, {"r": self.rid, "o": self.owner, "ttl": self.ttl})
			except SurrealDBTransactionConflict:
				results = None  # the CAS record is contended: treat like a lost poll and try again
			rows = results[-1] if results else []  # the CAS is one statement: its rows are the post-state
			if rows and isinstance(rows[0], dict) and rows[0].get("owner") == self.owner:
				self.expires_at = t0 + self.ttl / 1_000_000
				return time.monotonic() - t0
			if time.monotonic() - t0 >= timeout:
				raise LockTimeout(f"lock {self.key} not granted in {timeout}s")
			time.sleep(random.random() * delay + delay / 2)
			delay = min(delay * 1.6, POLL_CAP)

	def refresh(self) -> bool:
		"""Heartbeat: re-run the CAS as the current owner, which resets the expiry."""
		try:
			results = self.conn._autocommit_raw(CAS, {"r": self.rid, "o": self.owner, "ttl": self.ttl})
			rows = results[-1] if results else []
			return bool(rows and isinstance(rows[0], dict) and rows[0].get("owner") == self.owner)
		except Exception:
			return False

	def release(self):
		"""Best effort: concurrent CAS conflicts on the same record are retried; a broken connection
		propagates (the unit's own error must not be masked, but the caller releases quietly anyway)."""
		while True:
			try:
				self.conn._autocommit_raw(RELEASE, {"r": self.rid, "o": self.owner})
				return
			except SurrealDBTransactionConflict:
				time.sleep(0.0005)


def timeout_error(timeout: float) -> SurrealDBTimeoutError:
	"""The MariaDB shape of error 1205 (`frappe.db.is_timedout` maps it to `frappe.QueryTimeoutError`)."""
	return SurrealDBTimeoutError(
		ER_LOCK_WAIT_TIMEOUT, f"Lock wait timeout exceeded; tried to lock for {timeout} seconds"
	)


def deadlock_error(what: str) -> Exception:
	"""Internal: a retryable conflict message in the MariaDB 1213 shape."""
	from frappe.database.surrealdb.errors import SurrealDBTransactionConflict

	return SurrealDBTransactionConflict(ER_DEADLOCK, what)


def can_auto_retry(conn) -> bool:
	"""ADR 0002 gate: auto-retry only while no explicit `frappe.db.commit()` succeeded in this unit and the
	connection is alive. A connection loss during COMMIT is ambiguous (P0.4 h3) and is never retried."""
	return conn is not None and conn.units_committed == 0 and not conn._lost


def retry_request_commit(e: Exception, attempt: int) -> bool:
	"""Retry hook for `frappe.app` (surrealdb only). Returns True to re-dispatch the request.

	Called when `sync_database()`'s commit raised. The unit was rolled back, so the whole request re-runs
	with fresh data: bounded attempts, jittered backoff, per-request caches reset."""
	import frappe

	db = getattr(frappe.local, "db", None)
	if db is None or db.db_type != "surrealdb" or attempt >= MAX_REQUEST_RETRIES:
		return False
	if not isinstance(e, frappe.QueryDeadlockError):
		return False
	conn = getattr(db, "_conn", None)
	if not can_auto_retry(conn):
		return False
	db.rollback(chain=True)
	reset_request_state()
	time.sleep(jittered_backoff(attempt))
	conn.metrics["retries"] += 1
	return True


def retry_job_conflict(e: Exception, retry: int) -> bool:
	"""Retry hook for `frappe.utils.background_jobs.execute_job` (surrealdb only). Returns True to re-run
	the whole job. Only a deadlock is retried: on MariaDB a QueryTimeoutError in a job is logged and
	raised, never retried (execute_job's 1213/1205 branch only sees raw InternalErrors), and a lock wait
	timeout re-run would just wait again. A commit conflict has no MariaDB counterpart - conflicts surface
	at COMMIT only on SurrealDB - so the unit-of-work retry is ADR 0002's deliberate, gated addition."""
	import frappe

	db = frappe.db
	if db.db_type != "surrealdb" or retry >= MAX_JOB_RETRIES:
		return False
	if not isinstance(e, frappe.QueryDeadlockError):
		return False
	conn = getattr(db, "_conn", None)
	return can_auto_retry(conn)


def reset_request_state():
	"""Per-request state from the rolled-back attempt must not leak into the retry."""
	import frappe

	frappe.local.response = frappe._dict({"docs": []})
	frappe.local.message_log = []
	# A get_cached_doc during the failed attempt may have cached uncommitted data.
	try:
		frappe.cache.delete_keys("document_cache::*")
	except Exception:
		pass
	frappe.db.value_cache.clear()
