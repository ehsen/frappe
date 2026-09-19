"""Error model of the SurrealDB backend.

Two jobs:

1. `SurrealDBNotImplementedError` marks dispatch points that exist but are not implemented yet, so nothing can fall
   through into another engine (see the P1.1 inventory).
2. A DB-API-like exception hierarchy plus classifiers that turn what SurrealDB 3.2.4 / SDK 2.0.0 actually return
   (measured in `spike/P1.3-driver/error_kinds.py`) into errors Frappe already understands. Statement errors arrive
   as plain strings, RPC/commit errors carry a `kind`, so both are needed.

The classified errors carry MariaDB-shaped `args` (`(code, message)`) where a MariaDB equivalent exists, e.g.
`(1062, "Duplicate entry 'a' for key 'uq_name'")`, so Frappe code that parses those messages keeps working.
"""

import re

# MariaDB error numbers Frappe's exception predicates use (frappe/database/mariadb/database.py)
ER_ACCESS_DENIED = 1045
ER_PARSE_ERROR = 1064
ER_BAD_FIELD = 1054
ER_NO_SUCH_TABLE = 1146
ER_TABLE_EXISTS = 1050
ER_DUP_FIELDNAME = 1060
ER_DUP_KEYNAME = 1061
ER_DUP_ENTRY = 1062
ER_CANT_DROP_FIELD_OR_KEY = 1091
ER_DEADLOCK = 1213
ER_LOCK_WAIT_TIMEOUT = 1205
ER_DATA_TOO_LONG = 1406
ER_TRUNCATED_WRONG_VALUE = 1366
ER_STATEMENT_TIMEOUT = 1969
ER_CHECK_CONSTRAINT = 4025
CR_SERVER_GONE = 2006


class SurrealDBNotImplementedError(NotImplementedError):
	"""A SurrealDB code path that exists as a dispatch point but is not implemented yet.

	Every `db_type` branch that Frappe reaches for SurrealDB must either do the right thing or raise
	this. It must never fall through into another engine's behaviour (MariaDB/Postgres/SQLite).
	`chunk` names the plan chunk in the docs repo (`docs/DEVELOPMENT_PLAN.md`) that owns the work.
	"""

	def __init__(self, what: str, chunk: str):
		self.what = what
		self.chunk = chunk
		super().__init__(
			f"SurrealDB backend: {what} is not implemented yet (plan chunk {chunk}). "
			"It deliberately does not fall back to another database engine."
		)


def unsupported(what: str, chunk: str):
	raise SurrealDBNotImplementedError(what, chunk)


class SurrealDBError(Exception):
	"""Base of every error raised by the SurrealDB driver. `args == (code, message)` like PyMySQL."""

	def __init__(self, code: int, message: str, *, raw=None):
		super().__init__(code, message)
		self.code = code
		self.message = message
		self.raw = message if raw is None else raw


class SurrealDBProgrammingError(SurrealDBError):
	"""Bad statement: syntax error, unknown table/column, object already exists, unsupported parameters."""


class SurrealDBIntegrityError(SurrealDBError):
	"""Unique / primary key violation."""


class SurrealDBDataError(SurrealDBError):
	"""Value rejected by the schema: wrong type, too long, failed ASSERT."""


class SurrealDBInternalError(SurrealDBError):
	"""Anything the classifier does not recognise. Propagates unchanged; never mistaken for a known class."""


class SurrealDBOperationalError(SurrealDBError):
	"""Connection, authentication, timeout or concurrency problem."""


class SurrealDBConnectionError(SurrealDBOperationalError):
	"""Transport failure. The connection must be re-established; open transaction state is lost."""


class SurrealDBAuthError(SurrealDBOperationalError):
	pass


class SurrealDBTimeoutError(SurrealDBOperationalError):
	pass


class SurrealDBTransactionConflict(SurrealDBOperationalError):
	"""Optimistic commit-time conflict. Retryable by design (ADR 0002); maps to Frappe's QueryDeadlockError."""


class SurrealDBTransactionTainted(SurrealDBOperationalError):
	"""A statement failed inside an interactive transaction. SurrealDB 3.2.4 keeps that statement's write and
	would commit it (measured, P0.4 d), so the driver refuses to commit until the transaction is rolled back.
	Removed by the statement-atomicity emulation of P1.8."""


def _strip_quotes(value: str) -> str:
	value = value.strip()
	for quote in ("`", "'", '"'):  # SurrealDB wraps values in backticks and/or quotes
		if len(value) >= 2 and value[0] == value[-1] == quote:
			value = value[1:-1].strip()
	return value


def _record_key(record: str) -> str:
	"""`table:id` (possibly with backticks / angle brackets around the id) -> the id text."""
	_, _, key = record.partition(":")
	return key.strip("`⟨⟩ ") or record


