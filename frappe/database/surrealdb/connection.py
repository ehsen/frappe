"""Driver boundary of the SurrealDB backend (chunks P1.3, P1.8).

Rules fixed by measurement (docs/ENVIRONMENT.md "Driver findings", `spike/P1.3-driver/error_kinds.py`):

* WebSocket transport only; every call goes through `query_raw` and the status of **every** statement in the
  response is checked in Python (the SDK's `query()` inspects only the first one). No extra round trip.
* Frappe expects an autocommit-off connection (one request = one transaction). `SurrealConnection` therefore
  opens an interactive transaction lazily on the first statement and ends it on `commit`/`rollback`.
* SurrealDB 3.2.4 has no SAVEPOINT and no statement atomicity inside an interactive transaction: a statement
  that fails *after* writing (UNIQUE index, ...) leaves its write behind and would commit it (P0.4 d). The
  connection therefore keeps a **log of successful write statements** and repairs the transaction by replay:
  on any statement error that is not provably pre-write it cancels the transaction, begins a new one and
  re-runs the log without the failed statement (ADR 0002; unique constraints are additionally pre-checked by
  the driver so that duplicates are raised before the write). `savepoint`/`rollback to`/`release savepoint`
  are marks into that log.
* Concurrency is optimistic (snapshot isolation, conflicts at COMMIT). Frappe's blocking `for_update` /
  NOWAIT / SKIP LOCKED are built on application locks (`transactions.AppLock`: CAS on `__lock` records in
  autocommit, plus a fenced `__fence` write inside the unit of work) and the retry hooks in `frappe.app` /
  `frappe.utils.background_jobs` (ADR 0002, P0.7 prototype).
* Values are bound as parameters, never interpolated. The translator and Frappe's qb layer bind named
  (`$name`, dict) parameters; printf-style positional parameters (`%s`, pymysql's convention - used widely
  by upstream frappe code and test_db) bind as `$pN` via `positional_to_named`, with temporal values encoded
  to their canonical form there (raw sql has no column context). *Named* temporal values are still refused:
  ADR 0001 stores them as canonical text / integers and the *translator*, which knows the column type, encodes them.
* The SDK is imported lazily so that MariaDB sites never need it (it is an optional dependency).

Frappe's `Database.sql()` talks to a DB-API cursor; `SurrealCursor` is the small adapter that lets the upstream
`sql()` machinery (as_dict, pluck, write counting, logging) work unchanged.
"""

import datetime
import decimal
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from frappe.database.surrealdb import text_shadows
from frappe.database.surrealdb.errors import (
	CR_SERVER_GONE,
	ER_ACCESS_DENIED,
	ER_READONLY,
	SurrealDBAuthError,
	SurrealDBConnectionError,
	SurrealDBError,
	SurrealDBIntegrityError,
	SurrealDBOperationalError,
	SurrealDBProgrammingError,
	SurrealDBTransactionConflict,
	classify_exception,
	classify_rpc_error,
	classify_statement_error,
)
from frappe.database.surrealdb.values import to_date, to_datetime, to_time_us
from frappe.database.surrealdb.transactions import (
	DEFAULT_LOCK_TIMEOUT,
	DEFAULT_LOCK_TTL,
	FENCE,
	FENCE_TABLE,
	LOCK_TABLE,
	LOCK_TABLE_STATEMENTS,
	AppLock,
	LockTimeout,
	fence_record_id,
)
from frappe.database.surrealdb.transactions import timeout_error as _timeout_error

DEFAULT_NAMESPACE = "frappe"
ADMIN_DATABASE = "_admin"  # bound by root/provisioning connections; never written to
READ_ONLY_STARTS = ("select", "info", "return", "show")
WRITE_STARTS = ("create", "insert", "update", "upsert", "delete", "relate", "define", "remove", "alter")
_FIRST_WORD = re.compile(r"^\s*(\w+)")
# `/*cols:a,b,c*/` anywhere in the query text: the projection order emitted by the translator (see SurrealCursor)
_KINDS_HINT = re.compile(r"/\*\s*kinds:\s*([\w,]*)\s*\*/")
_NAMES_HINT = re.compile(r"/\*\s*names:\s*(\[.*?\])\s*\*/", re.S)
_COLUMNS_HINT = re.compile(r"/\*\s*cols:\s*(\w+(?:\s*,\s*\w+)*)\s*\*/")
# `/*uq:<table>:<param>*/` on a plain INSERT INTO: the driver pre-checks the table's unique indexes before
# writing, so a duplicate raises before any row is written (SurrealDB would leave the write in the txn)
_UQ_HINT = re.compile(r"/\*\s*uq:([\w@]+):(\w+)\s*\*/")
# `/*lock:<mode>:k:<table>:<id>*/` = lock this record before reading (get_value with `name = x`)
_LOCK_KEY_HINT = re.compile(r"/\*\s*lock:([lns]):k:(.+?)\s*\*/")
# `/*lock:<mode>:t:<table>[:<limit>]*/` = the script's first statement yields the ids to lock (general filters)
_LOCK_ROWS_HINT = re.compile(r"/\*\s*lock:([lns]):t:([\w@]+):?(\d*)\s*\*/")
_TEMPORAL = (datetime.datetime, datetime.date, datetime.time, datetime.timedelta)

