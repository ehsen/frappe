"""Schema layer of the SurrealDB backend (chunk P1.4): Frappe fieldtypes -> `DEFINE TABLE/FIELD/INDEX`.

Storage model (ADR 0001, findings/P0.3, findings/P1.4):

* Every table is `SCHEMAFULL`; a write to an undefined table would otherwise silently create a schemaless one.
* The *logical* column type stays MariaDB-shaped (`varchar(140)`, `decimal(21,9)`, `int(11)`, `datetime(6)` ...): it is
  what `frappe.db.type_map`, `information_schema` emulation and Frappe's alter logic reason about. The *physical*
  SurrealDB type follows from it (`string`, `int`, `decimal`); dates/datetimes are canonical text, times integer
  microseconds (values.py).
* Each field carries its logical type, nullability and default in a `COMMENT` (JSON), so the table can be introspected
  without the DocType meta. `INFO FOR TABLE` is the single source of truth.
* Every `varchar` column has two shadow fields: `<col>@ci` (collation key: equality, IN, UNIQUE, range, ORDER BY) and
  `<col>@like` (character-delimited weights for exact LIKE). `@` cannot occur in a Frappe fieldname, so shadows can
  never collide with a real column. Indexes and UNIQUE constraints on a varchar column are defined on `@ci`.
* A Frappe column called `id` is stored as `id@f` (`id` is the record id in SurrealDB).
* Indexes are named as MariaDB names them (`creation`, `parent`, the column name for unique/search indexes) so that
  `has_index` / `get_column_index` keep working.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal

import frappe
from frappe import _
from frappe.database.schema import NOT_NULL_TYPES, DbColumn, DBTable, get_definition, validate_column_name
from frappe.database.surrealdb import collation, text_shadows, values
from frappe.database.surrealdb.errors import SurrealDBProgrammingError
from frappe.utils import cint, cstr, flt
from frappe.utils.defaults import get_not_null_defaults

LOGICAL_TYPE = re.compile(r"^(\w+)(?:\(([\d,]+)\))?")
_IDENT = re.compile(r"^[^`\\\x00-\x1f⟩]{1,200}$")
_TABLE = re.compile(r"^[\w -]{1,200}$", re.ASCII)

_MYSQL_ESCAPES = {
	"\\": "\\\\",
	"\0": "\\0",
	"\n": "\\n",
	"\r": "\\r",
	"'": "\\'",
	'"': '\\"',
	"\x1a": "\\Z",
}  # what pymysql's escape_string does; MariaDB reports string defaults escaped this way

SHADOW_CI = "@ci"
SHADOW_LIKE = "@like"

# Columns every DocType table has, in MariaDB's `create()` order: (name, logical type, nullable, default)
DEFAULT_COLUMNS = (
	("creation", "datetime(6)", True, None),
	("modified", "datetime(6)", True, None),
	("modified_by", "varchar(140)", True, None),
	("owner", "varchar(140)", True, None),
	("docstatus", "tinyint(4)", False, 0),
	("idx", "int(11)", False, 0),
)
CHILD_COLUMNS = (("parent", "varchar(140)"), ("parentfield", "varchar(140)"), ("parenttype", "varchar(140)"))

DATE_REGEX = "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
DATETIME_REGEX = "^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{6}$"


# --- identifiers and literals ---------------------------------------------------------------------------------------
def precision_scale(arg: tuple) -> tuple[int, int]:
	"""(precision, scale) of a decimal column type; Frappe always sets both, MariaDB's own default is (10, 0)."""
	return (arg[0], arg[1]) if len(arg) == 2 else (21, 9)


def quote(name: str) -> str:
	"""Backtick-quote a table/field/index name. Frappe names contain spaces, reserved words and unicode."""
	if not isinstance(name, str) or not _IDENT.match(name):
		raise SurrealDBProgrammingError(
			0, f"Invalid identifier {name!r}: backticks, backslashes and control characters are not allowed."
		)
	return f"`{name}`"


def quote_table(table_name: str) -> str:
	if not isinstance(table_name, str) or not _TABLE.match(table_name):
		raise SurrealDBProgrammingError(0, f"Invalid table name {table_name!r}")
	return f"`{table_name}`"


# SurrealDB 3.2.4 accepts these names in `DEFINE FIELD/INDEX` even when backtick-quoted, but the table is then poisoned: every
# later statement on it fails with a "conversion error" (the definition is stored unquoted and cannot be parsed again).
# Measured over every fieldname in Frappe, ERPNext and Payments plus SurrealQL's keywords (6,045 names):
# spike/P1.4-schema/reserved_names_probe.py. `id` is the record id. They are stored as `<name>@f`.
RESERVED_NAMES = frozenset(
	"""alter break continue create define delete explain false for function id if info insert let none null rebuild relate remove
	return select sleep throw true update upsert""".split()
)