# (pattern, factory) — patterns are the measured server messages; first match wins.
_STATEMENT_PATTERNS = [
	(
		re.compile(
			r"Database index `(?P<index>[^`]+)` already contains (?P<value>.+?), with record `(?P<record>[^`]+)`",
			re.S,
		),
		lambda m: SurrealDBIntegrityError(
			ER_DUP_ENTRY, f"Duplicate entry '{_strip_quotes(m['value'])}' for key '{m['index']}'"
		),
	),
	(
		re.compile(r"Database record `(?P<record>[^`]+)` already exists"),
		lambda m: SurrealDBIntegrityError(
			ER_DUP_ENTRY, f"Duplicate entry '{_record_key(m['record'])}' for key 'PRIMARY'"
		),
	),
	(
		re.compile(
			r"Found (?P<value>.*) for field `(?P<field>[^`]+)`, with record `[^`]+`, but field must conform to: (?P<cond>.*)",
			re.S,
		),
		lambda m: (
			SurrealDBDataError(ER_DATA_TOO_LONG, f"Data too long for column '{m['field']}' at row 1")
			if "string::len(" in m["cond"] and "<=" in m["cond"]
			else SurrealDBDataError(
				ER_CHECK_CONSTRAINT, f"CONSTRAINT `{m['field']}` failed: {m['cond'].strip()}"
			)
		),
	),
	(
		re.compile(
			r"Couldn't coerce value for field `(?P<field>[^`]+)` of `[^`]+`: Expected `(?P<expected>[^`]+)` but found (?P<value>.*)",
			re.S,
		),
		lambda m: SurrealDBDataError(
			ER_TRUNCATED_WRONG_VALUE,
			f"Incorrect {m['expected']} value: '{_strip_quotes(m['value'])}' for column '{m['field']}' at row 1",
		),
	),
	(
		re.compile(r"Found field '(?P<field>[^']+)', but no such field exists for table '(?P<table>[^']+)'"),
		lambda m: SurrealDBProgrammingError(ER_BAD_FIELD, f"Unknown column '{m['field']}' in 'field list'"),
	),
	(
		re.compile(r"The table '(?P<table>[^']+)' does not exist"),
		lambda m: SurrealDBProgrammingError(ER_NO_SUCH_TABLE, f"Table '{m['table']}' doesn't exist"),
	),
	(
		re.compile(r"The table '(?P<name>[^']+)' already exists"),
		lambda m: SurrealDBProgrammingError(ER_TABLE_EXISTS, f"Table '{m['name']}' already exists"),
	),
	(
		re.compile(r"The field '(?P<name>[^']+)' already exists"),
		lambda m: SurrealDBProgrammingError(ER_DUP_FIELDNAME, f"Duplicate column name '{m['name']}'"),
	),
	(
		re.compile(r"The index '(?P<name>[^']+)' already exists"),
		lambda m: SurrealDBProgrammingError(ER_DUP_KEYNAME, f"Duplicate key name '{m['name']}'"),
	),
	(
		re.compile(r"The (?:index|field) '(?P<name>[^']+)' does not exist"),
		lambda m: SurrealDBProgrammingError(
			ER_CANT_DROP_FIELD_OR_KEY, f"Can't DROP '{m['name']}'; check that column/key exists"
		),
	),
	(
		re.compile(r"exceeded the timeout"),
		lambda m: SurrealDBTimeoutError(
			ER_STATEMENT_TIMEOUT, "Query execution was interrupted (max_statement_time exceeded)"
		),
	),
	(
		re.compile(r"Transaction conflict|Resource busy|This transaction can be retried", re.I),
		lambda m: SurrealDBTransactionConflict(
			ER_DEADLOCK, "Deadlock found when trying to get lock; try restarting transaction"
		),
	),
	(
		re.compile(r"IAM error|Not enough permissions|problem with authentication", re.I),
		lambda m: SurrealDBAuthError(ER_ACCESS_DENIED, "Access denied"),
	),
	(
		re.compile(r"Parse error", re.I),
		lambda m: SurrealDBProgrammingError(ER_PARSE_ERROR, "You have an error in your SQL syntax"),
	),
]


def classify_statement_error(message) -> SurrealDBError:
	"""Classify the `result` string of a statement whose status was not OK."""
	text = message if isinstance(message, str) else str(message)
	for pattern, factory in _STATEMENT_PATTERNS:
		if match := pattern.search(text):
			err = factory(match)
			err.raw = text
			return err
	return SurrealDBInternalError(0, text)


def _details_kind(details) -> str | None:
	if isinstance(details, dict):
		kind = details.get("kind")
		if isinstance(kind, str):
			return kind
	return None


def classify_rpc_error(error: dict) -> SurrealDBError:
	"""Classify the top-level `error` object of a `query_raw` response (`kind`, `code`, `message`, `details`)."""
	message = error.get("message") or str(error)
	kind = error.get("kind")
	if _details_kind(error.get("details")) == "TransactionConflict":
		return SurrealDBTransactionConflict(
			ER_DEADLOCK, "Deadlock found when trying to get lock; try restarting transaction", raw=message
		)
	if kind in ("NotAllowed", "Auth"):
		return SurrealDBAuthError(ER_ACCESS_DENIED, "Access denied", raw=message)
	if kind == "Validation" and "Parse error" in message:
		return SurrealDBProgrammingError(ER_PARSE_ERROR, "You have an error in your SQL syntax", raw=message)
	return classify_statement_error(message)


_TRANSPORT_NAMES = (
	"ConnectionClosed",
	"ConnectionUnavailableError",
	"ConnectionRefusedError",
	"InvalidStatus",
)


def is_transport_exception(exc: BaseException) -> bool:
	if isinstance(exc, ConnectionError | TimeoutError | EOFError | OSError):
		return True
	return any(cls.__name__.startswith(_TRANSPORT_NAMES) for cls in type(exc).__mro__)


def classify_exception(exc: BaseException) -> SurrealDBError:
	"""Classify an exception raised by the SDK / the transport / this driver."""
	if isinstance(exc, SurrealDBError):
		return exc
	if is_transport_exception(exc):
		return SurrealDBConnectionError(CR_SERVER_GONE, f"SurrealDB connection lost: {exc}", raw=str(exc))
	details = getattr(exc, "details", None)
	if _details_kind(details) == "TransactionConflict":
		return SurrealDBTransactionConflict(
			ER_DEADLOCK, "Deadlock found when trying to get lock; try restarting transaction", raw=str(exc)
		)
	if type(exc).__name__ == "NotAllowedError" or _details_kind(details) == "Auth":
		return SurrealDBAuthError(ER_ACCESS_DENIED, "Access denied", raw=str(exc))
	return classify_statement_error(str(exc))
