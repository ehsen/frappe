import frappe
from frappe.database.database import Database
from frappe.database.surrealdb.connection import (
	DEFAULT_NAMESPACE,
	ConnectionParams,
	SurrealConnection,
)
from frappe.database.surrealdb.errors import (
	ER_ACCESS_DENIED,
	ER_BAD_FIELD,
	ER_CANT_DROP_FIELD_OR_KEY,
	ER_DATA_TOO_LONG,
	ER_DUP_ENTRY,
	ER_DUP_FIELDNAME,
	ER_LOCK_WAIT_TIMEOUT,
	ER_NO_SUCH_TABLE,
	ER_PARSE_ERROR,
	ER_STATEMENT_TIMEOUT,
	SurrealDBConnectionError,
	SurrealDBDataError,
	SurrealDBError,
	SurrealDBOperationalError,
	SurrealDBProgrammingError,
	SurrealDBTransactionConflict,
	unsupported,
)


def _code(e) -> int | None:
	return e.code if isinstance(e, SurrealDBError) else None


class SurrealDBExceptionUtil:
	"""`is_*` predicates used by `Database` to classify driver errors. They answer from the classified error
	types/codes of `errors.py`; an error that was not classified is never mistaken for a known class."""

	ProgrammingError = SurrealDBProgrammingError
	TableMissingError = SurrealDBProgrammingError
	OperationalError = SurrealDBOperationalError
	InternalError = SurrealDBError
	SQLError = SurrealDBError
	DataError = SurrealDBDataError

	@staticmethod
	def is_deadlocked(e) -> bool:
		return isinstance(e, SurrealDBTransactionConflict)

	@staticmethod
	def is_timedout(e) -> bool:
		return _code(e) == ER_LOCK_WAIT_TIMEOUT

	@staticmethod
	def is_read_only_mode_error(e) -> bool:
		return False

	@staticmethod
	def is_table_missing(e) -> bool:
		return _code(e) == ER_NO_SUCH_TABLE

	@staticmethod
	def is_missing_column(e) -> bool:
		return _code(e) == ER_BAD_FIELD

	@staticmethod
	def is_duplicate_fieldname(e) -> bool:
		return _code(e) == ER_DUP_FIELDNAME

	@staticmethod
	def is_duplicate_entry(e) -> bool:
		return _code(e) == ER_DUP_ENTRY

	@staticmethod
	def is_access_denied(e) -> bool:
		return _code(e) == ER_ACCESS_DENIED

	@staticmethod
	def cant_drop_field_or_key(e) -> bool:
		return _code(e) == ER_CANT_DROP_FIELD_OR_KEY

	@staticmethod
	def is_syntax_error(e) -> bool:
		return _code(e) == ER_PARSE_ERROR

	@staticmethod
	def is_statement_timeout(e) -> bool:
		return _code(e) == ER_STATEMENT_TIMEOUT

	@staticmethod
	def is_data_too_long(e) -> bool:
		return _code(e) == ER_DATA_TOO_LONG

	@staticmethod
	def is_db_table_size_limit(e) -> bool:
		return False

	@staticmethod
	def is_primary_key_violation(e) -> bool:
		return _code(e) == ER_DUP_ENTRY and "for key 'PRIMARY'" in str(e.message)

	@staticmethod
	def is_unique_key_violation(e) -> bool:
		return _code(e) == ER_DUP_ENTRY and "for key 'PRIMARY'" not in str(e.message)

	@staticmethod
	def is_interface_error(e) -> bool:
		return isinstance(e, SurrealDBConnectionError)

	@staticmethod
	def is_nested_transaction_error(e) -> bool:
		return False


class _UnmappedTypeMap(dict):
	"""`Database.type_map` placeholder: lookups raise instead of silently defaulting (`type_map.get(ft, ("varchar",))`).

	Replaced by the real Frappe-fieldtype mapping in P1.4.
	"""

	def __getitem__(self, key):
		unsupported("the fieldtype -> SurrealDB type map", "P1.4")

	def get(self, key, default=None):
		unsupported("the fieldtype -> SurrealDB type map", "P1.4")

	def __contains__(self, key):
		unsupported("the fieldtype -> SurrealDB type map", "P1.4")


def get_namespace() -> str:
	return frappe.conf.get("db_namespace") or DEFAULT_NAMESPACE


def get_url(host, port) -> str:
	scheme = "wss" if frappe.conf.get("db_ssl") else "ws"
	return f"{scheme}://{host or '127.0.0.1'}:{port or SurrealDBDatabase.default_port}"


class SurrealDBDatabase(SurrealDBExceptionUtil, Database):
	default_port = 8000
	MAX_ROW_SIZE_LIMIT = None

	def setup_type_map(self):
		self.db_type = "surrealdb"
		self.type_map = _UnmappedTypeMap()

	def get_connection(self) -> SurrealConnection:
		"""Open an authenticated session as the site's database-level user (see setup_db.setup_database)."""
		params = ConnectionParams(
			url=get_url(self.host, self.port),
			namespace=get_namespace(),
			database=self.cur_db_name,
			username=self.user,
			password=self.password,
			level="database",
		)
		return SurrealConnection(params).open()

	def sql(self, *args, as_dict=0, **kwargs):
		# pluck / as_list / plain tuples read columns by position; only as_dict reads by name
		self._require_order = not as_dict
		return super().sql(*args, as_dict=as_dict, **kwargs)

	def execute_query(self, query, values=None):
		self._cursor.require_order = getattr(self, "_require_order", True)
		return super().execute_query(query, values)

	def set_execution_timeout(self, seconds: int):
		# Rendered as a `TIMEOUT` clause by the query translator (P1.6); SurrealDB has no session-level setting and
		# `--transaction-timeout` is not enforced on interactive transactions (P0.4).
		self._conn.statement_timeout = seconds

	@staticmethod
	def escape(s, percent=True):
		unsupported("string escaping (values must be bound, never interpolated)", "P1.3")

	def get_database_size(self):
		unsupported("database size", "P1.9c")

	def get_tables(self, cached=True):
		unsupported("table introspection", "P1.4")

	def _estimate_count(self, table: str) -> int:
		unsupported("row count estimation", "P1.9c")

	def has_index(self, table_name, index_name):
		unsupported("index introspection", "P1.4")

	def add_index(self, doctype, fields, index_name=None):
		unsupported("adding an index", "P1.4")

	def add_unique(self, doctype, fields, constraint_name=None):
		unsupported("adding a unique constraint", "P1.4")

	def get_row_size(self, doctype: str) -> int:
		unsupported("row size", "P1.4")

	def rename_column(self, doctype: str, old_column_name: str, new_column_name: str):
		unsupported("renaming a column", "P1.4")