def physical(fieldname: str) -> str:
	"""Stored field / index name of a Frappe column or index (reserved names get an `@f` suffix)."""
	return fieldname + "@f" if fieldname.lower() in RESERVED_NAMES else fieldname


def logical_name(stored: str) -> str:
	return stored[:-2] if stored.endswith("@f") else stored


def is_shadow(stored: str) -> bool:
	return stored.endswith((SHADOW_CI, SHADOW_LIKE))


def surql_string(value: str) -> str:
	"""Double-quoted SurrealQL string literal. Only used for defaults and metadata, never for user data."""
	out = []
	for ch in value:
		if ch == "\\":
			out.append("\\\\")
		elif ch == '"':
			out.append('\\"')
		elif ch == "\n":
			out.append("\\n")
		elif ch == "\r":
			out.append("\\r")
		elif ch == "\t":
			out.append("\\t")
		elif ord(ch) < 0x20:
			raise SurrealDBProgrammingError(
				0, f"Control character U+{ord(ch):04X} is not allowed in a schema literal"
			)
		else:
			out.append(ch)
	return '"' + "".join(out) + '"'


def unescape(inner: str) -> str:
	return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m[1], m[1]), inner)


# --- column specification ------------------------------------------------------------------------------------------
# Invariant (P1.15, see frappe/database/surrealdb/text_shadows.py):
# A text_collation_shadow column remains physically and semantically a Text column EXCEPT for
# string-collation operations. Its @ci and @like fields provide the MariaDB-compatible equivalence
# relation used by =, !=, IN, NOT IN, emptiness, LIKE, ORDER BY and GROUP BY. All of these operations
# MUST use the same collation representation. Mixing @ci with inline string::lowercase() on one
# column is forbidden.
# text_collation_shadow MUST NOT imply: varchar length limits (assertion() stays is_varchar-only);
# varchar DDL type / meta["t"] (kind is never mutated); index eligibility (index_field() stays
# is_varchar-only). Source, @ci, @like and @hash are written together by
# text_shadows.build_collation_shadows() in one statement; the engine rejects any write where
# @hash != sha256(source). If shadow integrity is not established, queries on the column RAISE —
# they never fall back to the inline lowercase path.
@dataclass
class ColumnSpec:
	name: str
	logical: str
	nullable: bool = True
	default: object = None  # python value, None = no DEFAULT beyond NULL
	unique: bool = False
	index: bool = False
	custom_assert: str | None = None
	order: int | None = None  # position in the table definition (SELECT * returns columns in this order)
	text_collation_shadow: bool = False  # set only by specs()/table_schema_from_info() from the registry
	kind: str = field(init=False)
	arg: tuple = field(init=False)

	def __post_init__(self):
		m = LOGICAL_TYPE.match(self.logical)
		base = m[1] if m else self.logical
		self.arg = tuple(int(x) for x in (m[2] or "").split(",") if x) if m else ()
		self.kind = {"longtext": "text", "text": "text", "json": "json", "mediumtext": "text"}.get(base, base)
		if self.text_collation_shadow and self.kind not in ("text", "json"):
			raise SurrealDBProgrammingError(
				0, f"text_collation_shadow is only legal on text/json columns ({self.name!r} is {self.kind!r})"
			)

	@property
	def is_varchar(self) -> bool:
		return self.kind == "varchar"

	@property
	def is_text(self) -> bool:
		return self.kind in ("text", "json")

	@property
	def has_collation_shadow(self) -> bool:
		"""Column carries @ci/@like and supports exact MariaDB string collation."""
		return self.is_varchar or self.text_collation_shadow

	@property
	def has_integrity_hash(self) -> bool:
		"""Column carries an engine-enforced @hash witness (P1.15: text shadows only)."""
		return self.text_collation_shadow

	@property
	def is_length_limited_string(self) -> bool:
		return self.is_varchar

	@property
	def shadow_index_eligible(self) -> bool:
		return self.is_varchar

	@property
	def surreal_type(self) -> str:
		base = {
			"varchar": "string",
			"text": "string",
			"json": "string",
			"uuid": "string",
			"date": "string",
			"datetime": "string",
			"int": "int",
			"tinyint": "int",
			"smallint": "int",
			"bigint": "int",
			"time": "int",
			"decimal": "decimal",
		}.get(self.kind)
		if base is None:
			raise SurrealDBProgrammingError(0, f"No SurrealDB storage type for column type {self.logical!r}")
		return f"{base} | null" if self.nullable else base

	def display_default(self) -> str | None:
		"""Default as MariaDB's `information_schema.columns.column_default` shows it."""
		if self.default is None:
			return "NULL" if self.nullable else None
		if self.kind == "decimal":
			precision, scale = precision_scale(self.arg)
			try:
				return format(values.to_decimal(self.default, precision, scale), "f")
			except values.ValueError_:
				pass  # not a number: shown as text below (MariaDB rejects such a DDL default anyway)
		if isinstance(self.default, str):
			return "'" + "".join(_MYSQL_ESCAPES.get(ch, ch) for ch in self.default) + "'"
		return str(self.default)

	def default_literal(self) -> str | None:
		if self.default is None:
			return "NULL" if self.nullable else None
		d = self.default
		if self.kind in ("int", "tinyint", "smallint", "bigint"):
			return str(values.to_int(d, "int" if self.kind == "int" else self.kind))
		if self.kind == "decimal":
			precision, scale = precision_scale(self.arg)
			return f"{format(values.to_decimal(d, precision, scale), 'f')}dec"
		if self.kind == "date":
			return surql_string(values.to_date(d))
		if self.kind == "datetime":
			return surql_string(values.to_datetime(d))
		if self.kind == "time":
			return str(values.to_time_us(d))
		return surql_string(cstr(d))

	def assertion(self) -> str | None:
		if self.custom_assert:
			return f"$value = NULL OR ({self.custom_assert})" if self.nullable else self.custom_assert
		v = "$value"
		conds = []
		if self.kind == "varchar":
			conds.append(f"string::len({v}) <= {self.arg[0] if self.arg else 140}")
		elif self.kind in ("int", "tinyint", "smallint"):
			lo, hi = values.INT_RANGES[self.kind]
			conds.append(f"{v} >= {lo} AND {v} <= {hi}")
		elif self.kind == "date":
			conds.append(f"string::matches({v}, /{DATE_REGEX}/)")
		elif self.kind == "datetime":
			conds.append(f"string::matches({v}, /{DATETIME_REGEX}/)")
		elif self.kind == "time":
			conds.append(f"{v} >= {-values.MAX_TIME_US} AND {v} <= {values.MAX_TIME_US}")
		if not conds:
			return None
		expr = " AND ".join(conds)
		return f"$value = NULL OR ({expr})" if self.nullable else expr

	def meta(self) -> dict:
		m = {"t": self.logical, "n": 1 if self.nullable else 0}
		if self.order is not None:
			m["o"] = self.order
		if (d := self.display_default()) is not None:
			m["d"] = d
		return m

	def define_field(self, table: str, overwrite: bool = False) -> str:
		parts = [
			"DEFINE FIELD OVERWRITE" if overwrite else "DEFINE FIELD",
			quote(physical(self.name)),
			"ON",
			quote_table(table),
			"TYPE",
			self.surreal_type,
		]
		if (lit := self.default_literal()) is not None:
			parts += ["DEFAULT", lit]
		if (assertion := self.assertion()) is not None:
			parts += ["ASSERT", assertion]
		parts += ["COMMENT", surql_string(json.dumps(self.meta(), separators=(",", ":"), ensure_ascii=False))]
		return " ".join(parts)

	def define_shadows(self, table: str, overwrite: bool = False) -> list[str]:
		if not self.is_varchar:
			return []
		out = []
		for suffix, kind in ((SHADOW_CI, "ci"), (SHADOW_LIKE, "like")):
			meta = surql_string(json.dumps({"s": kind, "of": self.name}, separators=(",", ":")))
			out.append(
				f"DEFINE FIELD {'OVERWRITE ' if overwrite else ''}{quote(physical(self.name) + suffix)} "
				f"ON {quote_table(table)} TYPE string | null DEFAULT NULL COMMENT {meta}"
			)
		return out

	def index_field(self) -> str:
		"""Stored field an index on this column is defined on (the collation shadow for varchar)."""
		return physical(self.name) + SHADOW_CI if self.is_varchar else physical(self.name)