_sdk_ready = False


class _SNull:
	"""Serialised as a real CBOR `null` (SurrealQL NULL). SDK 2.0.0 encodes Python `None` as NONE, so DEFAULTs would
	fire and NULL could never be sent (measured, P0.3). Frappe's `None` means SQL NULL."""

	def __repr__(self):
		return "SNULL"


SNULL = _SNull()


def load_sdk():
	"""Import the SurrealDB SDK lazily and install the NULL encoder once. Returns the `surrealdb` module."""
	global _sdk_ready
	try:
		import surrealdb
		import surrealdb.data.cbor as sdk_cbor
	except ImportError as e:  # pragma: no cover - depends on the environment
		raise SurrealDBConnectionError(
			2002,
			"The SurrealDB Python SDK is not installed. Install the optional dependency: pip install 'frappe[surrealdb]'",
			raw=str(e),
		) from e
	if not _sdk_ready:
		original = sdk_cbor.default_encoder

		def default_encoder(encoder, obj):
			if isinstance(obj, _SNull):
				encoder.write(b"\xf6")  # CBOR null
				return
			return original(encoder, obj)

		sdk_cbor.default_encoder = default_encoder
		_sdk_ready = True
	return surrealdb


def encode_value(value: Any) -> Any:
	"""Python -> bindable value. None -> NULL; temporal values are refused (see module docstring)."""
	if value is None:
		return SNULL
	if isinstance(value, _TEMPORAL):
		raise SurrealDBProgrammingError(
			0,
			f"Cannot bind a {type(value).__name__} directly: date/time values must be encoded to the column's "
			"canonical form by the query translator (ADR 0001 point 8; chunks P1.4/P1.6).",
		)
	if isinstance(value, list | tuple):
		return [encode_value(v) for v in value]
	if isinstance(value, dict):
		return {k: encode_value(v) for k, v in value.items()}
	return value


def encode_params(params) -> dict:
	if params is None:
		return {}
	if not isinstance(params, dict):
		raise SurrealDBProgrammingError(
			0,
			"SurrealDB queries take named parameters (a dict bound as `$name`); positional `%s` parameters are "
			"MariaDB-only. Render the query through the SurrealDB builder (P1.6) or use multisql (P1.9).",
		)
	return {str(k): encode_value(v) for k, v in params.items()}


_POSITIONAL_PARAM = re.compile(r"%(?:(\((\w+)\))([sd])|([sd])|%%|(%\S)?)")


def _bind_positional(value: Any) -> Any:
	"""Encode a temporal positional parameter to its canonical form (ADR 0001 point 8) - the
	translator does the same for literals. Zero microseconds are trimmed like pymysql's escape."""
	if isinstance(value, datetime.datetime):
		text = to_datetime(value)
		return text[:19] if text.endswith(".000000") else text
	if isinstance(value, datetime.date):
		return to_date(value)
	if isinstance(value, datetime.timedelta):
		return to_time_us(value)
	return value


def positional_to_named(query: str, params) -> tuple[str, Any]:
	"""Bind printf-style positional parameters (`%s`, pymysql's convention - upstream frappe code and
	test_db rely on them) as named `$pN` parameters: every value stays bound, never interpolated into
	the text (P1.3). `%(name)s` takes a dict of values; the `%d` form must convert to an integer;
	anything else printf-like fails closed. A `select <expr>` without FROM - which SurrealQL has no
	meaning for - is rendered as `RETURN <expr>` (MariaDB's `SELECT expr` shape)."""
	if params is None or isinstance(params, dict):
		return _fromless_select(query), params
	if not isinstance(params, (list, tuple)):
		params = (params,)
	if not params:
		return _fromless_select(query), {}
	bound: dict[str, Any] = {}
	index = 0

	def substitute(m: "re.Match") -> str:
		nonlocal index
		if m[0] == "%%":
			return "%"
		if m[1] is not None:
			raise SurrealDBProgrammingError(
				0,
				"Named printf parameters (`%(name)s`) take a dict of values, not a positional "
				"sequence; bind them as a `$name` dict instead.",
			)
		try:
			value = _bind_positional(params[index])
		except IndexError as e_:
			raise SurrealDBProgrammingError(
				0, f"Not enough positional parameters for {query.count('%s')} placeholder(s) in the query."
			) from e_
		if m[4] == "d":
			try:
				value = int(value)
			except (TypeError, ValueError) as e_:
				raise SurrealDBProgrammingError(0, f"`%d` needs an integer, got {value!r}") from e_
		name = f"p{index}"
		bound[name] = value
		index += 1
		return f"${name}"

	text = _POSITIONAL_PARAM.sub(substitute, query)
	if index == 0:
		# printf-formatted text without placeholders and values passed: pymysql fails the same way
		raise SurrealDBProgrammingError(
			0,
			"Positional parameters were passed for a query without `%s` placeholders "
			"(MariaDB's driver rejects this too).",
		)
	return _fromless_select(text), bound


