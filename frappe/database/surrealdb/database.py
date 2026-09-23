import datetime
import re

import frappe
from contextlib import contextmanager
from frappe.database.database import Database
from frappe.database.utils import Query, QueryValues
from frappe.database.surrealdb import collation, text_shadows
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
	ER_READONLY,
	ER_STATEMENT_TIMEOUT,
	SurrealDBConnectionError,
	SurrealDBDataError,
	SurrealDBError,
	SurrealDBOperationalError,
	SurrealDBProgrammingError,
	SurrealDBTransactionConflict,
	unsupported,
)
from frappe.database.surrealdb.schema import (
	SHADOW_CI,
	SHADOW_HASH,
	SHADOW_LIKE,
	ColumnSpec,
	SurrealDBTable,
	TableInfo,
	base_field,
	clear_schema_cache,
	convert_column,
	index_statement,
	parse_table_info,
	physical,
	quote,
	quote_table,
	system_table_statements,
	table_schema,
	table_schema_from_info,
	unquote,
)
from frappe.utils import get_table_name


def _display_values(values):
	"""Log-display form of bound values: datetimes render as their MariaDB literal shape."""
	if isinstance(values, dict):
		return {k: _display_values(v) for k, v in values.items()}
	if isinstance(values, (list, tuple)):
		return [_display_values(v) for v in values]
	if isinstance(values, (datetime.datetime, datetime.date, datetime.timedelta)):
		return str(values)
	return values


def _split_top(text: str) -> list[str]:
	"""Split on top-level commas (quotes and parentheses protected)."""
	parts, buf, depth, quote = [], [], 0, None
	for ch in text:
		if quote:
			buf.append(ch)
			if ch == quote:
				quote = None
		elif ch in "'\"`":
			quote = ch
			buf.append(ch)
		elif ch == "(":
			depth += 1
			buf.append(ch)
		elif ch == ")":
			depth -= 1
			buf.append(ch)
		elif ch == "," and depth == 0:
			parts.append("".join(buf))
			buf = []
		else:
			buf.append(ch)
	if buf:
		parts.append("".join(buf))
	return parts


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
	SequenceGeneratorLimitExceeded = SurrealDBOperationalError
	DataError = SurrealDBDataError

	@staticmethod
	def is_deadlocked(e) -> bool:
		return isinstance(e, SurrealDBTransactionConflict)

	@staticmethod
	def is_timedout(e) -> bool:
		return _code(e) == ER_LOCK_WAIT_TIMEOUT

	@staticmethod
	def is_read_only_mode_error(e) -> bool:
		# `START TRANSACTION READ ONLY` writes are rejected with MariaDB's ER_READONLY shape
		# (SurrealCursor); the base Database maps this to frappe.InReadOnlyMode.
		return _code(e) == ER_READONLY

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