def column_spec_from_docfield(col: DbColumn) -> ColumnSpec | None:
	"""Same decisions as `DbColumn.get_definition`, as a structured spec instead of MariaDB DDL text."""
	logical = get_definition(col.fieldtype, precision=col.precision, length=col.length, options=col.options)
	if not logical:
		return None
	nullable, default = True, None
	if col.fieldtype in NOT_NULL_TYPES:
		nullable = False
	if col.fieldtype in ("Check", "Int"):
		default = cint(col.default)
	elif col.fieldtype in ("Currency", "Float", "Percent"):
		default = flt(col.default)
	elif (
		col.default
		and (col.default not in frappe.db.DEFAULT_SHORTCUTS)
		and not cstr(col.default).startswith(":")
	):
		default = cstr(col.default)
	if col.not_nullable and nullable:
		if default is None:
			default = get_not_null_defaults(col.fieldtype)
		nullable = False
	spec = ColumnSpec(col.fieldname, logical, nullable, default)
	# MariaDB exempts only text/longtext from UNIQUE and indexes (not json); follow the same rule
	keyable = logical not in ("text", "longtext")
	spec.unique = bool(col.unique) and keyable
	spec.index = bool(col.set_index) and keyable
	return spec


# --- table statements -----------------------------------------------------------------------------------------------
def index_statement(
	table: str, index_name: str, spec_or_fields, unique: bool = False, if_not_exists: bool = False
) -> str:
	fields = (
		[spec_or_fields.index_field()] if isinstance(spec_or_fields, ColumnSpec) else list(spec_or_fields)
	)
	return (
		f"DEFINE INDEX {'IF NOT EXISTS ' if if_not_exists else ''}{quote(physical(index_name))} ON {quote_table(table)} "
		f"FIELDS {', '.join(quote(f) for f in fields)}{' UNIQUE' if unique else ''}"
	)


