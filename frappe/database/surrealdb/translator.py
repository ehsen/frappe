"""PyPika query objects -> SurrealQL (chunk P1.6, ADR 0003).

The renderer works on the *typed* query tree Frappe builds with `frappe.qb`; it never rewrites SQL text. Anything it does not
understand raises `SurrealDBNotImplementedError` (fail closed): a query must never be silently approximated.

Rules that make results identical to MariaDB (each one is covered by the MariaDB-vs-SurrealDB parity tests):

* **Three-valued logic.** MariaDB drops rows whose predicate is UNKNOWN (a NULL operand); SurrealDB compares NULLs as ordinary
  values (`NULL != 'a'`, `NULL < 1` are true). Negation is pushed down to the leaves (`NOT (a = 1)` -> `a != 1`, De Morgan) and every
  leaf except `=`/`IN`/`LIKE` on a nullable operand carries a NULL/NONE guard, which is exact for WHERE/HAVING.
* **Collation.** A varchar column is compared through its shadow fields (`<col>@ci` for =, IN, <, >, BETWEEN, ORDER BY;
  `<col>@like` for LIKE), built from the literal with the same function that built the stored shadow.
* **Types.** Every literal is encoded for the column it meets (dates as canonical text, times as microseconds, decimals exact).
  String<->number comparisons are not emulated and fail closed.
* **Column order.** Every projection ends with `/*cols:a,b,c*/` (SurrealDB returns object keys alphabetically).
* **Parameters.** Values are always bound (`$paramN`), never interpolated.
"""

import re
from decimal import Decimal, InvalidOperation

from pypika.terms import Node

import frappe
from frappe.database.surrealdb import collation, values
from frappe.database.surrealdb.errors import SurrealDBProgrammingError, unsupported
from frappe.database.surrealdb.schema import (
	SHADOW_CI,
	SHADOW_LIKE,
	ColumnSpec,
	TableSchema,
	encode_for_kind,
	physical,
	precision_scale,
	quote,
	quote_table,
	table_schema,
)