def _fromless_select(query: str) -> str:
	"""`select <expr>` with no FROM (SurrealQL has no such form) renders as `RETURN <expr>`,
	which returns the single value MariaDB's `SELECT expr` row would have."""
	if not isinstance(query, str):
		return query
	m = re.match(r"(?is)(\s*select\b)(.*)$", query)
	if m and not re.search(r"(?i)\bfrom\b", m[2]):
		return "RETURN" + m[2]
	return query


def decode_value(value: Any) -> Any:
	"""SDK value -> what Frappe expects. Decimal -> float (Frappe converts DECIMAL to float on read); the record `id`
	column is dropped (ADR 0001: the driver returns `name`, never a parsed id) and any other record id becomes its
	id text; containers recursively."""
	if isinstance(value, decimal.Decimal):
		return float(value)
	if isinstance(value, list):
		return [decode_value(v) for v in value]
	if isinstance(value, tuple):
		return tuple(decode_value(v) for v in value)
	if isinstance(value, dict):
		return {
			k: decode_value(v)
			for k, v in value.items()
			if not (k == "id" and type(v).__name__ == "RecordID")  # ADR 0001: `name` is a normal field
		}
	if type(value).__name__ == "RecordID" and hasattr(value, "id"):
		return decode_value(value.id)
	return value


def _first_word(query: str) -> str:
	match = _FIRST_WORD.match(query)
	return match[1].lower() if match else ""


def split_statements(query: str) -> list[str]:
	"""Split a script on `;` outside quotes/backticks, dropping empty tails."""
	out, start, quote = [], 0, None
	for i, ch in enumerate(query):
		if quote:
			if ch == quote:
				quote = None
		elif ch in "'\"`":
			quote = ch
		elif ch == ";":
			if query[start:i].strip():
				out.append(query[start:i].strip())
			start = i + 1
	if query[start:].strip():
		out.append(query[start:].strip())
	return out


def last_statement_word(query: str) -> str:
	"""First keyword of the last statement of a script, splitting on `;` outside quotes/backticks."""
	statements = split_statements(query)
	return _first_word(statements[-1]) if statements else ""


def is_read_only(query: str) -> bool:
	return _first_word(query) in READ_ONLY_STARTS and ";" not in query.strip().rstrip(";")


def has_write_statement(query: str) -> bool:
	"""True if any statement of the script is a write (the unit-of-work log only records write scripts)."""
	return any(_first_word(s) in WRITE_STARTS for s in split_statements(query))


@dataclass
class ConnectionParams:
	url: str
	namespace: str = DEFAULT_NAMESPACE
	database: str | None = None
	username: str | None = None
	password: str | None = None
	level: str = "database"  # root | namespace | database (the level the user is defined at)
	implicit_transaction: bool = True