def name_spec(autoname: str | None) -> ColumnSpec:
	if autoname == "autoincrement":
		return ColumnSpec("name", "bigint(20)", nullable=False)
	if autoname == "UUID":
		return ColumnSpec("name", "uuid", nullable=False)
	# ADR 0001: a document name is 1..140 characters
	return ColumnSpec(
		"name",
		"varchar(140)",
		nullable=False,
		custom_assert="string::len($value) >= 1 AND string::len($value) <= 140",
	)


def create_statements(
	table: str, columns: list[ColumnSpec], *, autoname=None, is_child=False, sort_modified=False
) -> list[str]:
	"""All statements to create a DocType table (ADR 0001 layout)."""
	stmts = [f"DEFINE TABLE {quote_table(table)} SCHEMAFULL"]
	nm = name_spec(autoname)
	nm.order = 0
	stmts.append(nm.define_field(table))
	stmts += nm.define_shadows(table)
	base = [ColumnSpec(n, t, nul, d) for n, t, nul, d in DEFAULT_COLUMNS]
	child = [ColumnSpec(n, t) for n, t in CHILD_COLUMNS] if is_child else []
	for position, spec in enumerate([*base, *columns, *child], start=1):
		spec.order = position
		stmts.append(spec.define_field(table))
		stmts += spec.define_shadows(table)

	# MariaDB's PRIMARY KEY on `name`. The record id already makes names unique, but a lookup by `name@ci` (every join on a document
	# name - Link fields, `parent`, `tabX.name = child.link` - and every `name IN (...)`) is a TableScan without an index: measured
	# 57 ms per lookup on 5,178 rows vs 0.2 ms with it (findings/P1.6-builder.md section 9).
	indexes = [("PRIMARY", [nm.index_field()], True)]
	if is_child:
		indexes.append(("parent", ["parent" + SHADOW_CI], False))
	else:
		indexes.append(("creation", ["creation"], False))
		if sort_modified:
			indexes.append(("modified", ["modified"], False))
	for spec in columns:
		if spec.unique:
			indexes.append((spec.name, [spec.index_field()], True))
		elif spec.index:
			indexes.append((spec.name, [spec.index_field()], False))
	seen = set()
	for name, fields, unique in indexes:
		if name in seen:
			continue
		seen.add(name)
		stmts.append(index_statement(table, name, fields, unique))
	return stmts


class SurrealDBColumn(DbColumn):
	"""Kept as a hook: `DBTable.get_columns_from_docfields` builds base `DbColumn`s, which is all the alter logic needs."""