_PLACEHOLDER = re.compile(r"^%\((\w+)\)s$")
_NAME = re.compile(r"^\w+$")
COMPARATORS = {"=": "=", "<>": "!=", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<="}
NEGATED = {"=": "!=", "!=": "=", ">": "<=", ">=": "<", "<": ">=", "<=": ">"}
FLIPPED = {"=": "=", "!=": "!=", ">": "<", ">=": "<=", "<": ">", "<=": ">="}
INT_KINDS = ("int", "tinyint", "smallint", "bigint")
NUMERIC_KINDS = frozenset((*INT_KINDS, "decimal"))


def _attr(query, name, default=None):
	"""Instance attribute of a query builder. `getattr` cannot be used: Frappe patches `Selectable.__getattr__` to return a
	`Field` for any missing attribute, which would make every optional feature look present."""
	return vars(query).get(name, default)


def _kind(term) -> str:
	return type(term).__name__


def _sql_word(comparator) -> str:
	return str(getattr(comparator, "value", comparator)).strip().lower()


class Params:
	"""Collects bound values. Uses Frappe's `NamedParameterWrapper` when `prepare_query` supplies one, so the values reach
	`frappe.db.sql` through the normal path; otherwise keeps its own dict (used by tests and debugging)."""

	def __init__(self, wrapper=None):
		self.wrapper = wrapper
		self.values: dict = {}

	def add(self, value) -> str:
		if self.wrapper is not None:
			placeholder = self.wrapper.get_sql(value)
			match = _PLACEHOLDER.match(placeholder)
			if not match:
				raise SurrealDBProgrammingError(0, f"Unexpected parameter placeholder {placeholder!r}")
			return "$" + match[1]
		name = f"param{len(self.values) + 1}"
		self.values[name] = value
		return "$" + name


class Renderer:
	def __init__(self, params: Params | None = None, schema_loader=None):
		self.params = params or Params()
		self.schema_loader = schema_loader or table_schema
		self.table: TableSchema | None = None
		self.table_names: set[str] = set()

	# --- entry -----------------------------------------------------------------------------------------------------
	def render(self, q) -> str:
		if _attr(q, "_insert_table") is not None:
			return self.insert(q)
		if _attr(q, "_update_table") is not None:
			return self.update(q)
		if _attr(q, "_delete_from", False):
			return self.delete(q)
		return self.select(q)

	def _reject(self, q, *names):
		for name in names:
			value = _attr(q, name)
			if value:
				unsupported(f"query feature {name.lstrip('_')}", "P1.6")

	# --- tables and columns ------------------------------------------------------------------------------------------
	def _bind_table(self, table) -> TableSchema:
		if _kind(table) != "Table":
			unsupported(f"a {_kind(table)} in FROM (joins, subqueries)", "P1.6")
		name = table._table_name
		if getattr(table, "_schema", None) is not None:
			unsupported("a schema-qualified table", "P1.6")
		self.table = self.schema_loader(name)
		self.table_names = {name, getattr(table, "alias", None)} - {None}
		return self.table

	def _column(self, field) -> tuple[str, ColumnSpec]:
		if _kind(field) != "Field":
			unsupported(f"a {_kind(field)} where a column is required", "P1.6")
		owner = getattr(field, "table", None)
		if owner is not None and getattr(owner, "_table_name", None) not in self.table_names | {
			getattr(owner, "alias", None)
		}:
			unsupported("a column of another table (joins)", "P1.6")
		spec = self.table.column(field.name)
		if spec is None:
			# what MariaDB says for an unknown column; SurrealDB would silently select NULL (H2)
			raise SurrealDBProgrammingError(1054, f"Unknown column '{field.name}' in 'field list'")
		return field.name, spec

	@staticmethod
	def _is_field(term) -> bool:
		return _kind(term) == "Field"

	@staticmethod
	def _literal(term):
		"""(True, python value) for a constant, else (False, term)."""
		if hasattr(term, "value") and _kind(term).endswith("ValueWrapper"):
			value = term.value
			return (
				(False, value) if isinstance(value, Node) else (True, value)
			)  # PyPika may wrap a term in a wrapper
		if _kind(term) == "NullValue":
			return True, None
		return False, term

	def _guard(self, ref: str) -> str:
		return f"{ref} != NULL AND {ref} != NONE"

	# --- literals for a column -------------------------------------------------------------------------------------------
	def _operand(self, spec: ColumnSpec, value):
		"""(stored field, bound placeholder) to compare column `spec` with the Python `value`."""
		name = physical(spec.name)
		if spec.is_varchar:
			if isinstance(value, bool | int | float | Decimal):
				unsupported(
					"comparing a string column with a number (MariaDB converts the column to a number)",
					"P1.6",
				)
			return quote(name + SHADOW_CI), self.params.add(collation.ci_key(values.to_str(value)))
		if spec.kind in INT_KINDS or spec.kind == "decimal":
			return quote(name), self.params.add(self._number(spec, value))
		if spec.kind in ("date", "datetime", "time", "uuid"):
			if spec.kind == "date" and isinstance(value, str) and re.search(r"[ T]\d{1,2}:\d", value):
				unsupported("comparing a date column with a datetime literal", "P1.6")
			try:
				encoded = encode_for_kind(
					spec, value.lower() if spec.kind == "uuid" and isinstance(value, str) else value
				)
			except values.ValueError_ as e:
				unsupported(f"an invalid {spec.kind} literal ({e})", "P1.6")
			return quote(name), self.params.add(encoded)
		unsupported(f"comparing a {spec.logical} column (long text needs the collation extension)", "P1.6")

	@staticmethod
	def _number(spec: ColumnSpec, value):
		if isinstance(value, bool):
			return int(value)
		if isinstance(value, int | Decimal):
			return value
		try:
			d = Decimal(str(value).strip())
		except InvalidOperation:
			unsupported(
				"comparing a number column with a non-numeric string (MariaDB converts it to 0)", "P1.6"
			)
		if not d.is_finite():
			unsupported("a non-finite number literal", "P1.6")
		if spec.kind in INT_KINDS and d == d.to_integral_value():
			return int(d)
		return d

	# --- predicates (WHERE / HAVING / ON), negation pushed to the leaves ---------------------------------------------------
	def predicate(self, term, negate: bool = False) -> str:
		kind = _kind(term)
		if kind == "ComplexCriterion":
			op = _sql_word(term.comparator)
			if op not in ("and", "or"):
				unsupported(f"the boolean operator {op}", "P1.6")
			flipped = {"and": "or", "or": "and"}
			joiner = flipped[op] if negate else op
			return (
				f"({self.predicate(term.left, negate)} {joiner.upper()} {self.predicate(term.right, negate)})"
			)
		if kind == "Not":
			return self.predicate(term.term, not negate)
		if kind == "BasicCriterion":
			return self._basic(term, negate)
		if kind == "ContainsCriterion":
			return self._contains(term, negate)
		if kind in ("NullCriterion", "NotNullCriterion"):
			_, spec = self._column(term.term)
			ref = quote(physical(spec.name))
			is_null = (kind == "NullCriterion") != negate
			return f"({ref} = NULL OR {ref} = NONE)" if is_null else f"({self._guard(ref)})"
		if kind == "BetweenCriterion":
			return self._between(term, negate)
		if kind.endswith("ValueWrapper"):
			truth = bool(term.value)
			return "true" if truth != negate else "false"
		unsupported(f"the predicate {kind}", "P1.6")

	def _basic(self, term, negate: bool) -> str:
		op = _sql_word(term.comparator)
		if op in ("like", "not like"):
			return self._like(term, negate, invert=(op == "not like"))
		if op not in COMPARATORS:
			unsupported(f"the comparator {op!r}", "P1.6")
		op = COMPARATORS[op]
		if negate:
			op = NEGATED[op]
		left, right = term.left, term.right
		if self._is_field(right) and not self._is_field(left):
			left, right, op = right, left, FLIPPED[op]
		if not self._is_field(left):
			unsupported("a comparison without a column operand", "P1.6")
		if self._is_field(right):
			return self._compare_columns(op, left, right)
		is_literal, value = self._literal(right)
		if not is_literal:
			unsupported(f"comparing a column with a {_kind(right)}", "P1.6")
		if value is None:
			return "false"  # `col = NULL` is UNKNOWN in MariaDB: never true, and neither is its negation
		_, spec = self._column(left)
		stored, operand = self._operand(spec, value)
		core = f"{stored} {op} {operand}"
		if op == "=":
			return f"({core})"
		return f"({self._guard(stored)} AND {core})"

	def _compare_columns(self, op, left, right) -> str:
		(_, a), (_, b) = self._column(left), self._column(right)
		if a.is_varchar and b.is_varchar:
			ra, rb = quote(physical(a.name) + SHADOW_CI), quote(physical(b.name) + SHADOW_CI)
		elif (a.kind == b.kind or {a.kind, b.kind} <= NUMERIC_KINDS) and a.kind not in ("text", "json"):
			ra, rb = quote(physical(a.name)), quote(physical(b.name))
		else:
			unsupported(f"comparing a {a.logical} column with a {b.logical} column", "P1.6")
		return f"({self._guard(ra)} AND {self._guard(rb)} AND {ra} {op} {rb})"

	def _like(self, term, negate: bool, invert: bool) -> str:
		if not self._is_field(term.left):
			unsupported("LIKE without a column on the left", "P1.6")
		is_literal, pattern = self._literal(term.right)
		if not is_literal or not isinstance(pattern, str):
			unsupported("LIKE with a pattern that is not a string constant", "P1.6")
		_, spec = self._column(term.left)
		if not spec.is_varchar:
			unsupported(f"LIKE on a {spec.logical} column (needs a shadow: long text)", "P1.6")
		escape = getattr(term, "escape", None)
		escape_char = "\\" if escape is None else self._literal(escape)[1]
		if not isinstance(escape_char, str) or len(escape_char) != 1:
			unsupported("LIKE with an ESCAPE that is not a single character", "P1.6")
		shadow = quote(physical(spec.name) + SHADOW_LIKE)
		match = f"string::matches({shadow}, {self.params.add(collation.like_regex(pattern, escape_char))})"
		positive = invert == negate  # NOT LIKE under NOT is LIKE
		return f"({self._guard(shadow)} AND {match if positive else f'NOT ({match})'})"

	def _contains(self, term, negate: bool) -> str:
		if _kind(term.term) != "Field":
			unsupported("IN without a column on the left", "P1.6")
		container = term.container
		if _kind(container) != "Tuple":
			unsupported("IN (subquery)", "P1.6")
		negate = negate != bool(getattr(term, "_is_negated", False))
		_, spec = self._column(term.term)
		items = []
		for item in container.values:
			is_literal, value = self._literal(item)
			if not is_literal:
				unsupported("IN with a non-constant element", "P1.6")
			if value is None:
				unsupported("IN with NULL (three-valued result)", "P1.6")
			items.append(value)
		if not items:
			return "true" if negate else "false"
		stored = None
		operands = []
		for value in items:
			stored, operand = self._operand(spec, value)
			operands.append(operand)
		membership = f"{stored} IN [{', '.join(operands)}]"
		return f"({self._guard(stored)} AND NOT ({membership}))" if negate else f"({membership})"

	def _between(self, term, negate: bool) -> str:
		if not self._is_field(term.term):
			unsupported("BETWEEN without a column", "P1.6")
		_, spec = self._column(term.term)
		bounds = []
		for bound in (term.start, term.end):
			is_literal, value = self._literal(bound)
			if not is_literal or value is None:
				unsupported("BETWEEN with a non-constant or NULL bound", "P1.6")
			bounds.append(self._operand(spec, value))
		(stored, lo), (_, hi) = bounds
		if negate:
			return f"({self._guard(stored)} AND ({stored} < {lo} OR {stored} > {hi}))"
		return f"({self._guard(stored)} AND {stored} >= {lo} AND {stored} <= {hi})"

	# --- SELECT ------------------------------------------------------------------------------------------------------
	def select(self, q) -> str:
		self._reject(
			q, "_joins", "_union", "_for_update", "_distinct", "_havings", "_groupbys", "_with", "_prewheres"
		)
		if len(q._from) != 1:
			unsupported("a SELECT without exactly one table", "P1.6")
		schema = self._bind_table(q._from[0])
		projection, names, sources = self._projection(q._selects, schema)
		parts = [f"SELECT {', '.join(projection)} FROM {quote_table(schema.name)}"]
		if q._wheres is not None:
			parts.append(f"WHERE {self.predicate(q._wheres)}")
		if q._orderbys:
			# SurrealDB only orders by selected expressions ("Missing order idiom"): project each under a hidden alias, which the
			# `/*cols:*/` hint leaves out of the result
			ordering = [self._order(field, order) for field, order in q._orderbys]
			parts[0] = parts[0].replace(
				" FROM ",
				"".join(f", {expr} AS `__o{i}`" for i, (expr, _) in enumerate(ordering)) + " FROM ",
				1,
			)
			parts.append(
				"ORDER BY " + ", ".join(f"`__o{i}` {direction}" for i, (_, direction) in enumerate(ordering))
			)
		if q._limit is not None:
			parts.append(f"LIMIT {int(q._limit)}")
		if q._offset:
			parts.append(f"START {int(q._offset)}")
		kinds = ",".join(schema.columns[n].kind for n in sources)
		return " ".join(parts) + f" /*cols:{','.join(names)}*/ /*kinds:{kinds}*/"

	def _projection(self, selects, schema: TableSchema):
		projection, names, sources = [], [], []
		for term in selects:
			kind = _kind(term)
			if kind == "Star":
				for name in schema.columns:
					self._add_column(projection, names, name, None)
					sources.append(name)
			elif kind == "Field":
				name, _ = self._column(term)
				self._add_column(projection, names, name, term.alias)
				sources.append(name)
			else:
				unsupported(f"the select term {kind}", "P1.6")
		return projection, names, sources

	@staticmethod
	def _add_column(projection, names, name, alias):
		out = alias or name
		if not _NAME.match(out):
			unsupported(f"the result column name {out!r}", "P1.6")
		stored = physical(name)
		projection.append(quote(stored) if stored == out else f"{quote(stored)} AS {quote(out)}")
		names.append(out)

	def _order(self, field, order) -> tuple[str, str]:
		_, spec = self._column(field)
		stored = physical(spec.name) + (SHADOW_CI if spec.is_varchar else "")
		if spec.kind in ("text", "json"):
			unsupported(f"ORDER BY a {spec.logical} column", "P1.6")
		direction = "DESC" if order is not None and _sql_word(order) == "desc" else "ASC"
		return quote(stored), direction

	# --- writes ----------------------------------------------------------------------------------------------------------
	def _encode(self, spec: ColumnSpec, value):
		"""Python value -> stored value for a column (None stays None: NULL)."""
		if isinstance(value, dict | list | tuple | set):
			unsupported(
				f"a {type(value).__name__} value for column {spec.name!r} (Frappe passes JSON as text)",
				"P1.6",
			)
		try:
			return encode_for_kind(spec, value)
		except values.ValueError_ as e:
			raise SurrealDBProgrammingError(
				1366, f"Incorrect {spec.kind} value for column '{spec.name}': {e}"
			) from e

	def _stored_fields(self, spec: ColumnSpec, encoded) -> dict:
		"""The stored fields a column value produces: the column itself and, for varchar, its two shadows."""
		name = physical(spec.name)
		out = {name: encoded}
		if spec.is_varchar:
			out[name + SHADOW_CI] = None if encoded is None else collation.ci_key(encoded)
			out[name + SHADOW_LIKE] = None if encoded is None else collation.like_shadow(encoded)
		return out

	def _record_key(self, schema: TableSchema, value) -> str:
		kind = schema.name_kind
		if value is None:
			raise SurrealDBProgrammingError(1048, "Column 'name' cannot be null")
		if kind == "varchar":
			return collation.record_id(values.to_str(value))
		return str(values.to_int(value, "bigint")) if kind == "bigint" else str(value).lower()

	def insert(self, q) -> str:
		self._reject(q, "_on_duplicate_key_updates", "_on_conflict", "_select", "_returns", "_with")
		schema = self._bind_table(q._insert_table)
		columns = []
		for c in q._columns:
			name = c.name if _kind(c) == "Field" else str(c)
			if schema.column(name) is None:
				raise SurrealDBProgrammingError(1054, f"Unknown column '{name}' in 'field list'")
			columns.append(schema.column(name))
		if "name" not in [c.name for c in columns]:
			raise SurrealDBProgrammingError(1364, "Field 'name' doesn't have a default value")
		if not q._values:
			unsupported("INSERT without VALUES", "P1.6")
		rows = []
		for row in q._values:
			if len(row) != len(columns):
				raise SurrealDBProgrammingError(1136, "Column count doesn't match value count")
			record = {}
			key = None
			for spec, term in zip(columns, row, strict=True):
				is_literal, value = self._literal(term)
				if not is_literal:
					unsupported("INSERT of an expression", "P1.6")
				encoded = self._encode(spec, value)
				if spec.name == "name":
					key = self._record_key(schema, encoded)
				record.update(self._stored_fields(spec, encoded))
			record["id"] = key
			rows.append(record)
		verb = "INSERT IGNORE INTO" if _attr(q, "_ignore", False) else "INSERT INTO"
		return f"{verb} {quote_table(schema.name)} {self.params.add(rows)} RETURN NONE"

	def _set_value(self, spec: ColumnSpec, term) -> dict | str:
		"""Assignments (`field = expr` strings) that set column `spec` to `term`."""
		name = physical(spec.name)
		is_literal, value = self._literal(term)
		if not is_literal:
			term = value  # a wrapper around a term
		if is_literal:
			fields = self._stored_fields(spec, self._encode(spec, value))
			return [f"{quote(f)} = {self.params.add(v)}" for f, v in fields.items()]
		if _kind(term) == "ArithmeticExpression":
			op = str(getattr(term.operator, "value", term.operator)).strip()
			if (
				op not in ("+", "-", "*")
				or spec.kind not in (*INT_KINDS, "decimal")
				or not self._is_field(term.left)
			):
				unsupported("an arithmetic SET expression other than `numeric column +|-|* number`", "P1.6")
			_, source = self._column(term.left)
			if source.name != spec.name:
				unsupported("an arithmetic SET expression on another column", "P1.6")
			is_literal, operand = self._literal(term.right)
			if not is_literal or operand is None:
				unsupported("an arithmetic SET expression with a non-constant operand", "P1.6")
			ref = quote(name)
			param = self.params.add(self._number(spec, operand))
			# NULL arithmetic raises in SurrealDB and yields NULL in MariaDB
			return [f"{ref} = IF {ref} = NULL OR {ref} = NONE THEN NULL ELSE {ref} {op} {param} END"]
		unsupported(f"the SET value {_kind(term)}", "P1.6")

	def update(self, q) -> str:
		self._reject(q, "_joins", "_from_extra", "_limit", "_orderbys")
		schema = self._bind_table(q._update_table)
		assignments = []
		for field, term in q._updates:
			name = field.name if _kind(field) == "Field" else str(field)
			spec = schema.column(name)
			if spec is None:
				raise SurrealDBProgrammingError(1054, f"Unknown column '{name}' in 'field list'")
			if name == "name":
				unsupported("changing `name` (the record id must change: rename needs a copy)", "P1.6")
			assignments += self._set_value(spec, term)
		sql = f"UPDATE {quote_table(schema.name)} SET {', '.join(assignments)}"
		if q._wheres is not None:
			sql += f" WHERE {self.predicate(q._wheres)}"
		return sql + " RETURN NONE"

	def delete(self, q) -> str:
		self._reject(q, "_joins", "_limit", "_orderbys")
		schema = self._bind_table(q._from[0])
		sql = f"DELETE {quote_table(schema.name)}"
		if q._wheres is not None:
			sql += f" WHERE {self.predicate(q._wheres)}"
		return sql + " RETURN NONE"


def render(query, param_wrapper=None, schema_loader=None):
	"""Render a PyPika query; returns (SurrealQL text, Params)."""
	params = Params(param_wrapper)
	return Renderer(params, schema_loader).render(query), params


__all__ = ["Params", "Renderer", "precision_scale", "render"]
