"""Driver boundary of the SurrealDB backend (chunk P1.3).

Rules fixed by measurement (docs/ENVIRONMENT.md "Driver findings", `spike/P1.3-driver/error_kinds.py`):

* WebSocket transport only; every call goes through `query_raw` and the status of **every** statement in the
  response is checked in Python (the SDK's `query()` inspects only the first one). No extra round trip.
* Frappe expects an autocommit-off connection (one request = one transaction). `SurrealConnection` therefore opens
  an interactive transaction lazily on the first statement and ends it on `commit`/`rollback`.
* SurrealDB 3.2.4 keeps the write of a statement that failed inside an interactive transaction and would commit
  it. The connection is *tainted* by such a failure and refuses to commit until it is rolled back (fails closed
  until the statement-atomicity emulation of P1.8 exists).
* Values are bound as parameters, never interpolated. Only named (`$name`, dict) parameters exist; MariaDB-style
  positional `%s` parameters are refused. Date/time values are refused too: ADR 0001 stores them as canonical text /
  integers and the *translator*, which knows the column type, encodes them.
* The SDK is imported lazily so that MariaDB sites never need it (it is an optional dependency).

Frappe's `Database.sql()` talks to a DB-API cursor; `SurrealCursor` is the small adapter that lets the upstream
`sql()` machinery (as_dict, pluck, write counting, logging) work unchanged.
"""

import datetime
import decimal
import json
import re
from dataclasses import dataclass
from typing import Any

from frappe.database.surrealdb.errors import (
	CR_SERVER_GONE,
	SurrealDBConnectionError,
	SurrealDBError,
	SurrealDBProgrammingError,
	SurrealDBTransactionTainted,
	classify_exception,
	classify_rpc_error,
	classify_statement_error,
	is_transport_exception,
	unsupported,
)

DEFAULT_NAMESPACE = "frappe"
ADMIN_DATABASE = "_admin"  # bound by root/provisioning connections; never written to
READ_ONLY_STARTS = ("select", "info", "return", "show")
WRITE_STARTS = ("create", "insert", "update", "upsert", "delete", "relate", "define", "remove", "alter")
_FIRST_WORD = re.compile(r"^\s*(\w+)")
# `/*cols:a,b,c*/` anywhere in the query text: the projection order emitted by the translator (see SurrealCursor)
_COLUMNS_HINT = re.compile(r"/\*\s*cols:\s*(\w+(?:\s*,\s*\w+)*)\s*\*/")
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


def last_statement_word(query: str) -> str:
	"""First keyword of the last statement of a script, splitting on `;` outside quotes/backticks."""
	last_start, quote = 0, None
	for i, ch in enumerate(query):
		if quote:
			if ch == quote:
				quote = None
		elif ch in "'\"`":
			quote = ch
		elif ch == ";" and query[i + 1 :].strip():
			last_start = i + 1
	return _first_word(query[last_start:])