class SurrealConnection:
	"""One authenticated WebSocket session bound to a namespace/database. Not thread-safe (Frappe uses one per worker)."""

	def __init__(self, params: ConnectionParams, client_factory=None):
		self.params = params
		self._client_factory = client_factory
		self._client = None
		self._txn = None
		self._lost = False
		# MariaDB's `START TRANSACTION READ ONLY`: writes are rejected with ER_READONLY until
		# commit/rollback; `SurrealDBExceptionUtil.is_read_only_mode_error` classifies the error.
		self._read_only = False
		# Serialise every websocket RPC: the SDK's blocking session matches responses
		# by request id and breaks ("Response ID mismatch") when two calls overlap.
		self._rpc_lock = threading.Lock()
		# Unit of work (ADR 0002): successful write statements for replay / savepoints, held app locks,
		# the auto-retry gate and metrics.
		self._log: list[tuple[str, dict]] = []  # successful write statements (whole script, bound params)
		self._marks: dict[str, int] = {}  # savepoint name -> log length
		self._locks: list[AppLock] = []
		self.units_committed = 0  # successful commits on this connection: > 0 disables the auto-retry hook
		self._lock_tables_ready = False
		self._last_beat = 0.0
		self.metrics = {
			"conflicts": 0,
			"retries": 0,
			"replays": 0,
			"replayed_stmts": 0,
			"lock_waits": 0,
			"lock_wait_ms": 0.0,
			"lock_lost": 0,
		}
		self.statement_timeout: int | None = (
			None  # seconds; rendered as a TIMEOUT clause by the translator (P1.6)
		)

	# --- lifecycle -------------------------------------------------------------------------------------------
	def open(self) -> "SurrealConnection":
		p = self.params
		factory = self._client_factory or load_sdk().Surreal
		client = factory(p.url)
		try:
			creds = {"username": p.username, "password": p.password}
			if p.level in ("namespace", "database"):
				creds["namespace"] = p.namespace
			if p.level == "database":
				creds["database"] = p.database
			client.signin(creds)
			client.use(p.namespace, p.database or ADMIN_DATABASE)
		except Exception as e:
			self._close_quietly(client)
			err = classify_exception(e)
			if isinstance(err, SurrealDBAuthError):
				# MariaDB-shaped connect failure: frappe/tests/test_db.py parses `Access denied for ...`
				# and the database name out of the message.
				host = urlsplit(p.url).hostname or p.url
				err = SurrealDBAuthError(
					ER_ACCESS_DENIED,
					f"Access denied for user '{p.username}' at '{host}'; database \"{p.database}\"",
				)
			raise err from e
		self._client = client
		self._txn = None
		self._lost = False
		self._reset_unit()
		text_shadows.freeze()  # the allow-list is closed once a driver connection exists (P1.15)
		return self

	def select_db(self, database: str):
		self._require_open()
		self._client.use(self.params.namespace, database)
		self.params.database = database

	def close(self):
		client, self._client, self._txn = self._client, None, None
		if client is not None:
			self._close_quietly(client)

	@staticmethod
	def _close_quietly(client):
		try:
			client.close()
		except Exception:  # closing a broken socket must not mask the real error
			pass

	def _require_open(self):
		if self._client is not None and not self._lost:
			return
		if self._client is not None and self._lost:
			# mysqlclient `auto_reconnect=True` parity (frappe/database/mariadb/database.py): a session
			# lost BETWEEN units of work (idle server-side close during a long non-DB stretch, lost
			# socket) is transparently re-established on the next statement, like MariaDB. The lost
			# session had no transaction (_mark_lost cleared it) and no unit state, so a fresh session
			# cannot repeat or hide work. A connection that failed mid-statement keeps the existing
			# in-flight semantics (the failing statement is not retried; the NEXT one lands here).
			self._reopen()
			return
		raise SurrealDBConnectionError(
			CR_SERVER_GONE,
			"SurrealDB connection is not open (lost or closed); reconnect before running queries.",
		)

	def _mark_lost(self):
		"""The session is gone: server-side transaction and unit state are dead."""
		self._lost, self._txn = True, None
		self._reset_unit()

	def _reset_unit(self):
		"""Forget everything about the current unit of work (its transaction is gone)."""
		self._log = []
		self._marks = {}
		self._locks = []

	# --- transactions ----------------------------------------------------------------------------------------
	@property
	def in_transaction(self) -> bool:
		return self._txn is not None

	def _ensure_txn(self):
		if self._txn is None:
			self._txn = self._call(self._client.begin)
		return self._txn

	def _cancel_txn(self):
		"""Best-effort cancel of an open transaction (used for discard/replay; errors are swallowed)."""
		txn, self._txn = self._txn, None
		if txn is not None:
			try:
				self._client.cancel(txn)
			except Exception:
				pass

	def begin(self):
		self._require_open()
		if self._txn is not None:
			self.commit()  # MariaDB: START TRANSACTION implicitly commits the current one
		# No server round trip: MariaDB's plain START TRANSACTION takes no snapshot - the read view is
		# fixed by the first real statement, not by BEGIN. SurrealDB's begin would pin the snapshot here,
		# and frappe.db.commit() always trails a begin(), so every later read in the unit would see data
		# as of the commit instead of the first read (measured live, P1.8 b/c: the counter stayed at its
		# seed value). Lazy: the first statement opens the transaction via _ensure_txn.

	def commit(self):
		"""Commit the unit of work. On an optimistic conflict the transaction is already rolled back on the
		server: release the locks, forget the unit and raise (the retry hooks re-run the unit, ADR 0002)."""
		self._read_only = False  # the flag ends with the unit even when no transaction was opened (lazy begin)
		if self._txn is None:
			return
		self._read_only = False
		txn, self._txn = self._txn, None
		try:
			self._call(self._client.commit, txn)
		except SurrealDBError as e:
			if isinstance(e, SurrealDBTransactionConflict):
				self.metrics["conflicts"] += 1
			self._release_locks_quietly()
			self._reset_unit()
			raise
		self.units_committed += 1
		self._release_locks_quietly()
		self._reset_unit()

	def rollback(self):
		self._read_only = False
		txn, self._txn = self._txn, None
		if txn is not None:
			self._call(self._client.cancel, txn)
		self._release_locks_quietly()
		self._reset_unit()

	def set_read_only_mode(self, flag: bool):
		"""Track MariaDB's `START TRANSACTION READ ONLY`: write statements are rejected with an
		ER_READONLY error until the next commit/rollback (which clear the flag again)."""
		self._read_only = bool(flag)

	# --- savepoints (statement-log marks; SurrealDB has no SAVEPOINT syntax) -----------------------------------
	def savepoint(self, name: str):
		self._ensure_txn()
		self._marks[name] = len(self._log)

	def rollback_to(self, name: str):
		if name not in self._marks:
			raise SurrealDBProgrammingError(1305, f"SAVEPOINT {name} does not exist")
		idx = self._marks[name]
		for later in [n for n, i in self._marks.items() if i > idx]:
			del self._marks[later]  # MariaDB: ROLLBACK TO releases savepoints taken after it
		if len(self._log) != idx:
			self._replay(idx)

	def release_savepoint(self, name: str):
		if name not in self._marks:
			raise SurrealDBProgrammingError(1305, f"SAVEPOINT {name} does not exist")
		del self._marks[name]

	def _replay(self, upto: int):
		"""Statement atomicity / savepoint emulation: cancel the transaction, begin anew and re-run the first
		`upto` logged write statements (measured 6/72/301 ms for 10/100/500 statements, P0.7)."""
		keep = self._log[:upto]
		marks = {name: idx for name, idx in self._marks.items() if idx <= upto}
		self._cancel_txn()
		try:
			self._txn = self._call(self._client.begin)
			for sql, bound in keep:
				self._raw(sql, bound, self._txn)
		except SurrealDBError as e:
			self._txn, self._log, self._marks = None, [], {}
			raise SurrealDBTransactionConflict(
				0, f"Replay diverged after a failed statement; the unit of work cannot continue: {e}"
			) from e
		self._log, self._marks = keep, marks
		self.metrics["replays"] += 1
		self.metrics["replayed_stmts"] += len(keep)

	# --- raw execution ----------------------------------------------------------------------------------------
	def _call(self, fn, *args, **kwargs):
		with self._rpc_lock:
			try:
				return fn(*args, **kwargs)
			except Exception as e:
				err = classify_exception(e)
				if isinstance(err, SurrealDBConnectionError):
					self._mark_lost()
				raise err from e

	def _raw(self, query: str, bound: dict, txn_id) -> list:
		"""One query text over the wire: RPC errors and every statement's status are classified."""
		with self._rpc_lock:
			try:
				raw = self._client.query_raw(query, bound, txn_id=txn_id)
			except Exception as e:
				err = classify_exception(e)
				if isinstance(err, SurrealDBConnectionError):
					self._mark_lost()
				raise err from e
			if raw.get("error"):
				raise classify_rpc_error(raw["error"])
			results = []
			for statement in raw.get("result") or []:
				if statement.get("status") != "OK":
					raise classify_statement_error(statement.get("result"))
				results.append(decode_value(statement.get("result")))
			return results

	def _autocommit_raw(self, query: str, params=None) -> list:
		"""One statement in autocommit (its own transaction), never touching the unit of work."""
		return self._raw(query, encode_params(params), None)

	def execute(self, query: str, params=None) -> list:
		"""Run one query text (one or more statements) as part of the current unit of work.

		Every statement's status is checked; the first failure raises a classified error. Statement atomicity
		is restored by replaying the write log when a failure was not provably pre-write (module docstring).
		Outside a transaction a multi-statement script is *not* atomic (SurrealDB behaviour) - callers that
		need atomicity use a transaction."""
		query, params = positional_to_named(query, params)
		bound = encode_params(params)
		self._require_open()
		self._maybe_heartbeat()
		self._precheck_insert(query, bound)  # raises before any write; never replayed
		had_transaction = self._txn is not None
		try:
			results = self._execute_once(query, bound)
		except SurrealDBConnectionError:
			# Lost the socket. Retry once on a fresh session, but only a read-only query and only if no
			# transaction was open when it started: a retry must never repeat a write or hide lost work.
			if not had_transaction and is_read_only(query):
				self._reopen()
				return self._execute_once(query, bound)
			raise
		except SurrealDBError as e:
			self._recover_statement_error(e)
			raise
		if self._txn is not None and has_write_statement(query):
			self._log.append((query, bound))
		return results

	def _execute_once(self, query: str, bound: dict) -> list:
		if self.params.implicit_transaction and self._txn is None:
			self._txn = self._call(self._client.begin)
		return self._raw(query, bound, self._txn)

	def _recover_statement_error(self, e: SurrealDBError):
		"""Repair the unit after a failed statement (module docstring)."""
		if self._txn is None:
			return  # autocommit statements are atomic on their own
		if isinstance(e, SurrealDBTransactionConflict):
			# MariaDB: a deadlock (1213) aborts the whole transaction
			self.metrics["conflicts"] += 1
			self._cancel_txn()
			self._release_locks_quietly()
			self._reset_unit()
			return
		if not self._failed_pre_write(e):
			# The statement wrote and then failed: undo everything since the last savepoint boundary
			self._replay(len(self._log))
		# provably pre-write failures leave nothing behind

	@staticmethod
	def _failed_pre_write(e: SurrealDBError) -> bool:
		"""Errors measured to fire *before* any write (P0.4 d2b) need no replay."""
		if isinstance(e, SurrealDBProgrammingError):
			return True  # parse errors, unknown table/field/index
		m = str(e.raw).lower()
		if "already exists" in m and "index" not in m:
			return True  # duplicate record id
		return "couldn't coerce" in m or ("found" in m and "for field" in m)  # coercion / ASSERT

	def _reopen(self):
		self.close()
		self.open()

	# --- unique pre-check --------------------------------------------------------------------------------------
	def _precheck_insert(self, query: str, bound: dict):
		"""`/*uq:<table>:<param>*/` on a plain INSERT INTO: check the table's unique indexes against the rows
		about to be written (+1 indexed read per insert), so a duplicate raises *before* the write (ADR 0002;
		SurrealDB would leave the failed statement's write in the transaction, P0.4 d2b). Race-safe: the real
		index still enforces at write time and the replay then undoes the failed statement."""
		hint = _UQ_HINT.search(query)
		if hint is None:
			return
		table, param = hint[1], hint[2]
		rows = bound.get(param)
		if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
			return
		schema = self._schema_loader(table)
		unique = [
			ix
			for ix in schema.indexes.values()
			if ix["unique"]
			and not any(_index_base_field(f) == "name" for f in ix["fields"])  # `name` is the record id
		]
		if not unique:
			return
		for ix in unique:
			for row in rows:
				values = {f"u{i}": row.get(f) for i, f in enumerate(ix["fields"])}
				if any(v is None or v is SNULL for v in values.values()):
					continue  # MariaDB: NULL never violates UNIQUE (bound params arrive encoded as SNULL)
				where = " AND ".join(quote_field(f, f"$u{i}") for i, f in enumerate(ix["fields"]))
				found = self._txn_read(f"SELECT VALUE id FROM {_quote_table(table)} WHERE {where} LIMIT 1", values)
				if found:
					display = "-".join(str(v) for v in values.values())
					raise SurrealDBIntegrityError(
						1062, f"Duplicate entry '{display}' for key '{ix['name']}'"
					)

	def _schema_loader(self, table: str):
		"""Hook for tests; the real loader reads `INFO FOR TABLE` (cached per request)."""
		from frappe.database.surrealdb.schema import table_schema

		return table_schema(table)

	def _txn_read(self, query: str, params: dict) -> list:
		"""A read inside the current transaction (sees committed data plus this unit's own writes)."""
		self._ensure_txn()
		results = self._raw(query, encode_params(params), self._txn)
		return results[-1] if results else []  # the rows of the (single) statement, not the wrapper

	# --- application locks (ADR 0002 / P0.7) ---------------------------------------------------------------------
	def _ensure_lock_tables(self):
		if self._lock_tables_ready:
			return
		try:
			self._autocommit_raw("; ".join(LOCK_TABLE_STATEMENTS))
		except SurrealDBError:
			# a concurrent connection may have created them between the check and the DEFINE
			info = self._autocommit_raw(f"INFO FOR TABLE {LOCK_TABLE}; INFO FOR TABLE {FENCE_TABLE}")
			if not (info and all(info)):
				raise
		self._lock_tables_ready = True

	def lock(self, key: str, timeout: float = DEFAULT_LOCK_TIMEOUT, ttl: float = DEFAULT_LOCK_TTL) -> bool:
		"""Blocking application lock for `for_update`. timeout=0 -> NOWAIT. Raises LockTimeout (-> 1205).

		After the grant: a transaction without writes is dropped for a fresh snapshot (its reads must see the
		previous holder's commit, P0.4 a1), then the fenced write makes a stale snapshot conflict at commit."""
		for held in self._locks:
			if held.key == key:
				return False
		self._maybe_heartbeat()  # a long wait for this lock must not expire the ones already held
		self._ensure_lock_tables()
		app_lock = AppLock(self, key, ttl)
		waited = app_lock.acquire(timeout)  # LockTimeout propagates to the caller
		self._locks.append(app_lock)
		self._last_beat = time.monotonic()  # the grant refreshed the expiry; beat from here
		if waited > 0:
			self.metrics["lock_waits"] += 1
			self.metrics["lock_wait_ms"] += waited * 1000
		if self._txn is not None and not self._log:
			self._cancel_txn()  # fresh snapshot
		self._txn_write(FENCE, {"r": fence_record_id(key)})
		return True

	def try_lock(self, key: str, ttl: float = DEFAULT_LOCK_TTL) -> bool:
		"""SKIP LOCKED building block: returns False instead of waiting."""
		try:
			return self.lock(key, timeout=0.0, ttl=ttl)
		except LockTimeout:
			return False

	def holds(self, key: str) -> bool:
		"""Whether this connection already holds the application lock for `key` (its own unit)."""
		return any(held.key == key for held in self._locks)

	def _txn_write(self, sql: str, params: dict):
		"""A driver-issued write inside the unit of work, logged for replay like any other write."""
		self._ensure_txn()
		self._raw(sql, encode_params(params), self._txn)
		self._log.append((sql, encode_params(params)))

	def _release_locks_quietly(self):
		locks, self._locks = self._locks, []
		for app_lock in reversed(locks):
			try:
				app_lock.release()
			except Exception:
				pass  # an expired/stolen lock is released by its TTL; never mask the unit's outcome

	def _maybe_heartbeat(self):
		"""Refresh the expiry of held locks while the unit runs (long units must not lose their locks)."""
		if not self._locks:
			return
		now = time.monotonic()
		ttl = max(lk.ttl for lk in self._locks) / 1_000_000
		if now - self._last_beat < max(ttl / 3, 1.0):
			return
		self._last_beat = now
		for app_lock in self._locks:
			if not app_lock.refresh():
				self.metrics["lock_lost"] += 1

	def cursor(self) -> "SurrealCursor":
		return SurrealCursor(self)