# Logical (MariaDB-shaped) type map: what `frappe.db.type_map` reports, what Frappe's alter logic compares, and what
# `information_schema.columns.column_type` would show. The physical SurrealDB type follows in schema.py.
def build_type_map(varchar_len: int) -> dict:
	return {
		"Currency": ("decimal", "21,9"),
		"Int": ("int", "11"),
		"Long Int": ("bigint", "20"),
		"Float": ("decimal", "21,9"),
		"Percent": ("decimal", "21,9"),
		"Check": ("tinyint", "4"),
		"Small Text": ("text", ""),
		"Long Text": ("longtext", ""),
		"Code": ("longtext", ""),
		"Text Editor": ("longtext", ""),
		"Markdown Editor": ("longtext", ""),
		"HTML Editor": ("longtext", ""),
		"Date": ("date", ""),
		"Datetime": ("datetime", "6"),
		"Time": ("time", "6"),
		"Text": ("text", ""),
		"Data": ("varchar", varchar_len),
		"Link": ("varchar", varchar_len),
		"Dynamic Link": ("varchar", varchar_len),
		"Password": ("text", ""),
		"Select": ("varchar", varchar_len),
		"Rating": ("decimal", "3,2"),
		"Read Only": ("varchar", varchar_len),
		"Attach": ("text", ""),
		"Attach Image": ("text", ""),
		"Signature": ("longtext", ""),
		"Color": ("varchar", varchar_len),
		"Barcode": ("longtext", ""),
		"Geolocation": ("longtext", ""),
		"Duration": ("decimal", "21,9"),
		"Icon": ("varchar", varchar_len),
		"Phone": ("varchar", varchar_len),
		"Autocomplete": ("varchar", varchar_len),
		"JSON": ("json", ""),
	}


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
		self.type_map = build_type_map(self.VARCHAR_LEN)

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
		# NOTE: the order matters - the hint paths recurse into sql()/execute_query() (as_dict INFO
		# reads) which reset `self._require_order` and the cursor flag; the flag captured below reflects
		# THIS call's consumer and is applied to the cursor after the recursion.
		require_order = getattr(self, "_require_order", True)
		query = self._order_by_projection(query)
		query = self._columns_hint_for_star(query, values, require_order)
		self._cursor.require_order = require_order
		return super().execute_query(query, values)

	_STAR_SELECT = re.compile(r"(?is)^\s*select\s+\*\s+from\s+(?:`|\")?(\w+)(?:`|\")?")

	def _order_by_projection(self, query: str) -> str:
		"""Raw `select <cols> from <table> ... order by <f>` where <f> is not in the projection:
		SurrealQL's ORDER BY only sees the projection (measured: `select code ... order by name`
		is a syntax error while `order by code` works). The translator projects its ordering
		columns as hidden `__oN` for the same reason - do the same here and pin the consumer's
		column order with the /*cols:*/ hint."""
		if re.search(r"/\*\s*cols:", query) or re.search(r"(?i)\bjoin\b", query):
			return query
		m = re.match(r"(?is)^(.*?)(\border\s+by\s+(.+))$", query.strip())
		if not m:
			return query
		head, order_part = m[1].strip(), m[3]
		# order clause: field list, then an optional LIMIT/START tail
		tail_m = re.search(r"(?is)\s+(limit|start|offset)\s+\d+", order_part)
		tail = order_part[tail_m.start() :] if tail_m else ""
		order_terms = (order_part[: tail_m.start()] if tail_m else order_part).strip()
		proj_m = re.match(r"(?is)^select\s+(.+?)\s+from\b", head)
		if not proj_m:
			return query
		projections = [p.strip() for p in _split_top(proj_m[1])]
		for p in projections:
			if not re.fullmatch(r"(?:`[\w@]+`|\w+)(?:\s+as\s+\w+)?", p, re.I):
				return query  # complex projection: leave the statement untouched
		projected = {re.sub(r"[`]", "", p.split()[-1 if re.search(r"(?i)\s+as\s", p) else 0]).lower() for p in projections}
		schema = None
		table_m = re.search(r"(?is)from\s+(`[^`]+`|\"[^\"]+\"|[\w@]+)", head)
		if table_m:
			try:
				schema = table_schema(unquote(table_m[1]), db=self)
			except Exception:
				schema = None
		hidden, rewritten = [], []
		for term in [t.strip() for t in _split_top(order_terms)]:
			fm = re.fullmatch(r"(`?)([\w@]+)\1(\s+(?:asc|desc))?", term, re.I)
			if not fm or fm[2].lower() in projected or (schema and schema.column(fm[2]) is None):
				rewritten.append(term)
				continue
			rewritten.append(f"`__o{len(hidden)}`{fm[3] or ''}")
			hidden.append(fm[2])
		if not hidden:
			return query
		proj_start, proj_end = proj_m.span(1)
		new_projections = ", ".join([*projections, *(f"`{f}` AS `__o{i}`" for i, f in enumerate(hidden))])
		query = f"{head[:proj_start]}{new_projections}{head[proj_end:]} ORDER BY {', '.join(rewritten)}{tail}"
		names = [re.sub(r"[`]", "", p.split()[-1] if re.search(r"(?i)\s+as\s", p) else p) for p in projections]
		if schema:
			kinds = [schema.column(n).kind for n in names if schema.column(n) is not None]
			if len(kinds) == len(names):
				query += f" /*kinds:{','.join(kinds)}*/"
		return f"{query} /*cols:{','.join(names)}*/"

	def _columns_hint_for_star(self, query: str, values, require_order: bool = True) -> str:
		"""A raw `select * from <table>` read positionally (as_list / plain tuples / pluck) needs the
		translator's column-order hint: SurrealDB returns object keys alphabetically - plus the
		collation shadows - while MariaDB returns columns in schema order. The hint (in the same shape
		the translator emits for its own star projections) restores the schema order, drops the shadow
		keys and decodes the temporal columns. `as_dict` consumers keep the raw alphabetical rows.

		Guarded by the driver contract (P1.6): it only fires for parameterless reads (values passed -
		including named temporal params, which are refused - must not touch the server before the
		refusal) and only when a *real* `INFO FOR TABLE` response (it carries the `fields` key) yields
		a schema; anything else lets the cursor's no-column-order error fire."""
		if not require_order or values is not None:
			return query
		m = self._STAR_SELECT.match(query)
		if not m or re.search(r"(?i)\bjoin\b", query) or re.search(r"/\*\s*cols:", query):
			return query
		try:
			info = self._info(f"INFO FOR TABLE {m[1]}")
			if "fields" not in info:
				return query  # not a real INFO response: no trustworthy column order
			cols = list(table_schema_from_info(m[1], info).columns.values())
		except Exception:
			return query  # unknown table: let the server report it
		if not cols:
			return query
		cols_hint = ",".join(spec.name for spec in cols)
		kinds_hint = ",".join(spec.kind for spec in cols)
		return f"{query} /*cols:{cols_hint}*/ /*kinds:{kinds_hint}*/"

	def sql_ddl(self, query, debug=False):
		"""Commit, run a DDL statement, commit again. MariaDB DDL autocommits; here it must too, because SurrealDB
		features outside the transaction (sequences) and other connections only see committed definitions."""
		transaction_control = self._disable_transaction_control
		self._disable_transaction_control = 0
		try:
			self.commit()
			self.sql(query, debug=debug)
			self.sql("commit")
			self.begin()
		finally:
			self._disable_transaction_control = transaction_control

	def check_implicit_commit(self, query: str, query_type: str):
		"""MariaDB commits implicitly on DDL, which Frappe guards against once a transaction has written. In SurrealDB
		`CREATE` is an ordinary row insert and DDL (`DEFINE`/`REMOVE`) is transactional, so nothing commits implicitly."""

	@staticmethod
	def clear_db_table_cache(query_type: str):
		# `create` is a row insert here; only DEFINE/REMOVE change the table list
		if query_type in ("define", "remove"):
			frappe.client_cache.delete_value("db_tables")
			clear_schema_cache()

	def set_execution_timeout(self, seconds: int):
		# Rendered as a `TIMEOUT` clause by the query translator (P1.6); SurrealDB has no session-level setting and
		# `--transaction-timeout` is not enforced on interactive transactions (P0.4).
		self._conn.statement_timeout = seconds

	@contextmanager
	def unbuffered_cursor(self):
		"""SurrealDB hands the SDK the complete result set - there is no server-side cursor to switch
		to - so the buffered cursor already returns every row; yield it unchanged. (Memory is not
		streamed, but all rows are returned exactly once, which is what the callers rely on.)"""
		yield

	@staticmethod
	def escape(s, percent=True):
		unsupported("string escaping (values must be bound, never interpolated)", "P1.3")

	def mogrify(self, query: Query, values: QueryValues):
		"""Log-display form of a statement. P1.3: the driver always executes with bound
		values, so nothing is interpolated into the query; the log shows the query as
		sent plus the bound values. This bypasses the base-class fallback, which would
		call `frappe.db.escape` - fail-closed on this backend - whenever a statement
		(e.g. a DDL-classified `CREATE ... SET x = $v`) is logged with values. Temporal
		values are displayed as their MariaDB literal form (`2026-09-21 10:00:00`)."""
		if not values:
			return query
		return f"{query} /* values: {_display_values(values)!r} */"

	def log_query(self, query, query_type, values, debug):
		mogrified_query = self.mogrify(query, values)
		self.last_query = mogrified_query
		self._log_query(mogrified_query, query_type, debug, query)
		return mogrified_query

	def get_database_size(self):
		unsupported("database size", "P4.5")

	def get_database_list(self):
		return [self.cur_db_name]

	# --- introspection (P1.4): INFO FOR DB / INFO FOR TABLE -------------------------------------------------------
	def _info(self, statement: str) -> dict:
		rows = self.sql(statement, as_dict=True)
		return dict(rows[0]) if rows else {}

	def get_tables(self, cached=True):
		"""Return list of tables."""
		to_query = not cached
		if cached:
			tables = frappe.client_cache.get_value("db_tables")
			to_query = not tables
		if to_query:
			tables = [unquote(name) for name in (self._info("INFO FOR DB").get("tables") or {})]
			frappe.client_cache.set_value("db_tables", tables)
		return tables

	def table_info(self, table_name: str) -> TableInfo:
		return parse_table_info(self._info(f"INFO FOR TABLE {quote_table(table_name)}"))

	def get_db_table_columns(self, table) -> list[str]:
		"""Column names of a table (the shadow fields are an implementation detail and never listed)."""
		key = f"table_columns::{table}"
		columns = frappe.client_cache.get_value(key)
		if columns is None:
			columns = list(self.table_info(table).columns)
			if columns:
				frappe.client_cache.set_value(key, columns)
		return columns

	def get_table_columns_description(self, table_name):
		"""Return list of columns with descriptions (the shape of MariaDB's information_schema query)."""
		info = self.table_info(table_name)
		out = []
		for name, meta in info.columns.items():
			indexed, unique = info.column_flags(name)
			out.append(
				frappe._dict(
					name=name,
					type=meta["t"],
					default=meta.get("d"),
					index=1 if indexed else 0,
					unique=unique,
					not_nullable=not meta["n"],
				)
			)
		return out

	def get_column_type(self, doctype, column):
		"""Return column type from database."""
		meta = self.table_info(get_table_name(doctype)).columns.get(column)
		if meta is None:
			raise IndexError(column)  # what `.run(pluck=True)[0]` raises on MariaDB for an unknown column
		return meta["t"]

	def describe(self, doctype: str) -> list | tuple:
		info = self.table_info(get_table_name(doctype))
		rows = []
		for name, meta in info.columns.items():
			indexed, unique = info.column_flags(name)
			key = "PRI" if name == "name" else "UNI" if unique else "MUL" if indexed else ""
			rows.append((name, meta["t"], "NO" if not meta["n"] else "YES", key, meta.get("d"), ""))
		return tuple(rows)

	def has_index(self, table_name, index_name):
		return index_name in self.table_info(table_name).indexes

	def get_column_index(self, table_name: str, fieldname: str, unique: bool = False) -> frappe._dict | None:
		"""Index whose *first* column is `fieldname` (and that has no second column, like MariaDB's clustered check)."""
		for name, ix in self.table_info(table_name).indexes.items():
			if ix["kind"] in ("SEARCH", "FULLTEXT") or ix["unique"] != unique:
				continue
			if len(ix["fields"]) == 1 and base_field(ix["fields"][0]) == fieldname:
				return frappe._dict(
					Key_name=name, Column_name=fieldname, Non_unique=int(not unique), Seq_in_index=1
				)
		return None

	def _stored_index_fields(self, table_name: str, fields: list) -> list[str]:
		"""Index on the collation shadow for varchar columns, on the raw field otherwise."""
		columns = self.table_info(table_name).columns
		out = []
		for f in fields:
			f = INDEX_LENGTH.sub("", f)
			meta = columns.get(f)
			if meta is None:
				raise SurrealDBProgrammingError(1072, f"Key column '{f}' doesn't exist in table")
			out.append(physical(f) + (SHADOW_CI if meta["t"].startswith("varchar") else ""))
		return out

	def add_index(self, doctype: str, fields: list, index_name: str | None = None):
		"""Creates an index with given fields if not already created.
		Index name will be `fieldname1_fieldname2_index`"""
		from frappe.custom.doctype.property_setter.property_setter import make_property_setter

		index_name = index_name or self.get_index_name(fields)
		table_name = get_table_name(doctype)
		if not self.has_index(table_name, index_name):
			self.commit()
			self.sql_ddl(
				index_statement(
					table_name, index_name, self._stored_index_fields(table_name, fields), if_not_exists=True
				)
			)
			# Ensure that DB migration doesn't clear this index, assuming this is manually added
			# via code or console.
			if len(fields) == 1 and not (frappe.flags.in_install or frappe.flags.in_migrate):
				make_property_setter(
					doctype,
					fields[0],
					property="search_index",
					value="1",
					property_type="Check",
					for_doctype=False,  # Applied on docfield
				)

	def add_unique(self, doctype, fields, constraint_name=None):
		if isinstance(fields, str):
			fields = [fields]
		if not constraint_name:
			constraint_name = "unique_" + "_".join(fields)
		table_name = "tab" + doctype
		if not self.has_index(table_name, constraint_name):
			self.commit()
			self.sql_ddl(
				index_statement(
					table_name, constraint_name, self._stored_index_fields(table_name, fields), unique=True
				)
			)

	def updatedb(self, doctype, meta=None):
		"""
		Syncs a `DocType` to the table
		* creates if required
		* updates columns
		* updates indices
		"""
		res = self.sql(
			"SELECT issingle FROM type::record($table, $id) /*cols:issingle*/",
			{"table": "tabDocType", "id": collation.record_id(doctype)},
		)
		if not res:
			raise Exception(f"Wrong doctype {doctype} in updatedb")
		if not res[0][0]:
			db_table = SurrealDBTable(doctype, meta)
			db_table.validate()
			db_table.sync()
			self.commit()

	# --- table / column alteration ---------------------------------------------------------------------------------
	def rename_table(self, old_name: str, new_name: str) -> list | tuple:
		"""SurrealDB has no RENAME: recreate the definitions under the new name, copy the rows, drop the old table."""
		old, new = get_table_name(old_name), get_table_name(new_name)
		info = self._info(f"INFO FOR TABLE {quote_table(old)}")
		old_q, new_q = quote_table(old), quote_table(new)
		self.sql_ddl(f"DEFINE TABLE {new_q} SCHEMAFULL")
		for definition in [*(info.get("fields") or {}).values(), *(info.get("indexes") or {}).values()]:
			if f" ON {old_q} " not in definition:
				raise SurrealDBProgrammingError(
					0, f"Cannot rename {old}: unexpected definition {definition!r}"
				)
			self.sql_ddl(definition.replace(f" ON {old_q} ", f" ON {new_q} ", 1))
		self.sql_ddl(
			f"INSERT INTO {new_q} (SELECT *, type::record('{new}', record::id(id)) AS id FROM {old_q}) RETURN NONE"
		)
		self.sql_ddl(f"REMOVE TABLE {old_q}")
		return ()

	def change_column_type(
		self, doctype: str, column: str, type: str, nullable: bool = False
	) -> list | tuple:
		table = get_table_name(doctype)
		current = self.table_info(table).columns.get(column)
		spec = ColumnSpec(column, type, nullable=nullable)
		convert_column(table, spec, current["t"] if current is not None else type, db=self)
		return ()

	def rename_column(self, doctype: str, old_column_name, new_column_name):
		table = get_table_name(doctype)
		info = self.table_info(table)
		meta = info.columns.get(old_column_name)
		if meta is None:
			raise SurrealDBProgrammingError(ER_CANT_DROP_FIELD_OR_KEY, f"Unknown column '{old_column_name}'")
		spec = ColumnSpec(new_column_name, meta["t"], nullable=bool(meta["n"]))
		# P1.15: the shadows belong to the stored column. When the OLD column carried them (varchar, or a
		# registry-listed text column), the renamed column gets the shadow fields re-defined under the new
		# name and the values copied - including @hash, whose ASSERT is re-defined for the new source name.
		# The registry entry for the old name is stale afterwards (the sync hook cleans the event up);
		# per §3.4 the copied shadows stay in place as documented dead storage until then.
		if spec.is_text and text_shadows.is_shadowed(table, old_column_name):
			spec.text_collation_shadow = True
		self.sql_ddl(spec.define_field(table))
		for stmt in spec.define_shadows(table):
			self.sql_ddl(stmt)
		old_s, new_s = physical(old_column_name), physical(new_column_name)
		sets = [f"{quote(new_s)} = {quote(old_s)}"]
		if spec.has_collation_shadow:
			sets += [
				f"{quote(new_s + SHADOW_CI)} = {quote(old_s + SHADOW_CI)}",
				f"{quote(new_s + SHADOW_LIKE)} = {quote(old_s + SHADOW_LIKE)}",
			]
		if spec.has_integrity_hash:
			sets.append(f"{quote(new_s + SHADOW_HASH)} = {quote(old_s + SHADOW_HASH)}")
		self.sql_ddl(f"UPDATE {quote_table(table)} SET {', '.join(sets)}")
		for name, ix in info.indexes.items():
			if any(base_field(f) == old_column_name for f in ix["fields"]):
				self.sql_ddl(f"REMOVE INDEX {quote(physical(name))} ON {quote_table(table)}")
				fields = [
					new_s + (SHADOW_CI if f.endswith(SHADOW_CI) else "")
					if base_field(f) == old_column_name
					else f
					for f in ix["fields"]
				]
				new_name = new_column_name if name == old_column_name else name
				self.sql_ddl(index_statement(table, new_name, fields, unique=ix["unique"]))
		self._remove_column_definitions(table, old_s, spec.has_collation_shadow, spec.has_integrity_hash)

	def drop_columns(self, doctype: str, columns: list[str]) -> list | tuple:
		"""Drop columns (and their shadow columns / integrity-hash witness) from a table. Mirrors MariaDB's
		ALTER TABLE ... DROP COLUMN, including its implicit commit."""
		table = get_table_name(doctype)
		info = self.table_info(table)
		for column in columns:
			meta = info.columns.get(column)
			if meta is None:
				raise SurrealDBProgrammingError(
					ER_CANT_DROP_FIELD_OR_KEY, f"Can't DROP '{column}'; check that column/key exists"
				)
			spec = ColumnSpec(column, meta["t"], nullable=bool(meta["n"]))
			if spec.is_text and text_shadows.is_shadowed(table, column):
				spec.text_collation_shadow = True
			self._remove_column_definitions(
				table, physical(column), spec.has_collation_shadow, spec.has_integrity_hash
			)
		self.commit()
		return ()

	def _remove_column_definitions(self, table: str, stored: str, shadowed: bool, hashed: bool = False):
		"""Drop a column (and, when present, its `@ci`/`@like` shadows and `@hash` witness). `REMOVE FIELD`
		leaves the stored values behind (and a SCHEMAFULL table then rejects them on the next copy/update),
		so they are unset too - after the definition is gone, because a still-defined `string | null` field
		refuses the NONE that UNSET produces."""
		names = [stored]
		if shadowed:
			names += [stored + SHADOW_CI, stored + SHADOW_LIKE]
		if hashed:
			names.append(stored + SHADOW_HASH)
		for n in names:
			self.sql_ddl(f"REMOVE FIELD {quote(n)} ON {quote_table(table)}")
		self.sql_ddl(f"UPDATE {quote_table(table)} UNSET {', '.join(quote(n) for n in names)}")

	def truncate(self, doctype: str):
		"""Remove every row. (MariaDB's TRUNCATE also commits implicitly and resets AUTO_INCREMENT.)"""
		self.sql_ddl(f"DELETE {quote_table(get_table_name(doctype))}")

	# --- system tables ---------------------------------------------------------------------------------------------
	def _create_system_table(self, name: str):
		if name in self.get_tables(cached=False):
			return
		for stmt in system_table_statements(name):
			self.sql_ddl(stmt)
		frappe.client_cache.delete_value("db_tables")

	def create_auth_table(self):
		self._create_system_table("__Auth")

	def create_global_search_table(self):
		# MariaDB adds FULLTEXT(content); the SurrealDB search index and MATCH ... AGAINST equivalent belong to P1.9c.
		self._create_system_table("__global_search")

	def create_user_settings_table(self):
		self._create_system_table("__UserSettings")

	def create_singles_table(self):
		"""MariaDB's tabSingles (`frappe.qb.into("Singles")` resolves to `tabSingles` via get_table_name)."""
		self._create_system_table("tabSingles")

	def create_series_table(self):
		"""MariaDB's tabSeries (naming-series counters; `frappe.qb.DocType("Series")` statements)."""
		self._create_system_table("tabSeries")

	def create_sessions_table(self):
		"""MariaDB's tabSessions (the `Session` doctype is gone in v16; sessions.py writes it directly)."""
		self._create_system_table("tabSessions")

	@staticmethod
	def get_on_duplicate_update():
		unsupported("INSERT ... ON DUPLICATE KEY UPDATE", "P1.6b")

	def get_row_size(self, doctype: str) -> int:
		"""Estimated max row size in bytes, from the logical column types with MariaDB's sizing rules."""
		total = 0
		for meta in self.table_info(get_table_name(doctype)).columns.values():
			total += estimated_column_size(meta["t"])
		return total

	def _estimate_count(self, table: str) -> int:
		from frappe.utils.data import cint

		rows = self.sql(f"SELECT count() AS c FROM {quote_table(table)} GROUP ALL /*cols:c*/")
		return cint(rows[0][0]) if rows else 0

	# --- sequences (P1.4; hardening is P3.2) -----------------------------------------------------------------------
	def _sequence_names(self) -> set[str]:
		return {unquote(n) for n in (self._info("INFO FOR DB").get("sequences") or {})}

	def create_sequence(
		self,
		doctype_name: str,
		*,
		slug: str = "_id_seq",
		temporary: bool = False,
		check_not_exists: bool = False,
		cycle: bool = False,
		cache: int = 0,
		start_value: int = 0,
		increment_by: int = 0,
		min_value: int = 0,
		max_value: int = 0,
	) -> str:
		if temporary or cycle or cache or increment_by or min_value or max_value:
			unsupported("sequence options (increment/min/max/cycle/cache/temporary)", "P3.2")
		name = frappe.scrub(doctype_name + slug)
		if check_not_exists and name in self._sequence_names():
			return name
		# BATCH 1 = MariaDB's `nocache`: a value is never pre-allocated, so a restart cannot skip numbers
		self.sql_ddl(f"DEFINE SEQUENCE {quote(name)} BATCH 1 START {start_value or 1}")
		return name

	def get_next_sequence_val(self, doctype_name: str, slug: str = "_id_seq") -> int:
		name = frappe.scrub(f"{doctype_name}{slug}")
		try:
			return self.sql("RETURN sequence::nextval($name)", {"name": name})[0][0]
		except IndexError:
			raise self.SequenceGeneratorLimitExceeded from None

	def set_next_sequence_val(
		self, doctype_name: str, next_val: int, *, slug: str = "_id_seq", is_val_used: bool = False
	) -> None:
		"""MariaDB SETVAL: the next `nextval` returns `next_val` (or `next_val + 1` if that value counts as used).
		SurrealDB fixes the start when the sequence is defined, so the sequence is recreated."""
		name = frappe.scrub(doctype_name + slug)
		self.sql_ddl(f"REMOVE SEQUENCE IF EXISTS {quote(name)}")
		self.sql_ddl(f"DEFINE SEQUENCE {quote(name)} BATCH 1 START {next_val + (1 if is_val_used else 0)}")