class SurrealDBTable(DBTable):
	def specs(self) -> list[ColumnSpec]:
		out = []
		std = set(frappe.db.DEFAULT_COLUMNS)
		for fieldname, col in self.columns.items():
			if fieldname in std:
				continue
			if (spec := column_spec_from_docfield(col)) is not None:
				if spec.is_text and text_shadows.is_shadowed(self.table_name, fieldname):
					spec.text_collation_shadow = True
				out.append(spec)
		return out

	def statements(self) -> list[str]:
		"""Every statement that creates this DocType's table (also used by the verification script, without a database)."""
		istable = cint(self.meta.get("istable", default=0))
		autoname = None if self.meta.issingle else self.meta.autoname
		return create_statements(
			self.table_name,
			self.specs(),
			autoname=autoname,
			is_child=bool(istable),
			sort_modified=(not istable and self.meta.sort_field == "modified"),
		)

	def create(self):
		for stmt in self.statements():
			frappe.db.sql_ddl(stmt)
		if not self.meta.issingle and self.meta.autoname == "autoincrement":
			frappe.db.create_sequence(self.doctype, check_not_exists=True)

	def validate(self):
		"""Refuse silent truncation when a varchar gets shorter (MariaDB's check, in SurrealQL)."""
		if self.is_new():
			return
		self.setup_table_columns()
		columns = [
			frappe._dict({"fieldname": f, "fieldtype": "Data"}) for f in frappe.db.STANDARD_VARCHAR_COLUMNS
		]
		if self.meta.get("istable"):
			columns += [
				frappe._dict({"fieldname": f, "fieldtype": "Data"}) for f in frappe.db.CHILD_TABLE_COLUMNS
			]
		columns += self.columns.values()
		for col in columns:
			if len(col.fieldname) >= 64:
				frappe.throw(
					_("Fieldname is limited to 64 characters ({0})").format(frappe.bold(col.fieldname))
				)
			logical = get_definition(
				col.fieldtype, precision=getattr(col, "precision", None), length=getattr(col, "length", None)
			)
			if not logical or not logical.startswith("varchar"):
				continue
			new_length = cint(getattr(col, "length", None)) or cint(frappe.db.VARCHAR_LEN)
			if not (1 <= new_length <= 1000):
				frappe.throw(_("Length of {0} should be between 1 and 1000").format(col.fieldname))
			current = self.current_columns.get(col.fieldname.lower())
			if not current or not current["type"].startswith("varchar"):
				continue
			current_length = cint(re.findall(r"varchar\((\d+)\)", current["type"])[0])
			if current_length > new_length:
				rows = frappe.db.sql(
					f"SELECT math::max(string::len({quote(physical(col.fieldname))})) AS m FROM {quote_table(self.table_name)} GROUP ALL /*cols:m*/"
				)
				if rows and rows[0][0] and rows[0][0] > new_length:
					if col.fieldname in self.columns:
						self.columns[col.fieldname].length = current_length
					frappe.msgprint(
						_(
							"Reverting length to {0} for '{1}' in '{2}'. Setting the length as {3} will cause truncation of data."
						).format(current_length, col.fieldname, self.doctype, new_length)
					)

	def alter(self):
		for col in self.columns.values():
			col.build_for_alter_table(self.current_columns.get(col.fieldname.lower()))
		table = self.table_name
		specs = {s.name: s for s in self.specs()}

		def ddl(stmt):
			frappe.db.sql_ddl(stmt)

		# new columns: MariaDB fills existing rows with the default; SurrealDB does not, so backfill NOT NULL columns
		for col in self.add_column:
			spec = specs.get(col.fieldname)
			if spec is None:
				continue
			spec.order = 1 + max(
				(c.get("o", 0) for c in frappe.db.table_info(table).columns.values()), default=0
			)
			ddl(spec.define_field(table))
			for stmt in spec.define_shadows(table):
				ddl(stmt)
			if not spec.nullable and spec.default is not None:
				backfill_default(table, spec)

		for col in {*self.change_type, *self.set_default, *self.change_nullability}:
			spec = specs.get(col.fieldname)
			if spec is None:
				continue
			current = self.current_columns.get(col.fieldname.lower())
			convert_column(
				table,
				spec,
				current["type"] if current else spec.logical,
				redefine=redefine_statements(spec, table),
			)
			if not spec.nullable and spec.default is not None:
				backfill_default(table, spec, only_null=True)

		for col in self.add_unique:
			if spec := specs.get(col.fieldname):
				self._run(index_statement(table, col.fieldname, spec, unique=True, if_not_exists=True))
		for col in self.add_index:
			if spec := specs.get(col.fieldname):
				if not frappe.db.get_column_index(table, col.fieldname, unique=False):
					self._run(index_statement(table, f"{col.fieldname}_index", spec, if_not_exists=True))
		if self.meta.sort_field == "modified" and not frappe.db.get_column_index(
			table, "modified", unique=False
		):
			self._run(index_statement(table, "modified", ["modified"], if_not_exists=True))

		# unique constraints of fields removed from the doctype are dropped, like MariaDB's alter does
		meta_columns, db_columns = set(self.columns), set(self.current_columns)
		for name in db_columns - meta_columns:
			if name in frappe.db.DEFAULT_COLUMNS or name in frappe.db.OPTIONAL_COLUMNS or is_shadow(name):
				continue
			if frappe.db.get_column_index(table, name, unique=True):
				self.drop_unique.append(SimpleColumn(name, unique=False, set_index=False))

		for col in {*self.drop_index, *self.drop_unique}:
			if col.fieldname == "name":
				continue
			current = self.current_columns.get(col.fieldname.lower())
			if current is None:
				continue
			if current.unique != col.unique and not col.unique:
				if unique_index := frappe.db.get_column_index(table, col.fieldname, unique=True):
					ddl(
						f"REMOVE INDEX IF EXISTS {quote(physical(unique_index.Key_name))} ON {quote_table(table)}"
					)
			if current.index != col.set_index and not col.set_index:
				if index_record := frappe.db.get_column_index(table, col.fieldname, unique=False):
					ddl(
						f"REMOVE INDEX IF EXISTS {quote(physical(index_record.Key_name))} ON {quote_table(table)}"
					)

	def _run(self, statement: str):
		try:
			frappe.db.sql_ddl(statement)
		except Exception as e:
			if frappe.db.is_duplicate_entry(e):
				fieldname = str(e).split("'")[-2]
				frappe.throw(
					_(
						"{0} field cannot be set as unique in {1}, as there are non-unique existing values"
					).format(fieldname, self.table_name)
				)
			raise