def _index_base_field(stored: str) -> str:
	"""`code@ci` -> `code` (the column an index field belongs to)."""
	from frappe.database.surrealdb.schema import base_field

	return base_field(stored)


def _quote_table(table: str) -> str:
	from frappe.database.surrealdb.schema import quote_table

	return quote_table(table)


def quote_field(stored: str, placeholder: str) -> str:
	"""`code@ci` -> `` `code@ci` = $u0 `` (shadow fields compare with the exact unique-index semantics)."""
	from frappe.database.surrealdb.schema import quote

	return f"{quote(stored)} = {placeholder}"


class SurrealCursor:
	"""Just enough DB-API for `frappe.database.database.Database.sql()`.

	Column order contract. SurrealDB returns each row as an object whose keys are *sorted alphabetically*, not in
	projection order (measured, spike/P1.3-driver + live test), and it omits fields that are NONE. MariaDB returns
	columns positionally, so the translator (P1.6) states the order with a `/*cols:a,b,c*/` comment. With a hint the
	rows follow it and missing fields become None. Without one, results are ordered alphabetically, which is only
	safe for consumers that read by name (`as_dict`); a multi-column result consumed positionally without a hint
	raises instead of silently scrambling columns (`require_order`, set by `SurrealDBDatabase.sql`).

	Lock contract (P1.8). `/*lock:<mode>:k:<key>*/` locks one record before reading (blocking `l`, NOWAIT `n`).
	`/*lock:<mode>:t:<table>*/` marks a two-statement script whose first statement yields the ids to lock; the
	rows are re-read after the grants so the caller sees fresh values, and SKIP LOCKED (`s`) drops the locked
	rows from the result instead.
	"""

	require_order = True

	arraysize = 1

	def __init__(self, connection: SurrealConnection):
		self.connection = connection
		self.description = None
		self.rowcount = -1
		self.lastrowid = None
		self._rows: tuple = ()
		self._pos = 0

	def close(self):
		self._rows = ()

	def execute(self, query: str, values=None):
		self.description, self.rowcount, self._rows, self._pos = None, -1, (), 0
		text = query.strip()
		# Database.sql appends `/* FRAPPE_TRACE_ID: ... */`; strip block comments so the
		# transaction-statement interception still matches (see patch_txn_comment.py, v2).
		# `plain` keeps case (savepoint names are case-sensitive marks); `lowered` only drives matching.
		plain = re.sub(r"/\*.*?\*/", " ", text, flags=re.S).rstrip(";").strip()
		lowered = plain.lower()
		# Frappe drives transactions with SQL text (Database.begin/commit/rollback/savepoint)
		if lowered.startswith(("start transaction", "begin")):
			# begin() first (it may implicitly commit a stale unit, which resets the read-only flag),
			# then record the mode of the *new* unit
			self.connection.begin()
			self.connection.set_read_only_mode("read only" in lowered)
			return None
		if lowered in ("commit", "commit and chain"):
			self.connection.commit()
			return self.connection.begin() if lowered.endswith("chain") else None
		if lowered in ("rollback", "rollback and chain"):
			self.connection.rollback()
			return self.connection.begin() if lowered.endswith("chain") else None
		if lowered.startswith("savepoint"):
			self.connection.savepoint(plain[len("savepoint") :].strip())
			return None
		if lowered.startswith("release savepoint"):
			self.connection.release_savepoint(plain[len("release savepoint") :].strip())
			return None
		if lowered.startswith("rollback to"):
			name = plain[len("rollback to") :].strip()
			if name.lower().startswith("savepoint"):
				name = name[len("savepoint") :].strip()
			self.connection.rollback_to(name)
			return None

		if self.connection._read_only and last_statement_word(text) in WRITE_STARTS:
			# MariaDB rejects writes inside a READ ONLY transaction (ER 1792); the base Database
			# classifies this error and raises frappe.InReadOnlyMode.
			raise SurrealDBOperationalError(
				ER_READONLY,
				f"Cannot execute statement in a READ ONLY transaction: {text[:160]}",
			)
		hint = _COLUMNS_HINT.search(text)
		columns = [c.strip() for c in hint[1].split(",")] if hint else None
		kinds_hint = _KINDS_HINT.search(text)
		kinds = kinds_hint[1].split(",") if kinds_hint else None
		names_hint = _NAMES_HINT.search(text)
		names = json.loads(names_hint[1].replace("\\/", "/")) if names_hint else None

		lock_key, lock_rows = _LOCK_KEY_HINT.search(text), _LOCK_ROWS_HINT.search(text)
		if lock_key is not None and lock_rows is None:
			if lock_key[1] == "s" and not (
				self.connection.try_lock(lock_key[2]) or self.connection.holds(lock_key[2])
			):
				# MariaDB's SKIP LOCKED on a single record: it is locked by another transaction, so
				# the read yields no rows at all (no wait, no 1205 error - the record is skipped).
				results = []
			else:
				# lock the record before reading it: the read then sees the previous holder's commit
				self._acquire_lock(lock_key[1], lock_key[2])
				results = self.connection.execute(text, values)
		elif lock_rows is not None:
			# two-statement script: [ids to lock, the query itself]; re-read after the grants
			mode, table = lock_rows[1], lock_rows[2]
			results = self.connection.execute(text, values)
			granted = self._lock_rows(mode, table, results[0], lock_rows[3] or None)
			results = self.connection.execute(text, values)
			rows = results[-1] if results else None
			if mode == "s" and isinstance(rows, list):
				# SKIP LOCKED: the result keeps only the rows this connection claimed
				keep = {f"{table}:{rid}" for rid in granted}
				results[-1] = [row for row in rows if f"{table}:{row.get('__lk0')}" in keep]
		else:
			results = self.connection.execute(text, values)

		last = results[-1] if results else None
		if last_statement_word(text) in WRITE_STARTS:
			# MariaDB returns no result set for writes, only a row count
			self.rowcount = len(last) if isinstance(last, list) else 0
		else:
			self._shape(last, columns, names)
			if kinds:
				self._decode(kinds)
		return None

	def _acquire_lock(self, mode: str, key: str):
		"""Blocking (`l`) or NOWAIT (`n`) single-record lock. Errors map to MariaDB's 1205 shape."""
		try:
			self.connection.lock(key, timeout=0.0 if mode == "n" else DEFAULT_LOCK_TIMEOUT)
		except LockTimeout as e:
			raise _timeout_error(0.0 if mode == "n" else DEFAULT_LOCK_TIMEOUT) from e

	def _lock_rows(self, mode: str, table: str, ids, want=None) -> list:
		"""Grant the row locks (blocking / NOWAIT), or claim the unlocked ones for `s` — at most `want` of
		them (the query's LIMIT), in candidate order. Returns the granted record ids."""
		timeout = 0.0 if mode == "n" else DEFAULT_LOCK_TIMEOUT
		want = int(want) if want else None
		granted = []
		try:
			for row in ids or []:
				record_id = row.get("__lk0") if isinstance(row, dict) else row
				if record_id is None:
					continue
				if want is not None and len(granted) >= want:
					break
				if mode == "s":
					# a row this unit already holds is ours, not a foreign one: MariaDB's SKIP LOCKED
					# does not skip a transaction's own locks
					if self.connection.try_lock(f"{table}:{record_id}") or self.connection.holds(
						f"{table}:{record_id}"
					):
						granted.append(record_id)
				else:
					self.connection.lock(f"{table}:{record_id}", timeout=timeout)
					granted.append(record_id)
		except LockTimeout as e:
			raise _timeout_error(timeout) from e
		return granted

	def _shape(self, result, hint=None, names=None):
		"""Turn a statement result into DB-API columns/rows. Rows are dicts (`SELECT`) or scalars (`SELECT VALUE`)."""
		if result is None:
			result = []
		if isinstance(result, dict):
			rows = [result]
		elif isinstance(result, list):
			rows = result
		else:
			rows = [result]
		seen = {key for row in rows if isinstance(row, dict) for key in row}
		columns = list(hint) if hint else sorted(seen)
		if len(columns) > 1 and not hint and self.require_order:
			raise SurrealDBProgrammingError(
				0,
				f"A multi-column result ({', '.join(columns)}) is read by position but carries no column order: "
				"SurrealDB objects are sorted alphabetically. The translator must add a /*cols:a,b*/ hint (P1.6).",
			)
		if columns:
			table = [tuple(row.get(c) if isinstance(row, dict) else None for c in columns) for row in rows]
		else:
			columns = ["value"]
			table = [(row,) for row in rows]
		self.description = tuple((name, None, None, None, None, None, None) for name in (names or columns))
		self._rows = tuple(table)
		self.rowcount = len(table)

	def _decode(self, kinds: list[str]):
		"""Stored date/datetime/time values -> the objects MariaDB's driver returns (the `/*kinds:...*/` hint names each column)."""
		from frappe.database.surrealdb.schema import decode_kind

		convert = [i for i, k in enumerate(kinds) if k in ("date", "datetime", "time")]
		if not convert:
			return
		self._rows = tuple(
			tuple(decode_kind(kinds[i], v) if i in convert else v for i, v in enumerate(row))
			for row in self._rows
		)

	def fetchall(self):
		rows, self._pos = self._rows[self._pos :], len(self._rows)
		return rows

	def fetchmany(self, size=None):
		size = size or self.arraysize
		rows = self._rows[self._pos : self._pos + size]
		self._pos += len(rows)
		return rows

	def fetchone(self):
		rows = self.fetchmany(1)
		return rows[0] if rows else None

	def mogrify(self, query, values=None):
		"""Query text plus its bound values, for logging only (never executed)."""
		if not values:
			return query
		return f"{query} /* params: {json.dumps(values, default=str, ensure_ascii=False)} */"