INDEX_LENGTH = re.compile(r"\(\d+\)$")


def estimated_column_size(logical: str) -> int:
	"""Max bytes of a column, MariaDB's sizing (mirrors `MariaDBDatabase.get_row_size`)."""
	spec = ColumnSpec("x", logical)
	if spec.kind == "tinyint":
		return 1
	if spec.kind == "smallint":
		return 2
	if spec.kind == "int":
		return 4
	if spec.kind == "bigint":
		return 8
	if spec.kind == "decimal":
		precision, scale = spec.arg if len(spec.arg) == 2 else (10, 0)
		ints = precision - scale
		return (ints // 9) * 4 + -(-(ints % 9) // 2) + (scale // 9) * 4 + -(-(scale % 9) // 2)
	if spec.kind == "date":
		return 3
	if spec.kind == "time":
		return 3 + -(-(spec.arg[0] if spec.arg else 0) // 2)
	if spec.kind == "datetime":
		return 5 + -(-(spec.arg[0] if spec.arg else 0) // 2)
	if spec.kind == "varchar":
		octets = 4 * (spec.arg[0] if spec.arg else 140)
		return (2 if octets > 255 else 1) + octets
	if spec.kind == "uuid":
		return 16
	if spec.kind == "text":
		return 10 if spec.logical == "text" else 12
	return 12