@dataclass
class SimpleColumn:
	fieldname: str
	unique: bool = False
	set_index: bool = False


def backfill_default(table: str, spec: ColumnSpec, only_null: bool = False, db=None):
	"""Give existing rows the column default (MariaDB does this for `ADD COLUMN ... NOT NULL DEFAULT x` and when a
	column becomes NOT NULL). Rows that lack the field, or hold NULL, get the encoded default."""
	stored = quote(physical(spec.name))
	sets = [f"{stored} = {spec.default_literal()}"]
	if spec.is_varchar:
		sets.append(
			f"{quote(physical(spec.name) + SHADOW_CI)} = {surql_string(collation.ci_key(cstr(spec.default)))}"
		)
		sets.append(
			f"{quote(physical(spec.name) + SHADOW_LIKE)} = {surql_string(collation.like_shadow(cstr(spec.default)))}"
		)
	cond = f"{stored} = NONE OR {stored} = NULL"
	(db or frappe.db).sql_ddl(f"UPDATE {quote_table(table)} SET {', '.join(sets)} WHERE {cond}")


def redefine_statements(spec: ColumnSpec, table: str) -> list[str]:
	return [spec.define_field(table, overwrite=True), *spec.define_shadows(table, overwrite=True)]


def convert_column(
	table: str, spec: ColumnSpec, old_logical: str, db=None, redefine: list[str] | None = None
):
	"""Change a column's definition, re-encoding existing values when the storage kind changes (MariaDB `MODIFY`
	converts the data or fails). Order matters: values are read and converted first (nothing is altered if one cannot
	be), then the field is redefined, then the converted values are written - the old type would reject them."""
	db = db or frappe.db
	redefine = redefine if redefine is not None else redefine_statements(spec, table)
	old = ColumnSpec(spec.name, old_logical)
	converted = []
	if old.kind != spec.kind:
		stored = quote(physical(spec.name))
		rows = db.sql(
			f"SELECT record::id(id) AS rid, {stored} AS v FROM {quote_table(table)} WHERE {stored} != NONE AND {stored} != NULL /*cols:rid,v*/"
		)
		for rid, v in rows:
			try:
				converted.append((rid, encode_for_kind(spec, v)))
			except values.ValueError_:
				frappe.throw(
					_(
						"Cannot change field type in {0}: some existing values cannot be converted to the new type"
					).format(table),
					title=_("Incompatible Values"),
				)
	for stmt in redefine:
		db.sql_ddl(stmt)
	for rid, new in converted:
		sets = [f"{quote(physical(spec.name))} = $v"]
		params = {"tb": table, "rid": rid, "v": new}
		if spec.is_varchar and new is not None:
			sets += [
				f"{quote(physical(spec.name) + SHADOW_CI)} = $ci",
				f"{quote(physical(spec.name) + SHADOW_LIKE)} = $lk",
			]
			params |= {"ci": collation.ci_key(new), "lk": collation.like_shadow(new)}
		db.sql(f"UPDATE type::record($tb, $rid) SET {', '.join(sets)}", params)


def encode_for_kind(spec: ColumnSpec, value):
	"""Encode a Python value for storage in a column of this spec (used by conversions and by the translator)."""
	if value is None:
		return None
	k = spec.kind
	if k in ("int", "tinyint", "smallint", "bigint"):
		return values.to_int(value, "int" if k == "int" else k)
	if k == "decimal":
		precision, scale = precision_scale(spec.arg)
		return values.to_decimal(value, precision, scale)
	if k == "date":
		return values.to_date(value)
	if k == "datetime":
		return values.to_datetime(value)
	if k == "time":
		return values.to_time_us(value)
	return values.to_str(value)


def decode_for_kind(spec: ColumnSpec, value):
	return decode_kind(spec.kind, value)


