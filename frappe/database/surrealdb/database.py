from frappe.database.database import Database
from frappe.database.surrealdb.errors import unsupported


class SurrealDBExceptionUtil:
	"""`is_*` predicates used by `Database` to classify driver errors (implemented in P1.3).

	They return False for now: an unclassified error propagates unchanged, it is never mistaken for a
	MariaDB error class.
	"""

	ProgrammingError = Exception
	TableMissingError = Exception
	OperationalError = Exception
	InternalError = Exception
	SQLError = Exception
	DataError = Exception

	@staticmethod
	def is_deadlocked(e) -> bool:
		return False

	@staticmethod
	def is_timedout(e) -> bool:
		return False

	@staticmethod
	def is_read_only_mode_error(e) -> bool:
		return False

	@staticmethod
	def is_table_missing(e) -> bool:
		return False

	@staticmethod
	def is_missing_column(e) -> bool:
		return False

	@staticmethod
	def is_duplicate_fieldname(e) -> bool:
		return False

	@staticmethod
	def is_duplicate_entry(e) -> bool:
		return False

	@staticmethod
	def is_access_denied(e) -> bool:
		return False

	@staticmethod
	def cant_drop_field_or_key(e) -> bool:
		return False

	@staticmethod
	def is_syntax_error(e) -> bool:
		return False

	@staticmethod
	def is_statement_timeout(e) -> bool:
		return False

	@staticmethod
	def is_data_too_long(e) -> bool:
		return False

	@staticmethod
	def is_db_table_size_limit(e) -> bool:
		return False

	@staticmethod
	def is_primary_key_violation(e) -> bool:
		return False

	@staticmethod
	def is_unique_key_violation(e) -> bool:
		return False

	@staticmethod
	def is_interface_error(e) -> bool:
		return False

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


class SurrealDBDatabase(SurrealDBExceptionUtil, Database):
	default_port = 8000
	MAX_ROW_SIZE_LIMIT = None

	def setup_type_map(self):
		self.db_type = "surrealdb"
		self.type_map = _UnmappedTypeMap()

	def get_connection(self):
		unsupported("the WebSocket connection", "P1.3")

	def set_execution_timeout(self, seconds: int):
		unsupported("statement execution timeout", "P1.3")

	def sql(self, *args, **kwargs):
		unsupported("running queries (driver boundary)", "P1.3")

	def sql_ddl(self, *args, **kwargs):
		unsupported("running DDL", "P1.4")

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