def is_read_only(query: str) -> bool:
	return _first_word(query) in READ_ONLY_STARTS and ";" not in query.strip().rstrip(";")


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
		self._tainted = False
		self._lost = False
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
			raise classify_exception(e) from e
		self._client, self._txn, self._tainted, self._lost = client, None, False, False
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
		if self._client is None or self._lost:
			raise SurrealDBConnectionError(
				CR_SERVER_GONE,
				"SurrealDB connection is not open (lost or closed); reconnect before running queries.",
			)

	# --- transactions ----------------------------------------------------------------------------------------
	@property
	def in_transaction(self) -> bool:
		return self._txn is not None

	def begin(self):
		self._require_open()
		if self._txn is not None:
			self.commit()  # MariaDB: START TRANSACTION implicitly commits the current one
		self._txn = self._call(self._client.begin)

	def commit(self):
		if self._txn is None:
			return
		if self._tainted:
			raise SurrealDBTransactionTainted(
				0,
				"A statement failed inside this transaction and SurrealDB would still commit its write. "
				"Roll back instead (statement-level atomicity is implemented in P1.8).",
			)
		txn, self._txn = self._txn, None
		self._call(self._client.commit, txn)

	def rollback(self):
		txn, self._txn, self._tainted = self._txn, None, False
		if txn is not None:
			self._call(self._client.cancel, txn)

	def _call(self, fn, *args, **kwargs):
		try:
			return fn(*args, **kwargs)
		except Exception as e:
			err = classify_exception(e)
			if isinstance(err, SurrealDBConnectionError):
				self._lost, self._txn = True, None
			raise err from e

	# --- statements ------------------------------------------------------------------------------------------
	def execute(self, query: str, params=None) -> list:
		"""Run one query text (one or more statements). Returns the decoded result of each statement.

		Every statement's status is checked; the first failure raises a classified error. Outside a transaction a
		multi-statement script is *not* atomic (SurrealDB behaviour) - callers that need atomicity use a transaction."""
		bound = encode_params(params)
		self._require_open()
		had_transaction = self._txn is not None
		try:
			return self._execute_once(query, bound)
		except SurrealDBConnectionError:
			# Lost the socket. Retry once on a fresh session, but only a read-only query and only if no
			# transaction was open when it started: a retry must never repeat a write or hide lost work.
			if not had_transaction and is_read_only(query):
				self._reopen()
				return self._execute_once(query, bound)
			raise

	def _reopen(self):
		self.close()
		self.open()

	def _execute_once(self, query: str, bound: dict) -> list:
		if self.params.implicit_transaction and self._txn is None:
			self._txn = self._call(self._client.begin)
		try:
			raw = self._client.query_raw(query, bound, txn_id=self._txn)
		except Exception as e:
			err = classify_exception(e)
			if isinstance(err, SurrealDBConnectionError):
				self._lost, self._txn = True, None
			raise err from e
		if raw.get("error"):
			self._taint()
			raise classify_rpc_error(raw["error"])
		results = []
		for statement in raw.get("result") or []:
			if statement.get("status") != "OK":
				self._taint()
				raise classify_statement_error(statement.get("result"))
			results.append(decode_value(statement.get("result")))
		return results

	def _taint(self):
		if self._txn is not None:
			self._tainted = True

	def cursor(self) -> "SurrealCursor":
		return SurrealCursor(self)


class SurrealCursor:
	"""Just enough DB-API for `frappe.database.database.Database.sql()`.

	Column order contract. SurrealDB returns each row as an object whose keys are *sorted alphabetically*, not in
	projection order (measured, spike/P1.3-driver + live test), and it omits fields that are NONE. MariaDB returns
	columns positionally, so the translator (P1.6) states the order with a `/*cols:a,b,c*/` comment. With a hint the
	rows follow it and missing fields become None. Without one, results are ordered alphabetically, which is only
	safe for consumers that read by name (`as_dict`); a multi-column result consumed positionally without a hint
	raises instead of silently scrambling columns (`require_order`, set by `SurrealDBDatabase.sql`).
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
		lowered = text.lower().rstrip(";").strip()
		# Frappe drives transactions with SQL text (Database.begin/commit/rollback/savepoint)
		if lowered.startswith(("start transaction", "begin")):
			return self.connection.begin()
		if lowered in ("commit", "commit and chain"):
			self.connection.commit()
			return self.connection.begin() if lowered.endswith("chain") else None
		if lowered in ("rollback", "rollback and chain"):
			self.connection.rollback()
			return self.connection.begin() if lowered.endswith("chain") else None
		if lowered.startswith(("savepoint", "release savepoint", "rollback to")):
			unsupported("savepoints (statement-level atomicity emulation)", "P1.8")

		hint = _COLUMNS_HINT.search(text)
		columns = [c.strip() for c in hint[1].split(",")] if hint else None
		results = self.connection.execute(text, values)
		last = results[-1] if results else None
		if last_statement_word(text) in WRITE_STARTS:
			# MariaDB returns no result set for writes, only a row count
			self.rowcount = len(last) if isinstance(last, list) else 0
		else:
			self._shape(last, columns)
		return None

	def _shape(self, result, hint=None):
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
		self.description = tuple((name, None, None, None, None, None, None) for name in columns)
		self._rows = tuple(table)
		self.rowcount = len(table)

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