def decode_kind(k: str, value):
	"""Stored value -> what MariaDB's driver returns to Frappe (dates/datetimes as objects, decimals as float)."""
	import datetime as dt

	if value is None:
		return None
	if k == "date":
		if value == "0000-00-00":
			return None
		return dt.date.fromisoformat(value)
	if k == "datetime":
		if value.startswith("0000-00-00"):
			return None
		return dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")
	if k == "time":
		return values.from_time_us(value)
	if k == "decimal":
		return float(value) if isinstance(value, Decimal) else value
	return value


# --- system tables -------------------------------------------------------------------------------------------------
# Primary/unique key of each system table: the record id is a hash of the collation keys of these columns (the tables have no `name`
# key of their own: `__Auth.name` repeats for every field of a document)
SYSTEM_KEYS = {
	"__Auth": ("doctype", "name", "fieldname"),
	"__global_search": ("doctype", "name"),
	"__UserSettings": ("user", "doctype"),
	"tabSessions": ("sid",),
	"tabSingles": ("doctype", "field"),
}


def system_record_id(table: str, row: dict) -> str:
	"""Record id of a system-table row from its key columns (`row` maps column -> stored value)."""
	parts = []
	for column in SYSTEM_KEYS[table]:
		value = row.get(column)
		if value is None:
			raise SurrealDBProgrammingError(1048, f"Column '{column}' cannot be null")
		parts.append(collation.ci_key(values.to_str(value)))
	return "~" + hashlib.blake2b(json.dumps(parts).encode(), digest_size=16).hexdigest()


def system_table_statements(name: str) -> list[str]:
	"""Tables Frappe creates outside DocType sync (`create_auth_table` etc.), same columns as MariaDB's."""
	if name == "__Auth":
		cols = [
			ColumnSpec("doctype", "varchar(140)", False),
			ColumnSpec("name", "varchar(255)", False),
			ColumnSpec("fieldname", "varchar(140)", False),
			ColumnSpec("password", "text", False),
			ColumnSpec("encrypted", "tinyint(4)", False, 0),
		]
		unique = ("PRIMARY", ["doctype", "name", "fieldname"])
	elif name == "__global_search":
		cols = [
			ColumnSpec("doctype", "varchar(100)"),
			ColumnSpec("name", "varchar(140)"),
			ColumnSpec("title", "varchar(140)"),
			ColumnSpec("content", "text"),
			ColumnSpec("route", "varchar(140)"),
			ColumnSpec("published", "tinyint(4)", False, 0),
		]
		unique = ("doctype_name", ["doctype", "name"])
	elif name == "__UserSettings":
		cols = [
			ColumnSpec("user", "varchar(180)", False),
			ColumnSpec("doctype", "varchar(180)", False),
			ColumnSpec("data", "text"),
		]
		unique = ("user", ["user", "doctype"])
	elif name == "tabSingles":
		cols = [
			ColumnSpec("doctype", "varchar(180)", False),
			ColumnSpec("field", "varchar(180)", False),
			ColumnSpec("value", "longtext", True),
		]
		unique = ("PRIMARY", ["doctype", "field"])
	elif name == "tabSeries":
		# tabSeries is created outside DocType sync too (MariaDB: framework SQL), but unlike the other
		# system tables it HAS a `name` column: the record id is the document-style one, and naming.py's
		# `Series` reads/writes it with plain `WHERE name = ...` qb statements.
		cols = [
			ColumnSpec("name", "varchar(100)", False),
			ColumnSpec("current", "int(11)", False, 0),
		]
		unique = ("PRIMARY", ["name"])
	elif name == "tabSessions":
		# The `Session` doctype no longer exists in v16 (MariaDB: framework SQL); sessions.py writes
		# the rows directly through `frappe.qb` (`insert_session_record`). `sid` is the row key.
		cols = [
			ColumnSpec("user", "varchar(255)"),
			ColumnSpec("sid", "varchar(255)"),
			ColumnSpec("sessiondata", "longtext"),
			ColumnSpec("ipaddress", "varchar(16)"),
			ColumnSpec("lastupdate", "datetime(6)"),
			ColumnSpec("status", "varchar(20)"),
		]
		unique = ("sid", ["sid"])
	else:
		raise SurrealDBProgrammingError(0, f"Unknown system table {name!r}")
	stmts = [f"DEFINE TABLE {quote_table(name)} SCHEMAFULL"]
	by_name = {c.name: c for c in cols}
	for c in cols:
		stmts.append(c.define_field(name))
		stmts += c.define_shadows(name)
	stmts.append(index_statement(name, unique[0], [by_name[f].index_field() for f in unique[1]], unique=True))
	return stmts


# --- introspection: INFO FOR TABLE -> columns / indexes ----------------------------------------------------------
_COMMENT = re.compile(r"""COMMENT (?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")
_INDEX = re.compile(
	r"^DEFINE INDEX (?P<name>`[^`]+`|\S+) ON (?P<table>`[^`]+`|\S+) FIELDS (?P<fields>.*?)(?: (?P<kind>UNIQUE|SEARCH|FULLTEXT|MTREE|HNSW)\b.*)?$"
)
_IDENT_TOKEN = re.compile(r"`[^`]*`|[^,\s]+")


def unquote(token: str) -> str:
	token = token.strip()
	if len(token) >= 2 and token[0] == token[-1] == "`":
		return token[1:-1]
	if len(token) >= 2 and token[0] == "\u27e8" and token[-1] == "\u27e9":
		return token[1:-1]
	return token


def parse_field_meta(definition: str) -> dict | None:
	"""The JSON metadata this backend stores in each field's COMMENT (logical type, nullability, default)."""
	m = _COMMENT.search(definition)
	if not m:
		return None
	try:
		return json.loads(unescape(m[1] if m[1] is not None else m[2]))
	except ValueError:
		return None


def parse_index(definition: str) -> dict | None:
	m = _INDEX.match(definition.strip())
	if not m:
		return None
	fields = [unquote(t) for t in _IDENT_TOKEN.findall(m["fields"])]
	return {
		"name": unquote(m["name"]),
		"fields": fields,
		"unique": m["kind"] == "UNIQUE",
		"kind": m["kind"] or "BTREE",
	}


def base_field(stored: str) -> str:
	"""Column an index field belongs to (`name@ci` -> `name`, `id@f` -> `id`)."""
	for suffix in (SHADOW_CI, SHADOW_LIKE):
		if stored.endswith(suffix):
			stored = stored[: -len(suffix)]
	return logical_name(stored)


@dataclass
class TableInfo:
	columns: dict  # logical column name -> meta dict {"t","n","d"}
	indexes: dict  # index name -> {"name","fields","unique","kind"}

	def column_flags(self, name: str) -> tuple[bool, bool]:
		"""(indexed, unique) as MariaDB's information_schema reports them: `index` = first column of a non-unique index,
		`unique` = column of a single-column unique index."""
		indexed = unique = False
		for ix in self.indexes.values():
			if not ix["fields"] or base_field(ix["fields"][0]) != name:
				continue
			if ix["unique"]:
				if len(ix["fields"]) == 1:
					unique = True
			else:
				indexed = True
		return indexed, unique


def parse_table_info(info: dict) -> TableInfo:
	columns = {}
	for key, definition in (info.get("fields") or {}).items():
		stored = unquote(key)
		if is_shadow(stored):
			continue
		if (meta := parse_field_meta(definition)) is not None:
			columns[logical_name(stored)] = meta
	indexes = {}
	for key, definition in (info.get("indexes") or {}).items():
		if (ix := parse_index(definition)) is not None:
			ix["name"] = logical_name(ix["name"])
			indexes[logical_name(unquote(key))] = ix
	return TableInfo(columns, indexes)


# --- table schema for the translator ---------------------------------------------------------------------------------
@dataclass
class TableSchema:
	name: str
	columns: dict  # logical column name -> ColumnSpec, in definition order
	indexes: dict

	def column(self, name: str) -> ColumnSpec | None:
		return self.columns.get(name)

	@property
	def name_kind(self) -> str:
		return self.columns["name"].kind if "name" in self.columns else "varchar"


def table_schema_from_info(table_name: str, info: dict) -> TableSchema:
	parsed = parse_table_info(info)
	ordered = sorted(parsed.columns.items(), key=lambda kv: (kv[1].get("o", 10**6), kv[0]))
	columns = {}
	for order, (name, meta) in enumerate(ordered):
		columns[name] = ColumnSpec(name, meta["t"], nullable=bool(meta["n"]), order=order)
	return TableSchema(table_name, columns, parsed.indexes)


def table_schema(table_name: str, db=None) -> TableSchema:
	"""Schema of a table from `INFO FOR TABLE`, cached per request/process (cleared by every DDL statement)."""
	cache = _schema_cache()
	if (cached := cache.get(table_name)) is not None:
		return cached
	db = db or frappe.db
	schema = table_schema_from_info(table_name, db._info(f"INFO FOR TABLE {quote_table(table_name)}"))
	if not schema.columns:
		# INFO FOR TABLE on a missing table returns an empty structure instead of an error (measured, P1.4)
		raise SurrealDBProgrammingError(1146, f"Table '{table_name}' doesn't exist")
	cache[table_name] = schema
	return schema


def _schema_cache() -> dict:
	try:
		return frappe.local.surrealdb_schemas
	except AttributeError:
		frappe.local.surrealdb_schemas = cache = {}
		return cache


def clear_schema_cache():
	_schema_cache().clear()
