"""PyPika query objects -> SurrealQL (chunk P1.6, ADR 0003).

The renderer works on the *typed* query tree Frappe builds with `frappe.qb`; it never rewrites SQL text. Anything it does not
understand raises `SurrealDBNotImplementedError` (fail closed): a query must never be silently approximated.

Rules that make results identical to MariaDB (each one is covered by the MariaDB-vs-SurrealDB parity tests):

* **Three-valued logic.** MariaDB drops rows whose predicate is UNKNOWN (a NULL operand); SurrealDB compares NULLs as ordinary
  values (`NULL != 'a'`, `NULL < 1` are true). Negation is pushed down to the leaves (`NOT (a = 1)` -> `a != 1`, De Morgan) and every
  leaf except `=`/`IN`/`LIKE` on a nullable operand carries a NULL/NONE guard, which is exact for WHERE/HAVING.
* **Collation.** A varchar column is compared through its shadow fields (`<col>@ci` for =, IN, <, >, BETWEEN, ORDER BY;
  `<col>@like` for LIKE), built from the literal with the same function that built the stored shadow. Expressions that
  produce strings carry the shadow of their result (`IFNULL(a, 'x')`) or, when it cannot be computed in SurrealQL, cannot be
  compared (fail closed).
* **Types.** Every literal is encoded for the column it meets (dates as canonical text, times as microseconds, decimals exact).
  String<->number comparisons are not emulated and fail closed.
* **Expressions.** Every value expression compiles to an `Expr` (SurrealQL text + a column-like type + collation shadows). NULL
  propagates like MariaDB (SurrealQL raises on `NULL + 1`), division is decimal, ROUND rounds half away from zero (SurrealDB
  rounds to even).
* **Joins.** SurrealDB has no JOIN: a join is a correlated sub-select per left row, flattened (see `_joined_source`).
* **Correlated sub-queries.** A sub-query that reads a column of the outer row is evaluated once per outer row: a
  one-element `array::map` closure carries the referenced outer columns to the sub-query (see `OuterScope`). Correlated
  `EXISTS` answers per row with the number of the inner rows for the binding, and a correlated `IN` carries its left
  operand through the same binding (both take the scalar sub-query's closure shape).
* **Column order.** Every projection ends with `/*cols:a,b,c*/` (SurrealDB returns object keys alphabetically).
* **Parameters.** Values are always bound (`$paramN`), never interpolated.
"""

import datetime as dt
import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import SimpleNamespace
from typing import ClassVar

from pypika.enums import Boolean, Equality, Matching
from pypika.queries import Field, QueryBuilder, Table
from pypika.terms import (
	AggregateFunction,
	BasicCriterion,
	BetweenCriterion,
	ComplexCriterion,
	ContainsCriterion,
	Function,
	Node,
	Not,
	NotNullCriterion,
	NullCriterion,
	NullValue,
	Tuple,
	ValueWrapper,
)

import frappe
from frappe.database.surrealdb import collation, text_shadows, values
from frappe.database.surrealdb.errors import SurrealDBProgrammingError, unsupported
from frappe.database.surrealdb.schema import (
	SHADOW_CI,
	SHADOW_HASH,
	SHADOW_LIKE,
	SYSTEM_KEYS,
	ColumnSpec,
	TableSchema,
	encode_for_kind,
	physical,
	precision_scale,
	quote,
	quote_table,
	system_record_id,
	table_schema,
)

_PLACEHOLDER = re.compile(r"^%\((\w+)\)s$")
_NAME = re.compile(r"^\w+$")
COMPARATORS = {"=": "=", "<>": "!=", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<="}
NEGATED = {"=": "!=", "!=": "=", ">": "<=", ">=": "<", "<": ">=", "<=": ">"}
FLIPPED = {"=": "=", "!=": "!=", ">": "<", ">=": "<=", "<": ">", "<=": ">="}
INT_KINDS = ("int", "tinyint", "smallint", "bigint")
NUMERIC_KINDS = frozenset((*INT_KINDS, "decimal"))
TEMPORAL_KINDS = ("date", "datetime", "time")
EMPTY_STRING_AS = {
	**dict.fromkeys(INT_KINDS, 0),
	"decimal": 0,
	"date": "0000-00-00",
	"datetime": "0000-00-00 00:00:00.000000",
	"time": 0,
}
_NOTHING = object()
P1_6C = "P1.6c"
P1_6D = "P1.6d"


AGGREGATE_NAMES = frozenset(
	"""COUNT SUM AVG MIN MAX GROUP_CONCAT STD STDDEV STDDEV_POP STDDEV_SAMP VARIANCE VAR_POP VAR_SAMP BIT_AND BIT_OR BIT_XOR
	JSON_ARRAYAGG JSON_OBJECTAGG STRING_AGG""".split()
)


def _is_aggregate(term) -> bool:
	"""PyPika derives `Abs` from `AggregateFunction`, so the class alone does not tell an aggregate from a scalar function."""
	return isinstance(term, AggregateFunction) and str(term.name).strip().upper() in AGGREGATE_NAMES


def _is_wrapper_of_term(term) -> bool:
	return hasattr(term, "value") and _kind(term).endswith("ValueWrapper") and isinstance(term.value, Node)


def _attr(query, name, default=None):
	"""Instance attribute of a query builder. `getattr` cannot be used: Frappe patches `Selectable.__getattr__` to return a
	`Field` for any missing attribute, which would make every optional feature look present."""
	return vars(query).get(name, default)


def _kind(term) -> str:
	return type(term).__name__


def _query_of(container):
	"""Frappe's `SubQuery` criterion wraps the real `QueryBuilder` (note.py's unseen-notes login check and
	listview.py's ToDo filter build their sub-queries through it); accept the wrapper wherever a bare
	sub-query is expected."""
	return container.subq if _kind(container) == "SubQuery" else container


def _sql_word(comparator) -> str:
	return str(getattr(comparator, "value", comparator)).strip().lower()


def _present(sql: str) -> str:
	return f"{sql} != NULL AND {sql} != NONE"


def _absent(sql: str) -> str:
	return f"({sql} = NULL OR {sql} = NONE)"


def _synthetic(kind: str, scale: int = 0) -> ColumnSpec:
	"""A column-like type for an expression result (drives literal encoding and the result decoder)."""
	logical = {
		"int": "bigint",
		"bigint": "bigint",
		"decimal": f"decimal(65,{scale})",
		"varchar": "varchar(65535)",
		"text": "longtext",
	}.get(kind, kind)
	return ColumnSpec("<expr>", logical)


def _scale_of(spec: ColumnSpec | None, const=_NOTHING) -> int:
	if const is not _NOTHING:
		if isinstance(const, bool | int):
			return 0
		exponent = Decimal(str(const)).as_tuple().exponent
		return max(-exponent, 0) if isinstance(exponent, int) else 0
	if spec is None or spec.kind in INT_KINDS:
		return 0
	return precision_scale(spec.arg)[1] if spec.kind == "decimal" else 0


@dataclass
class Expr:
	"""A compiled value expression: SurrealQL text, its type, and (collation-shadow columns only) the
	collation shadows of its value. `hash` carries the @hash witness reference of a plain shadowed-text
	column, so a column-to-column copy can move it; computed strings have none (fail closed)."""

	sql: str | None
	spec: ColumnSpec | None
	ci: str | None = None
	like: str | None = None
	hash: str | None = None
	const: object = _NOTHING
	opaque_string: bool = False  # `IFNULL(non_string, '')`: only comparable with '' (see `_ifnull`)

	@property
	def is_const(self) -> bool:
		return self.const is not _NOTHING

	@property
	def kind(self) -> str:
		return self.spec.kind if self.spec is not None else "null"


@dataclass
class TableCtx:
	alias: str  # key of the table in a joined row (`t0`, `t1`)
	schema: TableSchema
	table: object  # the PyPika Table
	prefix: str = ""  # how a stored field of this table is reached from where the expression is evaluated


def _attr_name(value) -> str:
	return str(getattr(value, "value", value)).strip()


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


class _Shared:
	"""State shared by a statement and its sub-queries."""

	def __init__(self):
		self.prelude: list[str] = []  # `LET $sqN = (...)` statements that run before the statement
		self.counter = 0
		self.now: str | None = None


class _CommaJoin:
	"""A synthesized `JOIN ... ON` step for Frappe's legacy comma joins (`how` "" - an INNER join)."""

	def __init__(self, item, criterion):
		self.item = item
		self.criterion = criterion
		self.how = ""


_FRAG_OP = {
	"=": Equality.eq,
	"!=": Equality.ne,
	"<>": Equality.ne,
	"<": Equality.lt,
	">": Equality.gt,
	"<=": Equality.lte,
	">=": Equality.gte,
}


# The SQL functions a raw fragment may call: exactly the ones the ordinary expression machinery
# maps (chunk P1.6d.2) - everything else keeps the fragment parser's fail-closed 1054.
_FRAG_FUNCTIONS = frozenset("IFNULL COALESCE CONCAT CONCAT_WS NULLIF ROUND TRUNCATE".split())


class _FragmentParser:
	"""Recursive-descent parser for the raw SQL fragments Frappe embeds as `RawCriterion` /
	`CombinedRawCriterion` (permission query conditions, `build_match_conditions`, ad-hoc
	`.where(RawCriterion(...))`). Chunk P1.6d.

	The fragment text is parsed into a PyPika criterion tree which the ordinary `predicate()` then
	renders, so every leaf goes through the same BasicCriterion machinery as a typed query - the
	same bound values, collation shadows, NULL guards and fail-closed rules. MariaDB syntax the
	parser does not know (sub-queries, EXISTS, arithmetic, %s placeholders) raises; mapped functions parse too (P1.6d.2); a
	fragment is never approximated."""

	_TOKEN = re.compile(
		r"""[ \t\r\n]+
		|`(?P<backtick>[^`]+)`
		|'(?P<sq>(?:[^'\\]|\\.|'')*)'
		|"(?P<dquote>(?:[^"\\]|\\.)*)"
		|(?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
		|(?P<word>[A-Za-z_@][A-Za-z0-9_$@]*)
		|(?P<op><=|>=|!=|<>|=|<|>)
		|(?P<paren>[(),.])
		""",
		re.VERBOSE,
	)

	def __init__(self, renderer, text: str):
		self.r = renderer
		self.toks = []
		pos = 0
		while pos < len(text):
			m = self._TOKEN.match(text, pos)
			if m is None:
				unsupported(f"the SQL fragment character {text[pos]!r}", P1_6D)
			pos = m.end()
			kind = m.lastgroup
			if kind is not None:  # whitespace has no named group
				self.toks.append((kind, m.group(kind)))
		self.i = 0

	def _peek(self) -> tuple:
		return self.toks[self.i] if self.i < len(self.toks) else (None, None)

	def _punct(self, text: str) -> bool:
		kind, token = self._peek()
		if kind == "paren" and token == text:
			self.i += 1
			return True
		return False

	def _kw(self, *words: str) -> bool:
		kind, token = self._peek()
		if kind == "word" and token.lower() in words:
			self.i += 1
			return True
		return False

	def _name(self) -> str:
		kind, token = self._peek()
		if kind not in ("backtick", "word"):
			unsupported("an identifier at the end of the SQL fragment", P1_6D)
		self.i += 1
		return token

	def parse(self):
		node = self._or()
		if self.i != len(self.toks):
			unsupported(f"a trailing {self.toks[self.i][1]!r} in the SQL fragment", P1_6D)
		return node

	def _or(self):
		node = self._and()
		while self._kw("or"):
			node = ComplexCriterion(Boolean.or_, node, self._and())
		return node

	def _and(self):
		node = self._not()
		while self._kw("and"):
			node = ComplexCriterion(Boolean.and_, node, self._not())
		return node

	def _not(self):
		if self._kw("not"):
			return Not(self._not())
		return self._primary()

	def _primary(self):
		if self._punct("("):
			node = self._or()
			if not self._punct(")"):
				unsupported("a missing ')' in the SQL fragment", P1_6D)
			return node
		return self._comparison()

	def _comparison(self):
		left = self._operand()
		kind, token = self._peek()
		if kind == "op":
			self.i += 1
			return BasicCriterion(_FRAG_OP[token], left, self._operand())
		if kind == "word":
			up = token.upper()
			if up == "LIKE":
				self.i += 1
				return BasicCriterion(Matching.like, left, self._operand())
			if up == "IS":
				self.i += 1
				negated = self._kw("not")
				if not self._kw("null"):
					unsupported("IS without NULL in the SQL fragment", P1_6D)
				return NotNullCriterion(left) if negated else NullCriterion(left)
			if up == "IN":
				self.i += 1
				return self._in(left)
			if up == "BETWEEN":
				self.i += 1
				lo = self._operand()
				if not self._kw("and"):
					unsupported("BETWEEN without AND in the SQL fragment", P1_6D)
				return BetweenCriterion(left, lo, self._operand())
			if up == "NOT":
				self.i += 1
				_, token2 = self._peek()
				up2 = (token2 or "").upper()
				if up2 == "LIKE":
					self.i += 1
					return BasicCriterion(Matching.not_like, left, self._operand())
				if up2 == "IN":
					self.i += 1
					return Not(self._in(left))
				if up2 == "BETWEEN":
					self.i += 1
					lo = self._operand()
					if not self._kw("and"):
						unsupported("BETWEEN without AND in the SQL fragment", P1_6D)
					return Not(BetweenCriterion(left, lo, self._operand()))
				unsupported(f"NOT {token2!r} in the SQL fragment (only NOT LIKE / NOT IN / NOT BETWEEN)", P1_6D)
		unsupported(f"a comparison with {token!r} in the SQL fragment", P1_6D)

	def _in(self, left):
		if not self._punct("("):
			unsupported("IN without a parenthesised list in the SQL fragment", P1_6D)
		items = []
		while True:
			items.append(self._operand())
			if self._punct(","):
				continue
			if not self._punct(")"):
				unsupported("a missing ')' of the IN list in the SQL fragment", P1_6D)
			break
		return ContainsCriterion(left, Tuple(*items))

	def _operand(self):
		kind, token = self._peek()
		if kind in ("sq", "dquote"):
			self.i += 1
			return ValueWrapper(_sql_string(token, single=kind == "sq"))
		if kind == "number":
			self.i += 1
			return ValueWrapper(int(token) if re.fullmatch(r"-?\d+", token) else Decimal(token))
		if kind in ("backtick", "word"):
			self.i += 1
			first = token
			_, token2 = self._peek()
			if token2 == ".":
				self.i += 1
				second = self._name()
				return self._fragment_field(first, second)
			if kind == "word":
				up = first.upper()
				_, nt = self._peek()
				if nt == "(" and up in _FRAG_FUNCTIONS:
					return self._fragment_function(up)
				if up == "NULL":
					return NullValue()
				if up in ("TRUE", "FALSE"):
					return ValueWrapper(up == "TRUE")
			return self._fragment_field(None, first)
		unsupported(f"an operand {token!r} in the SQL fragment", P1_6D)

	def _fragment_function(self, name):
		"""A function call in a SQL fragment (chunk P1.6d.2): only functions the ordinary expression
		machinery maps (`IFNULL`, `COALESCE`, `CONCAT`, ...) are accepted - they build PyPika Function
		terms and render through `_function`, exactly like a typed query. Every other function word
		stays refused (it falls through to the unknown-column 1054; a fragment is never approximated)."""
		self.i += 1  # the '('
		args = []
		if not self._punct(")"):
			while True:
				args.append(self._operand())
				if self._punct(","):
					continue
				if not self._punct(")"):
					unsupported(f"a missing ')' of the {name}(...) arguments in the SQL fragment", P1_6D)
				break
		return Function(name, *args)

	def _fragment_field(self, table, name):
		"""Resolve `table`.`name` (or a bare column) against the query's tables, mirroring `_owner`."""
		if table is None:
			matches = [c for c in self.r.ctxs if c.schema.column(name) is not None]
			if not matches:
				raise SurrealDBProgrammingError(1054, f"Unknown column '{name}' in 'field list'")
			if len(matches) > 1:
				raise SurrealDBProgrammingError(1052, f"Column '{name}' in field list is ambiguous")
			return Field(name, table=matches[0].table)
		matches = [c for c in self.r.ctxs if getattr(c.table, "_table_name", None) == table]
		if not matches:
			unsupported(f"a column of a table that is not in the FROM/JOIN list ('{table}')", P1_6D)
		if len(matches) > 1:
			unsupported(f"a column of the table '{table}' listed more than once (self-join)", P1_6D)
		return Field(name, table=matches[0].table)


def _sql_string(raw: str, single: bool) -> str:
	"""The body of a quoted SQL string -> its value: `''` doubling plus backslash escapes."""
	out = []
	i = 0
	while i < len(raw):
		c = raw[i]
		if c == "\\" and i + 1 < len(raw):
			next_ = raw[i + 1]
			out.append({"n": "\n", "t": "\t", "r": "\r", "0": "\0", "b": "\b", "Z": "\x1a"}.get(next_, next_))
			i += 2
			continue
		if single and c == "'" and i + 1 < len(raw) and raw[i + 1] == "'":
			out.append("'")
			i += 2
			continue
		out.append(c)
		i += 1
	return "".join(out)


class Renderer:
	def __init__(self, params: Params | None = None, schema_loader=None, parent: "Renderer | None" = None):
		self.params = params or Params()
		self.schema_loader = schema_loader or table_schema
		self.parent = parent
		self.shared = parent.shared if parent else _Shared()
		self.ctxs: list[TableCtx] = []
		self.outer: Grouping | None = None  # set while compiling the outer level of an aggregate query
		self.correlated: "OuterScope | None" = None  # set while compiling a correlated sub-query (P1.6c)
		self._key_projection = False
		self._aliases: dict = {}  # the projection's display names -> their terms, for ORDER BY <alias>

	# --- entry -----------------------------------------------------------------------------------------------------
	def render(self, q) -> str:
		if _attr(q, "_insert_table") is not None:
			text = self.insert(q)
		elif _attr(q, "_update_table") is not None:
			text = self.update(q)
		elif _attr(q, "_delete_from", False):
			text = self.delete(q)
		else:
			text = self.select(q)
		return "; ".join([*self.shared.prelude, text]) if self.shared.prelude else text

	def _reject(self, q, *names):
		for name in names:
			value = _attr(q, name)
			if value:
				unsupported(f"query feature {name.lstrip('_')}", "P1.6")

	def _child(self) -> "Renderer":
		return Renderer(self.params, self.schema_loader, parent=self)

	# --- tables and columns ------------------------------------------------------------------------------------------
	def _make_ctx(self, table, alias: str, prefix: str = "") -> TableCtx:
		if _kind(table) != "Table":
			unsupported(f"a {_kind(table)} in FROM/JOIN (derived tables)", P1_6C)
		if getattr(table, "_schema", None) is not None:
			unsupported("a schema-qualified table", "P1.6")
		return TableCtx(alias, self.schema_loader(table._table_name), table, prefix)

	def _bind_table(self, table) -> TableSchema:
		ctx = self._make_ctx(table, "t0")
		self.ctxs = [ctx]
		return ctx.schema

	@staticmethod
	def _same_table(a, b) -> bool:
		return a is b or (
			getattr(a, "_table_name", 1) == getattr(b, "_table_name", 2)
			and getattr(a, "alias", None) == getattr(b, "alias", None)
		)

	def _owner(self, field) -> TableCtx:
		owner = getattr(field, "table", None)
		if owner is None:
			matches = [c for c in self.ctxs if c.schema.column(field.name) is not None]
			if len(matches) == 1:
				return matches[0]
			if not matches:
				raise SurrealDBProgrammingError(1054, f"Unknown column '{field.name}' in 'field list'")
			raise SurrealDBProgrammingError(1052, f"Column '{field.name}' in field list is ambiguous")
		for ctx in self.ctxs:
			if self._same_table(ctx.table, owner):
				return ctx
		parent = self.parent
		while parent is not None:
			if any(self._same_table(c.table, owner) for c in parent.ctxs):
				unsupported("a correlated sub-query (it refers to a column of the outer query)", P1_6C)
			parent = parent.parent
		unsupported("a column of a table that is not in the FROM/JOIN list", "P1.6")

	def _column(self, field) -> tuple[str, ColumnSpec]:
		"""(name, spec) of a plain column (write paths)."""
		if _kind(field) != "Field":
			unsupported(f"a {_kind(field)} where a column is required", "P1.6")
		ctx = self._owner(field)
		spec = ctx.schema.column(field.name)
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

	def _col(self, ctx: TableCtx, spec: ColumnSpec) -> Expr:
		stored = physical(spec.name)
		if spec.text_collation_shadow and not spec.shadow_ready:
			# P1.15 §4.4: integrity not established for this column yet - every string-collation
			# operation raises; there is no fallback to the inline lowercase path (invariant §2).
			unsupported(
				f"{spec.name} collation shadows not ready (run bench migrate)", "P1.15",
			)
		expr = Expr(ctx.prefix + quote(stored), spec)
		if spec.has_collation_shadow:
			expr.ci = ctx.prefix + quote(stored + SHADOW_CI)
			expr.like = ctx.prefix + quote(stored + SHADOW_LIKE)
			if spec.has_integrity_hash:
				expr.hash = ctx.prefix + quote(stored + SHADOW_HASH)
		return expr

	def _tkey(self, term):
		"""Identity of a term, to match SELECT/ORDER BY terms with GROUP BY terms."""
		kind = _kind(term)
		if kind == "Field":
			return ("col", self._owner(term).alias, term.name)
		try:
			return ("sql", kind, term.get_sql(quote_char='"', with_namespace=True))
		except Exception:
			return ("id", id(term))

	def _field_expr(self, field) -> Expr:
		if self.correlated is not None:
			bound = self.correlated.try_resolve(field)
			if bound is not None:
				return bound
		ctx = self._owner(field)
		spec = ctx.schema.column(field.name)
		if spec is None:
			raise SurrealDBProgrammingError(1054, f"Unknown column '{field.name}' in 'field list'")
		if self.outer is not None:
			return self.outer.column(ctx, spec)
		return self._col(ctx, spec)

	def _row_mode(self):
		return _RowMode(self)

	# --- constants ---------------------------------------------------------------------------------------------------
	def _bind_const(self, value, family: str, spec: ColumnSpec | None = None):
		"""(param placeholder, ci placeholder | None, like placeholder | None) for constant `value` in a type family."""
		if value is None:
			return "NULL", "NULL", "NULL"
		if family == "str":
			text = values.to_str(value)
			return (
				self.params.add(text),
				self.params.add(collation.ci_key(text)),
				self.params.add(collation.like_shadow(text)),
			)
		if family == "num":
			if isinstance(value, bool):
				value = int(value)
			elif isinstance(value, float | Decimal):
				value = Decimal(str(value))
			elif isinstance(value, str):
				try:
					value = Decimal(value.strip())
				except InvalidOperation:
					unsupported("a non-numeric string where a number is required", P1_6C)
			if isinstance(value, Decimal) and not value.is_finite():
				unsupported("a non-finite number", P1_6C)
			return self.params.add(value), None, None
		try:
			return self.params.add(encode_for_kind(_synthetic(family), value)), None, None
		except values.ValueError_ as e:
			unsupported(
				f"a constant that is not a valid {family} ({e}); MariaDB would compare it as a string", P1_6C
			)

	# --- expressions -------------------------------------------------------------------------------------------------
	def expr(self, term) -> Expr:
		if self.outer is not None:
			key = self._tkey(term)
			if key in self.outer.keys:
				return self.outer.keys[key]
		kind = _kind(term)
		if kind == "Field":
			return self._field_expr(term)
		is_literal, value = self._literal(term)
		if is_literal:
			return Expr(None, None, const=value)
		if _is_wrapper_of_term(term):
			return self.expr(value)
		term = value
		kind = _kind(term)
		if _is_aggregate(term):
			if self.outer is None:
				unsupported("an aggregate outside an aggregate query", P1_6C)
			return self.outer.aggregate(term)
		if kind == "ArithmeticExpression":
			return self._arith(term)
		if kind == "Case":
			return self._case(term)
		if isinstance(term, QueryBuilder):
			return self._scalar_subquery(term)
		if hasattr(term, "name") and hasattr(term, "args"):
			return self._function(term)
		unsupported(f"the expression {kind}", P1_6C)

	# numbers -------------------------------------------------------------------------------------------------------
	def _is_number(self, e: Expr) -> bool:
		if e.is_const:
			return isinstance(e.const, bool | int | float | Decimal)
		return e.kind in NUMERIC_KINDS

	def _num(self, e: Expr, what: str) -> str:
		if not self._is_number(e):
			unsupported(f"{what} on a non-numeric operand", P1_6C)
		return self._bind_const(e.const, "num")[0] if e.is_const else e.sql

	def _null_safe(self, args: list[Expr], body: str) -> str:
		if any(a.is_const and a.const is None for a in args):
			return "NULL"
		checks = [f"{a.sql} = NULL OR {a.sql} = NONE" for a in args if not a.is_const]
		return f"IF {' OR '.join(checks)} THEN NULL ELSE ({body}) END" if checks else f"({body})"

	@staticmethod
	def _round_sql(sql: str, scale: int, truncate: bool = False) -> str:
		"""Round half away from zero (MariaDB) - `math::round`/`math::fixed` round half to even. `truncate` cuts toward zero."""
		x = f"<decimal>({sql})"
		factor = f"{10**scale}dec"
		if truncate:
			return f"(IF {x} < 0 THEN -math::floor(-{x} * {factor}) / {factor} ELSE math::floor({x} * {factor}) / {factor} END)"
		return (
			f"(IF {x} < 0 THEN -(math::floor(-{x} * {factor} + 0.5dec) / {factor}) "
			f"ELSE math::floor({x} * {factor} + 0.5dec) / {factor} END)"
		)

	def _arith(self, term) -> Expr:
		op = _attr_name(term.operator)
		if op not in ("+", "-", "*", "/"):
			unsupported(f"the arithmetic operator {op!r}", P1_6C)
		left, right = self.expr(term.left), self.expr(term.right)
		a, b = self._num(left, f"'{op}'"), self._num(right, f"'{op}'")
		sa = _scale_of(left.spec, left.const if left.is_const else _NOTHING)
		sb = _scale_of(right.spec, right.const if right.is_const else _NOTHING)
		both_int = all(
			(isinstance(e.const, bool | int) if e.is_const else e.kind in INT_KINDS) for e in (left, right)
		)
		if op == "/":
			scale = sa + 4  # div_precision_increment
			body = (
				f"IF {b} = 0 THEN NULL ELSE {self._round_sql(f'<decimal>({a}) / <decimal>({b})', scale)} END"
			)
			spec = _synthetic("decimal", scale)
		else:
			body = f"{a} {op} {b}"
			spec = (
				_synthetic("bigint")
				if both_int
				else _synthetic("decimal", sa + sb if op == "*" else max(sa, sb))
			)
		return Expr(self._null_safe([left, right], body), spec)

	# families ------------------------------------------------------------------------------------------------------
	@staticmethod
	def _family(e: Expr) -> str | None:
		if e.is_const:
			if e.const is None:
				return None
			if isinstance(e.const, bool | int | float | Decimal):
				return "num"
			if isinstance(e.const, dt.datetime):
				return "datetime"
			if isinstance(e.const, dt.date):
				return "date"
			if isinstance(e.const, dt.timedelta):
				return "time"
			return "str"
		if e.kind in NUMERIC_KINDS:
			return "num"
		if e.kind == "varchar":
			return "str"
		if e.kind in ("text", "json"):
			# P1.15: an allow-listed (ready) text column is a first-class string; unshadowed text/json
			# stays "long" (no collation shadow - comparisons refuse)
			if e.spec is not None and e.spec.has_collation_shadow:
				return "str"
			return "long"
		if e.kind in TEMPORAL_KINDS:
			return e.kind
		if e.kind == "uuid":
			return "str"
		return "long"  # long text / json: no collation shadow

	def _unify(self, exprs: list[Expr], what: str):
		"""Bring the operands of IFNULL/COALESCE/CASE to one type. Returns (spec, [(sql, ci, like)])."""
		fams = {f for e in exprs if (f := self._family(e)) is not None}
		non_const = [e for e in exprs if not e.is_const]
		if non_const:
			fams_nc = {self._family(e) for e in non_const}
			if len(fams_nc) != 1:
				unsupported(f"{what} over operands of different types", P1_6C)
			fam = fams_nc.pop()
			if fam in ("date", "datetime") and fams - {fam} == {"str"}:
				# MariaDB types `IFNULL(date_col, '0001-01-01')` as a string; the canonical text is what it prints
				parts = [
					(self.params.add(values.to_str(e.const)) if e.const is not None else "NULL")
					if e.is_const
					else e.sql
					for e in exprs
				]
				return _synthetic("varchar"), [(p, None, None) for p in parts]
			# a constant of another family is only safe when it converts exactly (e.g. 0 with a decimal column)
			if fams - {fam} and not (fam == "num" and fams <= {"num"}):
				if not (fam in TEMPORAL_KINDS and fams <= {fam, "str"}):
					unsupported(f"{what} mixing a {fam} operand with a constant of another type", P1_6C)
		elif len(fams) <= 1:
			fam = next(iter(fams), "str")
		else:
			unsupported(f"{what} over constants of different types", P1_6C)
		if fam == "long":
			unsupported(f"{what} over a long-text column (no collation shadow)", P1_6C)
		out, scale, all_int = [], 0, True
		for e in exprs:
			if e.is_const:
				out.append(self._bind_const(e.const, fam))
				if fam == "num" and e.const is not None:
					scale = max(scale, _scale_of(None, e.const))
					all_int &= isinstance(e.const, bool | int)
			else:
				out.append((e.sql, e.ci, e.like))
				if fam == "num":
					scale = max(scale, _scale_of(e.spec))
					all_int &= e.kind in INT_KINDS
		if fam == "num":
			spec = _synthetic("bigint") if all_int else _synthetic("decimal", scale)
		elif fam == "str":
			spec = _synthetic("varchar")
		else:
			spec = _synthetic(fam)
		return spec, out

	def _chain(self, parts: list) -> str | None:
		return None if any(p is None for p in parts) else "(" + " ?? ".join(parts) + ")"

	def _case(self, term) -> Expr:
		cases = getattr(term, "_cases", [])
		if not cases:
			unsupported("CASE without WHEN", P1_6C)
		conditions = [self.predicate(criterion) for criterion, _ in cases]
		results = [self.expr(result) for _, result in cases]
		other = getattr(term, "_else", None)
		results.append(self.expr(other) if other is not None else Expr(None, None, const=None))
		spec, out = self._unify(results, "CASE")

		def chain(pick) -> str:
			# one END for the whole `IF .. ELSE IF .. ELSE ..` chain: a nested IF must be parenthesised
			branches = " ELSE ".join(f"IF {c} THEN {pick(out[i])}" for i, c in enumerate(conditions))
			return f"{branches} ELSE {pick(out[-1])} END"

		expr = Expr(chain(lambda t: t[0]), spec)
		if spec.is_varchar and all(t[1] is not None for t in out):
			expr.ci, expr.like = chain(lambda t: t[1]), chain(lambda t: t[2])
		return expr

	def _fn_field(self, args: list[Expr], term) -> Expr:
		"""FIELD(needle, v1 .. vn): MariaDB's 1-based position of the first value equal to the needle, else 0.
		The needle meets every value in its own comparison (a varchar needle through the collation shadows), and
		NULL never matches - `FIELD(NULL, ..)` and a NULL entry both simply rank 0."""
		if len(args) < 2:
			unsupported("FIELD with fewer than two operands", P1_6C)
		needle, values = args[0], args[1:]
		if needle.is_const and all(v.is_const for v in values):
			# constant-folded, like MariaDB (numbers coerce strings; strings meet under the *_ci collation)
			index = next((i for i, v in enumerate(values, 1) if self._const_eq(needle.const, v.const)), 0)
			return Expr(str(index), _synthetic("bigint"), const=index)
		chain = " ELSE ".join(f"IF {self._compare('=', needle, v)} THEN {i}" for i, v in enumerate(values, 1))
		return Expr(f"{chain} ELSE 0 END", _synthetic("bigint"))

	@staticmethod
	def _const_eq(a, b) -> bool:
		"""MariaDB's two-literal `=`: numbers coerce (a string becomes its number), strings meet under the
		connection's *_ci collation, anything else must be exactly equal."""
		if a is None or b is None:
			return False
		if isinstance(a, bool):
			a = int(a)
		if isinstance(b, bool):
			b = int(b)
		if isinstance(a, int | float | Decimal) and isinstance(b, int | float | Decimal):
			return Decimal(str(a)) == Decimal(str(b))
		if isinstance(a, str) and isinstance(b, str):
			return collation.ci_key(a) == collation.ci_key(b)
		if isinstance(a, str) and isinstance(b, int | float | Decimal):
			try:
				return Decimal(a.strip()) == Decimal(str(b))
			except InvalidOperation:
				return False
		if isinstance(b, str) and isinstance(a, int | float | Decimal):
			try:
				return Decimal(b.strip()) == Decimal(str(a))
			except InvalidOperation:
				return False
		return a == b

	# functions -----------------------------------------------------------------------------------------------------
	def _function(self, term) -> Expr:
		name = str(term.name).strip().upper()
		handler = getattr(self, "_fn_" + re.sub(r"\W", "", name).lower(), None)
		if handler is None:
			unsupported(f"the SQL function {name}", P1_6C)
		if name == "EXTRACT":  # the unit is a keyword, not a value
			unit = term.args[0]
			unit = getattr(unit, "_value", None) or getattr(unit, "value", None)
			return handler([Expr(None, None, const=unit)], term)
		return handler([self.expr(a) for a in term.args], term)

	def _ifnull_like(self, args: list[Expr], name: str) -> Expr:
		if len(args) < 2:
			unsupported(f"{name} with fewer than two operands", P1_6C)
		first = args[0]
		if (
			len(args) == 2
			and args[1].is_const
			and args[1].const == ""
			and not first.is_const
			and first.kind in ("text", "json")
			and not (first.spec is not None and first.spec.has_collation_shadow)
		):
			# stays a long-text value; only its emptiness can be tested (`_compare`) - a SHADOWED text
			# column goes through _unify instead, where IFNULL(col, '') gets full collation semantics
			return Expr(f"({first.sql} ?? {self.params.add('')})", first.spec)
		if (
			len(args) == 2
			and args[1].is_const
			and args[1].const == ""
			and not first.is_const
			and self._family(first) in ("num", *TEMPORAL_KINDS)
		):
			# `IFNULL(number_or_date, '')`: MariaDB makes it a string; the only thing Frappe does with it is `= ''` ("is not set"),
			# which is exactly `IS NULL`. Anything else would need MariaDB's number->string formatting: refused in `_operand`.
			empty = self.params.add(collation.ci_key(""))
			return Expr(
				f"IF {_present(first.sql)} THEN 'x' ELSE '' END",
				_synthetic("varchar"),
				ci=f"IF {_present(first.sql)} THEN 'x' ELSE {empty} END",
				opaque_string=True,
			)
		spec, out = self._unify(args, name)
		expr = Expr(self._chain([t[0] for t in out]), spec)
		if spec.is_varchar:
			expr.ci = self._chain([t[1] for t in out])
			expr.like = self._chain([t[2] for t in out])
		return expr

	def _fn_ifnull(self, args, term):
		return self._ifnull_like(args, "IFNULL")

	def _fn_coalesce(self, args, term):
		return self._ifnull_like(args, "COALESCE")

	def _fn_nullif(self, args, term):
		if len(args) != 2 or args[0].is_const:
			unsupported("NULLIF with a constant first operand", P1_6C)
		a = args[0]
		condition = self._compare("=", a, args[1])
		expr = Expr(f"IF {condition} THEN NULL ELSE {a.sql} END", a.spec)
		if a.ci is not None:
			expr.ci = f"IF {condition} THEN NULL ELSE {a.ci} END"
			expr.like = f"IF {condition} THEN NULL ELSE {a.like} END"
		return expr

	def _fn_concat(self, args, term):
		parts = []
		for a in args:
			fam = self._family(a)
			if a.is_const:
				if a.const is None:
					return Expr("NULL", _synthetic("varchar"))
				if fam not in ("str", "num") or isinstance(a.const, float | Decimal):
					unsupported("CONCAT of a constant that MariaDB would format", P1_6C)
				parts.append(self.params.add(str(a.const) if fam == "num" else values.to_str(a.const)))
			elif a.kind in ("varchar", "text", "date", "datetime", "uuid"):
				parts.append(a.sql)  # stored as MariaDB prints them
			elif a.kind in INT_KINDS:
				parts.append(f"<string>{a.sql}")
			else:
				unsupported(f"CONCAT of a {a.kind} value (MariaDB's formatting is not reproduced)", P1_6C)
		return Expr(self._null_safe(args, f"string::concat({', '.join(parts)})"), _synthetic("varchar"))

	def _fn_concat_ws(self, args, term):
		"""`CONCAT_WS(sep, v1, v2, ..)`. MariaDB returns NULL for a NULL separator but *skips* NULL values, while SurrealDB's
		`string::concat` would print them as 'NULL'; so every value part carries a NULL/NONE guard (measured, P1.6c)."""
		if not args:
			unsupported("CONCAT_WS without a separator", P1_6C)
		sep = args[0]
		if not sep.is_const:
			unsupported("CONCAT_WS with a non-constant separator", P1_6C)
		if sep.const is None:
			return Expr("NULL", _synthetic("varchar"))
		if len(args) == 1:  # no values: MariaDB returns ''
			return Expr(f"string::concat({self.params.add('')})", _synthetic("varchar"))
		fam = self._family(sep)
		if fam not in ("str", "num") or isinstance(sep.const, float | Decimal):
			unsupported("CONCAT_WS with a constant separator that MariaDB would format", P1_6C)
		parts = [self.params.add(str(sep.const) if fam == "num" else values.to_str(sep.const))]
		for a in args[1:]:
			if a.is_const:
				if a.const is None:
					continue  # MariaDB skips NULL values
				fam = self._family(a)
				if fam not in ("str", "num") or isinstance(a.const, float | Decimal):
					unsupported("CONCAT_WS of a constant that MariaDB would format", P1_6C)
				parts.append(self.params.add(str(a.const) if fam == "num" else values.to_str(a.const)))
			elif a.kind in ("varchar", "text", "date", "datetime", "uuid"):
				parts.append(f"IF {a.sql} = NULL OR {a.sql} = NONE THEN '' ELSE {a.sql} END")
			elif a.kind in INT_KINDS:
				parts.append(f"IF {a.sql} = NULL OR {a.sql} = NONE THEN '' ELSE <string>{a.sql} END")
			else:
				unsupported(f"CONCAT_WS of a {a.kind} value (MariaDB's formatting is not reproduced)", P1_6C)
		return Expr(f"string::concat({', '.join(parts)})", _synthetic("varchar"))

	def _rounding(self, args, truncate: bool) -> Expr:
		x = args[0]
		digits = 0
		if len(args) > 1:
			if not (args[1].is_const and isinstance(args[1].const, int)) or args[1].const < 0:
				unsupported("ROUND/TRUNCATE with a non-constant or negative digit count", P1_6C)
			digits = args[1].const
		if not self._is_number(x):
			unsupported("ROUND/TRUNCATE of a non-numeric value", P1_6C)
		if not x.is_const and x.kind in INT_KINDS:
			return x
		sql = self._round_sql(self._num(x, "ROUND"), digits, truncate)
		return Expr(self._null_safe([x], sql), _synthetic("decimal", digits))

	def _fn_round(self, args, term):
		return self._rounding(args, False)

	def _fn_truncate(self, args, term):
		if len(args) != 2:
			unsupported("TRUNCATE without a digit count", P1_6C)
		return self._rounding(args, True)

	def _fn_abs(self, args, term):
		(x,) = args
		spec = x.spec if not x.is_const else _synthetic("decimal", _scale_of(None, x.const))
		return Expr(self._null_safe([x], f"math::abs({self._num(x, 'ABS')})"), spec)

	def _fn_ceil(self, args, term):
		return self._floor_ceil(args, "ceil")

	_fn_ceiling = _fn_ceil

	def _fn_floor(self, args, term):
		return self._floor_ceil(args, "floor")

	def _floor_ceil(self, args, function: str) -> Expr:
		(x,) = args
		if not x.is_const and x.kind in INT_KINDS:
			return x
		return Expr(
			self._null_safe([x], f"math::{function}({self._num(x, function.upper())})"),
			_synthetic("decimal", 0),
		)

	def _string_arg(self, e: Expr, what: str) -> str:
		if e.is_const:
			return self.params.add(values.to_str(e.const))
		if e.kind not in ("varchar", "text"):
			unsupported(f"{what} of a {e.kind} value", P1_6C)
		return e.sql

	def _fn_char_length(self, args, term):
		(s,) = args
		return Expr(
			self._null_safe([s], f"string::len({self._string_arg(s, 'CHAR_LENGTH')})"), _synthetic("bigint")
		)

	_fn_character_length = _fn_char_length

	def _fn_substring(self, args, term):
		if len(args) not in (2, 3) or not all(a.is_const and isinstance(a.const, int) for a in args[1:]):
			unsupported("SUBSTRING with non-constant positions", P1_6C)
		position = args[1].const
		if position < 1 or (len(args) == 3 and args[2].const < 0):
			unsupported("SUBSTRING with a zero, negative or reversed position", P1_6C)
		s = self._string_arg(args[0], "SUBSTRING")
		length = f", {position - 1 + args[2].const}" if len(args) == 3 else ""  # string::slice(s, from, to)
		return Expr(
			self._null_safe([args[0]], f"string::slice({s}, {position - 1}{length})"), _synthetic("varchar")
		)

	_fn_substr = _fn_substring

	# dates and times (the stored forms are canonical text, see values.py) ---------------------------------------------
	def _now_param(self) -> str:
		if self.shared.now is None:
			# MariaDB's NOW() is constant within a statement; the DB session zone is UTC on both engines
			now = dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)
			self.shared.now = self.params.add(now.strftime("%Y-%m-%d %H:%M:%S") + ".000000")
		return self.shared.now

	def _fn_now(self, args, term):
		return Expr(self._now_param(), _synthetic("datetime"))

	_fn_current_timestamp = _fn_now
	_fn_sysdate = _fn_now

	def _temporal(self, e: Expr, what: str, kinds=("date", "datetime")) -> Expr:
		if e.is_const:
			# a constant date/datetime literal takes its type from its shape
			text = values.to_str(e.const) if not isinstance(e.const, dt.date) else None
			fam = self._family(e)
			if fam == "str" and text and re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text.strip()):
				fam = "date"
			elif fam == "str" and text and re.match(r"\d{4}-\d{1,2}-\d{1,2}[ T]\d", text.strip()):
				fam = "datetime"
			if fam not in kinds:
				unsupported(f"{what} of a constant that is not a {'/'.join(kinds)}", P1_6C)
			sql, _, _ = self._bind_const(e.const, fam)
			return Expr(sql, _synthetic(fam))
		if e.kind not in kinds:
			unsupported(f"{what} of a {e.kind} value", P1_6C)
		return e

	@staticmethod
	def _as_datetime(e: Expr) -> str:
		"""SurrealQL datetime from a stored date/datetime text (UTC, the session zone of both engines)."""
		if e.kind == "date":
			return f"<datetime>({e.sql} + 'T00:00:00Z')"
		return f"<datetime>(string::replace(string::slice({e.sql}, 0, 19), ' ', 'T') + 'Z')"

	def _fn_date(self, args, term):
		(x,) = args
		x = self._temporal(x, "DATE()")
		if x.kind == "date":
			return x
		return Expr(self._null_safe([x], f"string::slice({x.sql}, 0, 10)"), _synthetic("date"))

	def _fn_timestamp(self, args, term):
		first = self._temporal(args[0], "TIMESTAMP()")
		if len(args) == 1:
			if first.kind == "datetime":
				return first
			return Expr(
				self._null_safe([first], f"({first.sql} + ' 00:00:00.000000')"), _synthetic("datetime")
			)
		if len(args) != 2 or first.kind != "date":
			unsupported("TIMESTAMP(x, y) other than (date, time)", P1_6C)
		t = args[1]
		if t.is_const:
			unsupported("TIMESTAMP(date, constant time)", P1_6C)
		if t.kind != "time":
			unsupported(f"TIMESTAMP(date, {t.kind})", P1_6C)
		body = f"time::format({self._as_datetime(first)} + duration::from_micros({t.sql}), '%Y-%m-%d %H:%M:%S%.6f')"
		return Expr(self._null_safe([first, t], body), _synthetic("datetime"))

	def _fn_unix_timestamp(self, args, term):
		(x,) = args
		x = self._temporal(x, "UNIX_TIMESTAMP()")
		if x.kind == "date":
			return Expr(self._null_safe([x], f"time::unix({self._as_datetime(x)})"), _synthetic("bigint"))
		micros = f"<decimal>(<int>string::slice({x.sql}, 20, 26)) / 1000000dec"
		body = f"<decimal>time::unix({self._as_datetime(x)}) + {micros}"
		return Expr(self._null_safe([x], body), _synthetic("decimal", 6))

	def _part(self, args, start: int, end: int, what: str) -> Expr:
		(x,) = args
		x = self._temporal(x, what)
		return Expr(
			self._null_safe([x], f"<int>string::slice({x.sql}, {start}, {end})"), _synthetic("bigint")
		)

	def _fn_year(self, args, term):
		return self._part(args, 0, 4, "YEAR()")

	def _fn_month(self, args, term):
		return self._part(args, 5, 7, "MONTH()")

	def _fn_day(self, args, term):
		return self._part(args, 8, 10, "DAY()")

	_fn_dayofmonth = _fn_day

	def _fn_quarter(self, args, term):
		month = self._part(args, 5, 7, "QUARTER()")
		return Expr(
			self._null_safe([args[0]], f"math::floor((<decimal>({month.sql}) + 2dec) / 3dec)"),
			_synthetic("bigint"),
		)

	def _fn_extract(self, args, term):
		unit = _attr_name(args[0].const).lower() if args and args[0].is_const else None
		field = getattr(term, "field", None)
		if field is None or unit not in ("year", "month", "day", "quarter"):
			unsupported(f"EXTRACT({unit} ...)", P1_6C)
		x = self.expr(field)
		if unit == "quarter":
			return self._fn_quarter([x], term)
		start, end = {"year": (0, 4), "month": (5, 7), "day": (8, 10)}[unit]
		return self._part([x], start, end, f"EXTRACT({unit})")

	def _fn_monthname(self, args, term):
		(x,) = args
		x = self._temporal(x, "MONTHNAME()")
		return Expr(
			self._null_safe([x], f"time::format({self._as_datetime(x)}, '%B')"), _synthetic("varchar")
		)

	def _fn_dayname(self, args, term):
		(x,) = args
		x = self._temporal(x, "DAYNAME()")
		return Expr(
			self._null_safe([x], f"time::format({self._as_datetime(x)}, '%A')"), _synthetic("varchar")
		)

	_DATE_FORMATS: ClassVar[dict[str, str]] = {
		"Y": "%Y", "y": "%y", "m": "%m", "c": "%-m", "d": "%d", "e": "%-d", "H": "%H", "k": "%-H", "h": "%I", "I": "%I",
		"l": "%-I", "i": "%M", "s": "%S", "S": "%S", "f": "%6f", "T": "%H:%M:%S", "M": "%B", "b": "%b", "W": "%A",
		"a": "%a", "j": "%j", "p": "%p", "%": "%%",
	}  # fmt: skip

	def _fn_date_format(self, args, term):
		if len(args) != 2 or not (args[1].is_const and isinstance(args[1].const, str)):
			unsupported("DATE_FORMAT with a non-constant format", P1_6C)
		x = self._temporal(args[0], "DATE_FORMAT()")
		out, spec = [], iter(args[1].const)
		for ch in spec:
			if ch != "%":
				out.append("%%" if ch == "%" else ch)
				continue
			code = next(spec, "")
			if code not in self._DATE_FORMATS:
				unsupported(f"the DATE_FORMAT specifier %{code}", P1_6C)
			out.append(self._DATE_FORMATS[code])
		fmt = self.params.add("".join(out))
		return Expr(
			self._null_safe([x], f"time::format({self._as_datetime(x)}, {fmt})"), _synthetic("varchar")
		)

	# --- predicates (WHERE / HAVING / ON), negation pushed to the leaves ---------------------------------------------------
	def _guard(self, ref: str) -> str:
		return f"{ref} != NULL AND {ref} != NONE"

	def _operand(self, e: Expr, value):
		"""(stored side, bound placeholder) to compare expression `e` with the Python `value`."""
		spec = e.spec
		if spec is None:
			unsupported("comparing constants", "P1.6")
		if spec.has_collation_shadow:
			# patch_text_int_cmp (official-run blocker #3) (varchar or allow-listed text: the constant
			# binds through its collation key)
			if isinstance(value, float | Decimal):
				unsupported(
					"comparing a string column with a non-integer number (MariaDB converts the column to a number)",
					"P1.6",
				)
			if isinstance(value, bool | int):
				# MariaDB casts the string column to a number; see the text branch note.
				value = "1" if (isinstance(value, bool) and value) else str(int(value))
			if e.ci is None:
				unsupported(
					"comparing a computed string (its collation key cannot be computed in SurrealQL)", P1_6C
				)
			if e.opaque_string and value != "":
				unsupported("comparing IFNULL(non-string, '') with anything but ''", P1_6C)
			return e.ci, self.params.add(collation.ci_key(values.to_str(value)))
		if isinstance(value, str) and not value.strip() and spec.kind in EMPTY_STRING_AS:
			# MariaDB converts '' to the zero of the other operand's type (`date <> ''` is Frappe's "is set" for a Date field)
			return e.sql, self.params.add(EMPTY_STRING_AS[spec.kind])
		if spec.kind in INT_KINDS or spec.kind == "decimal":
			return e.sql, self.params.add(self._number(spec, value))
		if spec.kind in ("date", "datetime", "time", "uuid"):
			if (
				spec.kind == "date"
				and isinstance(value, str | dt.datetime)
				and (isinstance(value, dt.datetime) or re.search(r"[ T]\d{1,2}:\d", value))
			):
				# MariaDB compares a DATE with a datetime literal as a DATETIME: the date at midnight
				try:
					encoded = encode_for_kind(_synthetic("datetime"), value)
				except values.ValueError_ as e_:
					unsupported(f"an invalid datetime literal ({e_})", "P1.6")
				midnight = f"IF {_absent(e.sql)[1:-1]} THEN NULL ELSE ({e.sql} + ' 00:00:00.000000') END"
				return f"({midnight})", self.params.add(encoded)
			try:
				encoded = encode_for_kind(
					spec, value.lower() if spec.kind == "uuid" and isinstance(value, str) else value
				)
			except values.ValueError_ as e_:
				unsupported(f"an invalid {spec.kind} literal ({e_})", "P1.6")
			return e.sql, self.params.add(encoded)
		if spec.kind == "text":
			if spec.has_collation_shadow:  # pragma: no cover - the collation branch above owns these
				unsupported(
					"a shadowed text column reached the inline lowercase path (invariant: one collation representation)",
					"P1.15",
				)
			# Text columns (Small Text/Text/Long/Medium - they share the `text` kind) have no collation
			# shadow - only varchar columns get one - but their comparisons are ASCII in practice.
			# Approximate MariaDB's *_ci collation with an inline case-fold on both sides; NULL never
			# compares equal (MariaDB UNKNOWN - and SurrealDB's string::lowercase(NULL) raises, so the
			# stored side carries the NULL/NONE guard). Upstream test_db compares these columns directly
			# (bulk_insert cleanup, DocField cache flows), so they are supported; LIKE/ORDER BY/GROUP BY
			# on them stay refused (the collation extension, P1.6d).
			if not isinstance(value, str):
				# patch_text_int_cmp (official-run blocker #3)
				if isinstance(value, bool | int):
					# MariaDB casts the string column to a number, so `text_col = 1` matches '1'
					# (Property Setter: `value = 1` on every Email Account validate). Frappe stores
					# flag values canonically as '1'/'0'; approximate the numeric cast with the
					# canonical decimal string ('01'/' 1' variants differ - see OPEN-ITEMS).
					value = "1" if (isinstance(value, bool) and value) else str(int(value))
				else:
					unsupported(f"comparing a text column with a non-string ({type(value).__name__})", "P1.6")
			operand = self.params.add(values.to_str(value))
			return self._null_safe([e], f"string::lowercase({e.sql})"), f"string::lowercase({operand})"
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
		if kind == "ExistsCriterion":
			return self._exists(term, negate)
		if kind in ("NullCriterion", "NotNullCriterion"):
			e = self.expr(term.term)
			if e.is_const:
				unsupported("IS NULL of a constant", "P1.6")
			is_null = (kind == "NullCriterion") != negate
			return _absent(e.sql) if is_null else f"({self._guard(e.sql)})"
		if kind == "BetweenCriterion":
			return self._between(term, negate)
		if kind.endswith("ValueWrapper"):
			truth = bool(term.value)
			return "true" if truth != negate else "false"
		if kind in ("RawCriterion", "CombinedRawCriterion"):
			return self._raw_criterion(term, negate)
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
		return self._compare(op, self.expr(term.left), self.expr(term.right))

	def _compare(self, op: str, left: Expr, right: Expr) -> str:
		if left.is_const and right.is_const:
			unsupported("a comparison of two constants", "P1.6")
		if left.is_const:
			left, right, op = right, left, FLIPPED[op]
		if right.is_const:
			if right.const is None:
				return "false"  # `col = NULL` is UNKNOWN in MariaDB: never true, and neither is its negation
			if (
				left.spec is not None
				and left.spec.kind in ("text", "json")
				and not left.spec.has_collation_shadow
				and isinstance(right.const, str)
				and op in ("=", "!=")
				and collation.equals_empty(right.const)
			):
				# "is set" / "is not set" on UNshadowed long text: exact without a shadow (see
				# collation.empty_pattern) - a shadowed column takes the @ci path below, like varchars
				matches = f"string::matches({left.sql}, {self.params.add(collation.empty_pattern())})"
				present = self._guard(left.sql)
				return f"({present} AND {matches})" if op == "=" else f"({present} AND NOT ({matches}))"
			stored, operand = self._operand(left, right.const)
			core = f"{stored} {op} {operand}"
			if op == "=":
				return f"({core})"
			return f"({self._guard(stored)} AND {core})"
		return self._compare_exprs(op, left, right)

	def _compare_exprs(self, op: str, a: Expr, b: Expr) -> str:
		fa, fb = self._family(a), self._family(b)
		if fa != fb or fa == "long":
			unsupported(f"comparing a {a.kind} value with a {b.kind} value", "P1.6")
		if a.opaque_string or b.opaque_string:
			unsupported("comparing IFNULL(non-string, '') with a column", P1_6C)
		if fa == "str" and a.ci is not None and b.ci is not None:
			ra, rb = a.ci, b.ci
		elif fa == "str" and not (a.kind == b.kind == "uuid"):
			unsupported(f"comparing a {a.kind} value with a {b.kind} value", "P1.6")
		else:
			ra, rb = a.sql, b.sql
		return f"({self._guard(ra)} AND {self._guard(rb)} AND {ra} {op} {rb})"

	def _like(self, term, negate: bool, invert: bool) -> str:
		left = self.expr(term.left)
		is_literal, pattern = self._literal(term.right)
		if left.is_const or not is_literal or not isinstance(pattern, str):
			unsupported("LIKE with a pattern that is not a string constant", "P1.6")
		if not left.spec.has_collation_shadow:
			unsupported(
				f"LIKE on a {left.spec.logical} column (not in the text-collation-shadow allow-list)", "P1.6"
			)
		if left.like is None or left.opaque_string:
			unsupported(
				"LIKE on a computed string (its collation shadow cannot be computed in SurrealQL)", P1_6C
			)
		escape = getattr(term, "escape", None)
		escape_char = "\\" if escape is None else self._literal(escape)[1]
		if not isinstance(escape_char, str) or len(escape_char) != 1:
			unsupported("LIKE with an ESCAPE that is not a single character", "P1.6")
		shadow = left.like
		match = f"string::matches({shadow}, {self.params.add(collation.like_regex(pattern, escape_char))})"
		positive = invert == negate  # NOT LIKE under NOT is LIKE
		return f"({self._guard(shadow)} AND {match if positive else f'NOT ({match})'})"

	def _contains(self, term, negate: bool) -> str:
		negate = negate != bool(getattr(term, "_is_negated", False))
		left = self.expr(term.term)
		container = _query_of(term.container)
		if isinstance(container, QueryBuilder):
			return self._in_subquery(left, container, negate)
		if _kind(container) != "Tuple":
			unsupported(f"IN over a {_kind(container)}", "P1.6")
		if left.is_const:
			return self._const_in(left, container, negate)
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
			stored, operand = self._operand(left, value)
			operands.append(operand)
		membership = f"{stored} IN [{', '.join(operands)}]"
		return f"({self._guard(stored)} AND NOT ({membership}))" if negate else f"({membership})"

	def _const_in(self, left: Expr, container, negate: bool) -> str:
		"""`'x' IN (..)` with a constant on the left (the note.py login check and friends): MariaDB folds the
		literal elements; an element that is a column is one membership comparison, and a NULL element or needle
		is UNKNOWN - the row never matches (for NOT IN too, unless the list is empty)."""
		needle = left.const
		literal, exprs = [], []
		for item in container.values:
			is_literal, value = self._literal(item)
			(literal if is_literal else exprs).append(value if is_literal else item)
		if needle is None:
			if not literal and not exprs:
				return "true" if negate else "false"  # an empty list: `x NOT IN ()` is vacuously true
			return "false"  # NULL = anything is UNKNOWN, and its negation over a non-empty list as well
		matches = any(value is not None and self._const_eq(needle, value) for value in literal)
		has_null = any(value is None for value in literal)
		if negate:
			if matches or has_null:
				return "false"  # an element equal to the needle is false; a NULL element is UNKNOWN
			compares = [self._compare("!=", self.expr(item), left) for item in exprs]
			return "(" + " AND ".join(compares) + ")" if compares else "true"
		if matches:
			return "true"
		if has_null and not exprs:
			return "false"  # only UNKNOWN elements: no element can make the membership true
		compares = [self._compare("=", self.expr(item), left) for item in exprs]
		return "(" + " OR ".join(compares) + ")" if compares else "false"

	def _between(self, term, negate: bool) -> str:
		left = self.expr(term.term)
		if left.is_const:
			unsupported("BETWEEN without a column", "P1.6")
		lo, hi = self.expr(term.start), self.expr(term.end)
		if not (lo.is_const and hi.is_const):
			if negate:
				return f"({self._compare('<', left, lo)} OR {self._compare('>', left, hi)})"
			return f"({self._compare('>=', left, lo)} AND {self._compare('<=', left, hi)})"
		if lo.const is None or hi.const is None:
			unsupported("BETWEEN with a NULL bound", "P1.6")
		(stored, lo_p), (_, hi_p) = self._operand(left, lo.const), self._operand(left, hi.const)
		if negate:
			return f"({self._guard(stored)} AND ({stored} < {lo_p} OR {stored} > {hi_p}))"
		return f"({self._guard(stored)} AND {stored} >= {lo_p} AND {stored} <= {hi_p})"

	def _raw_criterion(self, term, negate: bool) -> str:
		"""`RawCriterion` / `CombinedRawCriterion` (chunk P1.6d): parse the fragment text into a
		PyPika criterion and render it through the ordinary predicate() machinery."""
		if _kind(term) == "CombinedRawCriterion":
			op = str(term.operator).strip().upper()
			if op not in ("AND", "OR"):
				unsupported(f"the boolean operator {op!r} of a CombinedRawCriterion", P1_6D)
			joiner = ("or" if op == "AND" else "and") if negate else op.lower()
			return (
				f"({self._raw_side(term.left, negate)} {joiner.upper()} {self._raw_side(term.right, negate)})"
			)
		return self.predicate(_FragmentParser(self, term.sql_string).parse(), negate)

	def _raw_side(self, term, negate: bool) -> str:
		kind = _kind(term)
		if kind == "RawCriterion":
			return self.predicate(_FragmentParser(self, term.sql_string).parse(), negate)
		if kind == "CombinedRawCriterion":
			return self._raw_criterion(term, negate)
		return self.predicate(term, negate)

	# sub-queries -----------------------------------------------------------------------------------------------------

	def _hoist(self, rows: str) -> str:
		"""A `LET $sqN` statement that runs the (already rendered) uncorrelated sub-query once, before the statement."""
		self.shared.counter += 1
		name = f"$sq{self.shared.counter}"
		self.shared.prelude.append(f"LET {name} = (SELECT VALUE `__c0` FROM ({rows}))")
		return name

	def _in_subquery(self, left: Expr, q, negate: bool) -> str:
		if len(q._selects) != 1:
			raise SurrealDBProgrammingError(1241, "Operand should contain 1 column(s)")
		child = self._child()
		child._key_projection = True
		child.correlated = OuterScope(self, child)
		rows = child.select(q, hints=False)
		spec = child.result_specs[0]
		if left.is_const and left.const is None:
			# `NULL IN (..)` is UNKNOWN (never true); its negation is true only over an empty list
			if not negate:
				return "false"
			if child.correlated.bound:
				arr = f"(SELECT VALUE `__c0` FROM ({rows}))"
				return f"(array::map([{{{', '.join(child.correlated.bundle)}}}], |$o| array::len({arr}) = 0))[0]"
			return f"array::len({self._hoist(rows)}) = 0"
		fa = self._family(left)
		if fa != self._family(Expr(None, spec)) or fa == "long":
			unsupported("IN (sub-query) over columns of different types", P1_6C)
		varchar_key = fa == "str" and spec.has_collation_shadow
		name = None if child.correlated.bound else self._hoist(rows)
		if not left.is_const:
			if fa == "str" and not (left.ci is not None and spec.has_collation_shadow):
				unsupported("IN (sub-query) over a computed string", P1_6C)
			if left.opaque_string:
				unsupported("IN (sub-query) over IFNULL(non-string, '')", P1_6C)
		if child.correlated.bound:
			return self._correlated_in(child.correlated, left, rows, negate, varchar_key)
		if left.is_const:
			if left.const is None:
				return "false"  # `NULL IN (..)` is UNKNOWN, and its negation over a non-empty list as well
			bound = self._bind_const(left.const, fa, spec)
			ref = bound[1] if varchar_key else bound[0]
			# a bound constant is never NULL/NONE: it needs no guard
			if not negate:
				return f"({ref} IN {name})"
			return (
				f"(array::len({name}) = 0 OR (NOT ({ref} IN {name}) "
				f"AND NOT (NULL IN {name}) AND NOT (NONE IN {name})))"
			)
		ref = left.ci if varchar_key else left.sql
		if not negate:
			return f"({self._guard(ref)} AND {ref} IN {name})"
		# NOT IN: true for every row when the sub-query is empty, never true when it holds a NULL
		return (
			f"(array::len({name}) = 0 OR ({self._guard(ref)} AND NOT ({ref} IN {name}) "
			f"AND NOT (NULL IN {name}) AND NOT (NONE IN {name})))"
		)

	def _correlated_in(self, scope: OuterScope, left: Expr, rows: str, negate: bool, varchar_key: bool) -> str:
		"""`x IN (SELECT y FROM .. WHERE <reads this row>)`: the truth is per outer row. The binding object of the
		scalar sub-query's closure shape (an `array::map` over the referenced outer columns) carries the left operand
		under its own key, and the inner rows for that binding answer the membership (the hoisted shape's guards)."""
		if left.is_const:
			if left.const is None:
				return "false"  # `NULL IN (..)` is UNKNOWN, and its negation over a non-empty list as well
			bound = self._bind_const(left.const, self._family(left))
			ref = bound[1] if varchar_key else bound[0]
		else:
			if varchar_key and left.ci is None:
				unsupported("IN (correlated sub-query) with a computed string on the left", P1_6C)
			carried = scope.carry(left)
			ref = carried.ci if varchar_key else carried.sql
		arr = f"(SELECT VALUE `__c0` FROM ({rows}))"
		guard = "" if left.is_const else f"{self._guard(ref)} AND "
		if negate:
			body = (
				f"(array::len({arr}) = 0 OR ({guard}NOT ({ref} IN {arr}) "
				f"AND NOT (NULL IN {arr}) AND NOT (NONE IN {arr})))"
			)
		else:
			body = f"({guard}{ref} IN {arr})"
		return f"(array::map([{{{', '.join(scope.bundle)}}}], |$o| {body}))[0]"

	def _exists(self, term, negate: bool) -> str:
		negate = negate != bool(getattr(term, "_is_negated", False))
		child = self._child()
		child._key_projection = False
		child.correlated = OuterScope(self, child)
		rows = child.select(_query_of(term.container), hints=False)
		if not child.correlated.bound:
			name = self._hoist(rows)
			return f"array::len({name}) {'=' if negate else '>'} 0"
		# `EXISTS (SELECT .. WHERE <reads this row>)`: one truth per outer row - the scalar sub-query's closure
		# shape, with the number of the inner rows for that binding instead of its first value.
		obj = "{" + ", ".join(child.correlated.bundle) + "}"
		body = f"array::len((SELECT VALUE `__c0` FROM ({rows})))"
		return f"(array::map([{obj}], |$o| {body}))[0] {'=' if negate else '>'} 0"

	def _count_scalar(self, q) -> bool:
		"""`SELECT COUNT(*) FROM t` without GROUP BY: MariaDB answers one row (0 for an empty t), while SurrealDB's
		`GROUP ALL` over no records returns none, so this scalar sub-query needs an empty-set default of 0."""
		if q._groupbys or q._distinct or q._havings or q._limit is not None or q._offset or len(q._selects) != 1:
			return False
		term = q._selects[0]
		return (
			isinstance(term, AggregateFunction)
			and str(term.name).strip().upper() == "COUNT"
			and not _attr(term, "_distinct", False)
		)

	def _scalar_subquery(self, q) -> Expr:
		if len(q._selects) != 1:
			raise SurrealDBProgrammingError(1241, "Operand should contain 1 column(s)")
		child = self._child()
		child._key_projection = False
		child.correlated = OuterScope(self, child)
		rows = child.select(q, hints=False)
		zero = self._count_scalar(q)
		if not child.correlated.bound:
			name = self._hoist(rows)
			ref = f"array::first({name})"
			if zero:
				ref = f"IF array::len({name}) = 0 THEN 0 ELSE {ref} END"
			return Expr(ref, child.result_specs[0])
		# A correlated scalar sub-query is evaluated per outer row: one closure step carries the referenced outer
		# columns as the keys of a one-element object (the binding the join renderer uses, so an index on the inner
		# column is used - SurrealDB plans a TableScan for `$parent.x`, findings/P1.6-builder.md § 9), and the inner
		# query picks the first of its rows for that value (NONE when there is none, like MariaDB's empty result).
		obj = "{" + ", ".join(child.correlated.bundle) + "}"
		if zero:
			# the inner query runs once per row: a `LET` and the brace form of IF, exactly the LEFT JOIN block's shape
			self.shared.counter += 1
			name = f"$sq{self.shared.counter}"
			body = (
				f"{{ LET {name} = (SELECT VALUE `__c0` FROM ({rows})); "
				f"IF array::len({name}) = 0 {{ 0 }} ELSE {{ array::first({name}) }} }}"
			)
		else:
			body = f"(SELECT VALUE `__c0` FROM ({rows}))[0]"
		return Expr(f"(array::map([{obj}], |$o| {body}))[0]", child.result_specs[0])

	# --- SELECT ------------------------------------------------------------------------------------------------------
	def _conjuncts(self, term):
		if _kind(term) == "ComplexCriterion" and _sql_word(term.comparator) == "and":
			yield from self._conjuncts(term.left)
			yield from self._conjuncts(term.right)
		else:
			yield term

	def _only_main(self, term) -> bool:
		main = self.ctxs[0]
		try:
			if any(isinstance(node, QueryBuilder) for node in term.nodes_()):
				return False
			fields = term.fields_()
		except Exception:
			return False
		return bool(fields) and all(
			getattr(f, "table", None) is not None and self._same_table(main.table, f.table) for f in fields
		)

	def _joined_source(self, joins, pushed_sql: str, page=None) -> str:
		"""The rows of `FROM a JOIN b ON ...` as an array of `{t0: <a row>, t1: <b row or NONE>}` objects.

		SurrealDB has no JOIN. Each step maps over the rows of the previous step with a **closure** whose parameter carries the row
		(`array::map(rows, |$r| SELECT ... FROM b WHERE b.x@ci = $r.t0.y@ci)`): SurrealDB 3.2.4 plans an index lookup for a
		comparison against a constant, a bound parameter or a closure parameter, but a `TableScan` for `$parent.x` (measured,
		`findings/P1.6-builder.md` § 9), so this is the form that uses the join column's index (`parent@ci`, ...). A LEFT JOIN
		keeps a row without a match with the joined side NONE (which every predicate treats as NULL). The whole ON criterion is
		part of the WHERE of the step, so extra conditions and any number of tables work; without an index on the join column
		the step scans the table once per row (quadratic).

		`page` = (ordering of the first table, row count): when the query is a LEFT JOIN paged by ORDER BY/LIMIT on the first
		table alone, only the ids of the first `count` rows of that table are selected (ordered, without copying whole records)
		and only those records are joined - every row of the first table yields at least one joined row, so no later row can
		belong to the page."""
		ctxs = self.ctxs
		table = quote_table(ctxs[0].schema.name)
		where = f" WHERE {pushed_sql}" if pushed_sql else ""
		if page is None:
			source = f"(SELECT VALUE {{ `t0`: $this }} FROM {table}{where})"
		else:
			order, count = page
			keys = "".join(f", {key} AS `__p{i}`" for i, (key, _) in enumerate(order))
			ordering = (
				" ORDER BY " + ", ".join(f"`__p{i}` {d}" for i, (_, d) in enumerate(order)) if order else ""
			)
			ids = f"SELECT id{keys} FROM {table}{where}{ordering} LIMIT {count}"
			source = f"(SELECT VALUE {{ `t0`: $this }} FROM (SELECT VALUE id FROM ({ids})))"
		for k in range(1, len(ctxs)):
			ctx, join = ctxs[k], joins[k - 1]
			for earlier in ctxs[:k]:
				earlier.prefix = f"$r.`{earlier.alias}`."
			ctx.prefix = ""
			condition = self.predicate(join.criterion)
			carried = ", ".join(f"`{c.alias}`: $r.`{c.alias}`" for c in ctxs[:k])
			matched = f"SELECT VALUE {{ {carried}, `{ctx.alias}`: $this }} FROM {quote_table(ctx.schema.name)} WHERE {condition}"
			if _attr_name(join.how).lower() == "":
				body = f"({matched})"
			else:
				body = (
					f"{{ LET $m = ({matched}); IF array::len($m) = 0 "
					f"{{ [{{ {carried}, `{ctx.alias}`: NONE }}] }} ELSE {{ $m }} }}"
				)
			source = f"array::flatten(array::map({source}, |$r| {body}))"
		for ctx in ctxs:
			ctx.prefix = f"`{ctx.alias}`."
		return f"({source})"

	def _from_and_where(self, q) -> tuple[str, str | None]:
		"""Bind the FROM/JOIN tables; returns (source for the FROM clause, WHERE text or None)."""
		joins = list(_attr(q, "_joins") or [])
		consumed: set[int] = set()
		if len(q._from) > 1:
			if joins:
				unsupported("a comma join mixed with JOIN ... ON", P1_6C)
			# Frappe's legacy comma join (`FROM a, b WHERE a.x = b.y`): a cross-table equality conjunct is
			# exactly an INNER JOIN on it (the comma join is the cross product filtered by the WHERE), so the
			# equalities become the join steps and the remaining conjuncts stay in the WHERE.
			joins, consumed = self._comma_joins(q)
		self.ctxs = [self._make_ctx(q._from[0], "t0")]
		for i, join in enumerate(joins, 1):
			if _kind(join) not in ("JoinOn", "_CommaJoin"):
				unsupported("JOIN ... USING", P1_6C)
			how = _attr_name(join.how).lower()
			if how not in ("", "left", "left outer"):
				unsupported(f"a {how} join", P1_6C)
			self.ctxs.append(self._make_ctx(join.item, f"t{i}"))
		if not joins:
			return quote_table(self.ctxs[0].schema.name), (
				self.predicate(q._wheres) if q._wheres is not None else None
			)
		# WHERE conjuncts that concern only the first table are applied before joining
		pushed, rest = [], []
		for c in self._conjuncts(q._wheres) if q._wheres is not None else []:
			if id(c) in consumed:
				continue  # consumed by a synthesized comma-join step
			(pushed if self._only_main(c) else rest).append(c)
		self.ctxs[0].prefix = ""
		pushed_sql = " AND ".join(self.predicate(c) for c in pushed)
		page = None
		if (
			q._limit is not None
			and not rest
			and all(_attr_name(j.how).lower() != "" for j in joins)
			and not (q._groupbys or q._distinct or q._havings)
			and not any(self._has_aggregate(t) for t in q._selects)
			and all(self._only_main(field) for field, _ in q._orderbys or [])
		):
			page = (
				[self._order(field, order) for field, order in q._orderbys or []],
				int(q._limit) + int(q._offset or 0),
			)
		source = self._joined_source(joins, pushed_sql, page)
		where = " AND ".join(self.predicate(c) for c in rest) if rest else None
		return source, where

	def _comma_joins(self, q) -> tuple[list, set]:
		"""The INNER JOIN steps of a legacy comma join, in an order that connects every table, with the ids of
		the WHERE conjuncts they consume. A conjunct qualifies when both of its sides are plain columns of one
		FROM table each; other conjuncts stay in the WHERE (the cross product is filtered there, as MariaDB does)."""
		if any(_kind(t) != "Table" for t in q._from[1:]):
			unsupported("a derived table in a comma join", P1_6C)
		eqs = []
		for c in self._conjuncts(q._wheres) if q._wheres is not None else []:
			if _kind(c) != "BasicCriterion" or _sql_word(c.comparator) != "=":
				continue
			sides = []
			for side in (c.left, c.right):
				try:
					fields = side.fields_()
				except Exception:
					fields = None
				tables = {id(f.table) for f in fields or [] if getattr(f, "table", None) is not None}
				if not fields or len(tables) != 1 or any(getattr(f, "table", None) is None for f in fields):
					break
				sides.append(next(iter(tables)))
			else:
				if sides[0] != sides[1]:
					eqs.append((c, sides[0], sides[1]))
		order, joins, consumed = [id(q._from[0])], [], set()
		by_id = {id(t): t for t in q._from}
		while len(order) < len(q._from):
			for c, a, b in eqs:
				if id(c) in consumed or (a in order) == (b in order):
					continue  # already connected, or neither side is bound yet
				table_id = b if a in order else a
				if table_id not in by_id or table_id in order:
					continue
				joins.append(_CommaJoin(by_id[table_id], c))
				consumed.add(id(c))
				order.append(table_id)
				break
			else:
				unsupported("a comma join whose tables are not connected by equality conjuncts", P1_6C)
		return joins, consumed

	@staticmethod
	def _has_aggregate(term) -> bool:
		try:
			return any(_is_aggregate(node) for node in term.nodes_())
		except Exception:
			return _is_aggregate(term)

	def select(self, q, hints: bool = True) -> str:
		self._reject(q, "_union", "_with", "_prewheres")
		self._aliases = {}
		source, where = self._from_and_where(q)
		mode = self._for_update_mode(q)
		grouped = q._groupbys or q._havings or any(self._has_aggregate(t) for t in q._selects)
		if q._distinct and not grouped:
			grouped = not self._distinct_noop(q)
		if grouped:
			if mode is not None:
				unsupported("FOR UPDATE with GROUP BY / DISTINCT / HAVING / aggregates", "P1.8")
			return self.grouped_select(q, source, where, hints)
		columns = self._projection(q._selects, hints)
		self._aliases = {self._display_name(term, None): term for term in q._selects if _kind(term) != "Star"}
		hidden = []
		ordering = []
		for field, order in q._orderbys or []:
			expr, direction = self._order(field, order)
			ordering.append(f"`__o{len(ordering)}` {direction}")
			hidden.append(f"{expr} AS `__o{len(hidden)}`")
		projection = [c["sql"] for c in columns] + hidden
		key = None
		if mode is not None:
			if self.shared.prelude:
				unsupported("FOR UPDATE with sub-queries", "P1.8")
			key = self._lock_key(q)
			projection.append("id AS `__lk0`")  # each row's id: the lock key and the SKIP LOCKED filter
		parts = [f"SELECT {', '.join(projection)} FROM {source}"]
		if where is not None:
			parts.append(f"WHERE {where}")
		if ordering:
			parts.append("ORDER BY " + ", ".join(ordering))
		if mode is None:
			return self._finish(parts, q, columns, hints)
		if key is not None:
			# `name = <literal>` in the WHERE: lock that record before the read (get_value's shape)
			return self._finish(parts, q, columns, hints) + f" /*lock:{mode}:k:{self.ctxs[0].schema.name}:{key}*/"
		return self._for_update_script(q, mode, source, where, ordering, hidden, parts, columns, hints)

	# --- FOR UPDATE (P1.8: application locks and fences, ADR 0002) ----------------------------------------------
	def _for_update_mode(self, q) -> str | None:
		"""None, or `l` (blocking) / `n` (NOWAIT) / `s` (SKIP LOCKED) when the query is a `for_update` read."""
		if not _attr(q, "_for_update", False):
			return None
		if _attr(q, "_for_update_of", None):
			unsupported("FOR UPDATE OF ...", "P1.8")
		if _attr(q, "_for_update_nowait", False):
			return "n"
		if _attr(q, "_for_update_skip_locked", False):
			return "s"
		return "l"

	def _lock_key(self, q) -> str | None:
		"""A `name = <literal>` conjunct in the WHERE: the record id whose lock stands for the row, so the
		lock is granted *before* the read and the read sees the previous holder's commit (P0.4 a1)."""
		if q._wheres is None:
			return None
		schema = self.ctxs[0].schema
		for term in self._conjuncts(q._wheres):
			if _kind(term) != "BasicCriterion" or _sql_word(term.comparator) != "=":
				continue
			left = term.left
			if _kind(left) != "Field" or left.name != "name" or (
				left.table is not None and not self._same_table(left.table, self.ctxs[0].table)
			):
				continue
			is_literal, value = self._literal(term.right)
			if is_literal:
				return self._record_key(schema, value)
		return None

	def _for_update_script(self, q, mode: str, source: str, where, ordering: list, hidden: list, parts, columns, hints: bool) -> str:
		"""FOR UPDATE as a two-statement script (P1.8, ADR 0002): first the ids to lock, then the query.
		The cursor grants the application locks (`__lock` CAS plus a `__fence` write) between the two and
		re-runs the script, so the caller reads the locked rows' fresh state. SKIP LOCKED (`s`) claims only
		unlocked rows and drops the rest of the result instead."""
		schema = self.ctxs[0].schema
		limit = int(q._limit) if q._limit is not None else None
		ids = ["SELECT " + ", ".join(["id AS `__lk0`", *hidden]) + f" FROM {source}"]
		if where is not None:
			ids.append(f"WHERE {where}")
		if ordering:
			ids.append("ORDER BY " + ", ".join(ordering))
		if mode != "s":
			# block on exactly the rows the query returns
			if limit is not None:
				ids.append(f"LIMIT {limit}")
			if q._offset:
				ids.append(f"START {int(q._offset)}")
			main = self._finish(parts, q, columns, hints)
		else:
			# candidates come without LIMIT (the cursor claims the first `limit` unlocked ones in query
			# order), and the main select runs without LIMIT/START so its rows can be filtered down to
			# the claimed ids
			main = self._finish(parts, q, columns, hints, page=False)
		script = "; ".join([" ".join(ids), main])
		return script + f" /*lock:{mode}:t:{schema.name}:{'' if limit is None else limit}*/"

	def _finish(self, parts: list, q, columns: list, hints: bool, page: bool = True) -> str:
		if page and q._limit is not None:
			parts.append(f"LIMIT {int(q._limit)}")
		if page and q._offset:
			parts.append(f"START {int(q._offset)}")
		self.result_specs = [c["spec"] for c in columns]
		text = " ".join(parts)
		if not hints:
			return text
		keys, names = [c["key"] for c in columns], [c["name"] for c in columns]
		kinds = ",".join(c["spec"].kind for c in columns)
		text += f" /*cols:{','.join(keys)}*/"
		if names != keys:
			text += " /*names:" + json.dumps(names, ensure_ascii=False).replace("/", "\\/") + "*/"
		return text + f" /*kinds:{kinds}*/"

	def _materialize(self, e: Expr) -> Expr:
		"""A constant in a projection becomes a bound parameter."""
		if not e.is_const:
			return e
		fam = self._family(e) or "str"
		if fam == "num":
			spec = (
				_synthetic("bigint")
				if isinstance(e.const, bool | int)
				else _synthetic("decimal", _scale_of(None, e.const))
			)
		else:
			spec = _synthetic("varchar" if fam == "str" else fam)
		return Expr(self._bind_const(e.const, fam)[0], spec)

	def _display_name(self, term, e: Expr) -> str:
		alias = getattr(term, "alias", None)
		if alias:
			return alias
		if _kind(term) == "Field":
			return term.name
		try:
			return term.get_sql(quote_char="`")
		except Exception:
			return "expr"

	def _column_entry(self, index: int, e: Expr, name: str, plain: bool) -> dict:
		"""One projected column. `plain`: a column of a single table, keyed by its own name; otherwise keyed `__cN`
		(names repeat across joined tables and expressions have none), with the display name in the `/*names:*/` hint."""
		e = self._materialize(e)
		sql = e.sql
		if self._key_projection and index == 0 and e.spec.has_collation_shadow:
			if e.ci is None:
				unsupported(
					"a computed string used as a sub-query key (its collation key cannot be computed)", P1_6C
				)
			sql = e.ci
		if plain:
			if not _NAME.match(name):
				unsupported(f"the result column name {name!r}", "P1.6")
			text = sql if sql == quote(name) else f"{sql} AS {quote(name)}"
			return {"sql": text, "key": name, "name": name, "spec": e.spec}
		return {"sql": f"{sql} AS `__c{index}`", "key": f"__c{index}", "name": name, "spec": e.spec}

	def _projection(self, selects, hints: bool = True) -> list:
		columns = []
		single = len(self.ctxs) == 1 and hints  # one table, top level: keys are the column names
		for term in selects:
			if _kind(term) == "Star":
				owner = getattr(term, "table", None)
				for ctx in [c for c in self.ctxs if owner is None or self._same_table(c.table, owner)]:
					for spec in ctx.schema.columns.values():
						columns.append(
							self._column_entry(len(columns), self._col(ctx, spec), spec.name, single)
						)
				continue
			e = self.expr(term)
			columns.append(
				self._column_entry(
					len(columns), e, self._display_name(term, e), single and _kind(term) == "Field"
				)
			)
		return columns

	def _distinct_noop(self, q) -> bool:
		"""`SELECT DISTINCT` is a no-op when the projection holds the primary key of the one table it reads
		(`name`): every output row is one record, so no two rows can be equal - the report view's
		`SELECT DISTINCT name` needs neither GROUP BY nor sorting, just the rows."""
		if len(self.ctxs) != 1 or self.ctxs[0].schema.column("name") is None:
			return False
		main = self.ctxs[0]
		for term in q._selects:
			table = getattr(term, "table", None)
			if _kind(term) == "Star":
				if table is None or self._same_table(main.table, table):
					return True
				continue
			if _kind(term) != "Field" or term.name != "name":
				continue
			if table is None or self._same_table(main.table, table):
				return True
		return False

	def _order(self, field, order) -> tuple[str, str]:
		if (
			_kind(field) == "Field"
			and field.name in self._aliases
			and all(c.schema.column(field.name) is None for c in self.ctxs)
		):
			# ORDER BY <select alias>: the name is not a column of any table read, so it must name the projection's
			# output (MariaDB resolves ORDER BY names against the output columns first; pypika qualifies a string
			# order_by with the FROM table). A name that is also a real column stays a direct reference - with
			# `SELECT p.name, k.name` both terms display as "name" and the map cannot tell them apart.
			field = self._aliases[field.name]
		e = self.expr(field)
		if e.is_const:
			unsupported("ORDER BY a constant", "P1.6")
		if e.kind in ("text", "json") and not (e.spec is not None and e.spec.has_collation_shadow):
			unsupported(
				f"ORDER BY a {e.spec.logical} column (not in the text-collation-shadow allow-list)", "P1.6"
			)
		if e.spec.has_collation_shadow:
			if e.ci is None:
				unsupported(
					"ORDER BY a computed string (its collation key cannot be computed in SurrealQL)", P1_6C
				)
			key = e.ci
		else:
			key = e.sql
		direction = "DESC" if order is not None and _sql_word(order) == "desc" else "ASC"
		return key, direction

	# --- aggregates, GROUP BY, DISTINCT, HAVING ------------------------------------------------------------------------------
	# Two levels, because SurrealDB cannot nest aggregates and returns non-grouped values as arrays (P0.3, measured):
	#   SELECT <display> FROM (SELECT <keys>, <aggregates> FROM t WHERE .. GROUP BY <keys> | GROUP ALL) WHERE <having> ORDER BY ..
	# * a varchar group key is the collation shadow (MariaDB groups collation-equal strings together); the value shown is one
	#   member of the group (`array::first(array::group(col))`), as MariaDB shows one arbitrary member;
	# * a column that is not grouped (MariaDB's non-strict GROUP BY) shows the first value of the group;
	# * SUM / MIN / MAX of nothing (empty set, or all NULL) are 0 / +-Infinity in SurrealDB and NULL in MariaDB: each carries the
	#   count of non-NULL inputs and the outer level turns "none" into NULL;
	# * COUNT(col) counts non-NULL values (`count(col != NULL AND col != NONE)`), COUNT(*) is `count()`.
	def grouped_select(self, q, source: str, where: str | None, hints: bool) -> str:
		group_terms = list(q._groupbys)
		if q._distinct and not group_terms and not any(self._has_aggregate(t) for t in q._selects):
			group_terms = list(
				q._selects
			)  # SELECT DISTINCT a, b == GROUP BY a, b (an aggregate row is one row anyway)
		g = Grouping(self)
		for term in group_terms:
			g.add_key(term)
		self.outer = g
		try:
			self._aliases = {self._display_name(term, None): term for term in q._selects if _kind(term) != "Star"}
			columns = []
			for term in q._selects:
				if _kind(term) == "Star":
					unsupported("SELECT * in an aggregate query", "P1.6")
				e = self.expr(term)
				columns.append(self._column_entry(len(columns), e, self._display_name(term, e), False))
			having = self.predicate(q._havings) if q._havings is not None else None
			hidden, ordering = [], []
			for field, order in q._orderbys or []:
				key, direction = self._order(field, order)
				hidden.append(f"{key} AS `__o{len(hidden)}`")
				ordering.append(f"`__o{len(ordering)}` {direction}")
		finally:
			self.outer = None
		inner_sql = f"SELECT {', '.join(g.inner)} FROM {source}"
		if where is not None:
			inner_sql += f" WHERE {where}"
		inner_sql += f" GROUP BY {', '.join(g.group_by)}" if g.group_by else " GROUP ALL"
		parts = [f"SELECT {', '.join([c['sql'] for c in columns] + hidden)} FROM ({inner_sql})"]
		if having:
			parts.append(f"WHERE {having}")
		if ordering:
			parts.append("ORDER BY " + ", ".join(ordering))
		return self._finish(parts, q, columns, hints)

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
		"""The stored fields a column value produces: the column itself and, for a collation-shadow column
		(varchar or allow-listed text), its shadows; a shadowed text column also carries the @hash witness
		(P1.15). A NULL value produces all-NONE shadows - writing them removes the fields (P0.8)."""
		name = physical(spec.name)
		out = {name: encoded}
		if spec.has_collation_shadow:
			shadows = text_shadows.build_collation_shadows(spec, encoded)
			out[name + SHADOW_CI] = shadows["ci"]
			out[name + SHADOW_LIKE] = shadows["like"]
			if spec.has_integrity_hash:
				out[name + SHADOW_HASH] = shadows["hash"]
		return out

	def _record_key(self, schema: TableSchema, value) -> str:
		kind = schema.name_kind
		if value is None:
			raise SurrealDBProgrammingError(1048, "Column 'name' cannot be null")
		if kind == "varchar":
			return collation.record_id(values.to_str(value))
		return str(values.to_int(value, "bigint")) if kind == "bigint" else str(value).lower()

	def insert(self, q) -> str:
		self._reject(q, "_on_conflict", "_select", "_returns", "_with")
		schema = self._bind_table(q._insert_table)
		system = schema.name in SYSTEM_KEYS
		columns = []
		for c in q._columns:
			name = c.name if _kind(c) == "Field" else str(c)
			if schema.column(name) is None:
				raise SurrealDBProgrammingError(1054, f"Unknown column '{name}' in 'field list'")
			columns.append(schema.column(name))
		if not system and "name" not in [c.name for c in columns]:
			raise SurrealDBProgrammingError(1364, "Field 'name' doesn't have a default value")
		if not q._values:
			unsupported("INSERT without VALUES", "P1.6")
		rows = []
		for row in q._values:
			if len(row) != len(columns):
				raise SurrealDBProgrammingError(1136, "Column count doesn't match value count")
			record = {}
			key = None
			plain = {}
			for spec, term in zip(columns, row, strict=True):
				is_literal, value = self._literal(term)
				if not is_literal:
					unsupported("INSERT of an expression", "P1.6")
				encoded = self._encode(spec, value)
				plain[spec.name] = encoded
				if spec.name == "name" and not system:
					key = self._record_key(schema, encoded)
				record.update(self._stored_fields(spec, encoded))
			if system:
				key = system_record_id(schema.name, plain)
			record["id"] = key
			rows.append(record)
		duplicates = _attr(q, "_duplicate_updates") or []
		if duplicates and _attr(q, "_ignore", False):
			unsupported("INSERT IGNORE ... ON DUPLICATE KEY UPDATE", "P1.6")
		verb = "INSERT IGNORE INTO" if _attr(q, "_ignore", False) else "INSERT INTO"
		placeholder = self.params.add(rows)
		text = f"{verb} {quote_table(schema.name)} {placeholder}"
		if verb == "INSERT INTO" and not duplicates:
			# the driver pre-checks the table's unique indexes against these rows before writing (P1.8);
			# INSERT IGNORE and ON DUPLICATE KEY UPDATE have their own duplicate handling
			text += f" /*uq:{schema.name}:{placeholder[1:]}*/"
		if duplicates:
			text += " ON DUPLICATE KEY UPDATE " + ", ".join(self._duplicate_assignments(schema, duplicates))
		return text + " RETURN NONE"

	def _duplicate_assignments(self, schema: TableSchema, duplicates) -> list[str]:
		"""`ON DUPLICATE KEY UPDATE col = VALUES(col) | constant`. It fires when the record id exists (the id is the primary key:
		`name`, or the composite key of a system table)."""
		out = []
		for field, term in duplicates:
			if _is_wrapper_of_term(term):
				term = term.value
			spec = schema.column(field.name if _kind(field) == "Field" else str(field))
			if spec is None:
				raise SurrealDBProgrammingError(1054, f"Unknown column '{field.name}' in 'field list'")
			if _kind(term) == "Values":
				source = schema.column(term.field.name)
				if source is None or source.kind != spec.kind:
					unsupported("VALUES() of a column of another type", "P1.6")
				stored = [physical(spec.name)] + (
					[physical(spec.name) + SHADOW_CI, physical(spec.name) + SHADOW_LIKE]
					+ ([physical(spec.name) + SHADOW_HASH] if spec.has_integrity_hash else [])
					if spec.has_collation_shadow
					else []
				)
				src = [physical(source.name)] + (
					[physical(source.name) + SHADOW_CI, physical(source.name) + SHADOW_LIKE]
					+ ([physical(source.name) + SHADOW_HASH] if source.has_integrity_hash else [])
					if source.has_collation_shadow
					else []
				)
				if (spec.has_integrity_hash or source.has_integrity_hash) and len(stored) != len(src):
					# P1.15 fail-closed: a hashed target needs a source that carries its own witness.
					unsupported(
						"VALUES() of a column with different shadowing (the @hash witness cannot be copied)",
						"P1.6",
					)
				out += [f"{quote(a)} = $input.{quote(b)}" for a, b in zip(stored, src, strict=True)]
				continue
			is_literal, value = self._literal(term)
			if not is_literal:
				unsupported("ON DUPLICATE KEY UPDATE with an expression", "P1.6")
			fields = self._stored_fields(spec, self._encode(spec, value))
			out += [f"{quote(f)} = {self.params.add(v)}" for f, v in fields.items()]
		return out

	def _assignments(self, spec: ColumnSpec, term) -> list[str]:
		"""Assignments (`field = expr` strings) that set column `spec` to `term`."""
		e = self.expr(term)
		if e.is_const:
			fields = self._stored_fields(spec, self._encode(spec, e.const))
			return [f"{quote(f)} = {self.params.add(v)}" for f, v in fields.items()]
		name = physical(spec.name)
		ref = quote(name)
		if spec.kind in NUMERIC_KINDS:
			if e.kind not in NUMERIC_KINDS:
				unsupported(f"storing a {e.kind} expression in a {spec.logical} column", P1_6C)
			if spec.kind == "decimal":
				sql = f"IF {_absent(e.sql)} THEN NULL ELSE {self._round_sql(e.sql, precision_scale(spec.arg)[1])} END"
			elif e.kind in INT_KINDS:
				sql = e.sql
			else:  # MariaDB rounds half away from zero when a decimal is stored in an integer column
				sql = f"IF {_absent(e.sql)} THEN NULL ELSE <int>{self._round_sql(e.sql, 0)} END"
			return [f"{ref} = {sql}"]
		if spec.has_collation_shadow:
			if self._family(e) != "str" or e.ci is None or e.like is None or e.opaque_string:
				unsupported(
					"storing a computed string (its collation shadows cannot be computed in SurrealQL)", P1_6C
				)
			out = [
				f"{ref} = {e.sql}",
				f"{quote(name + SHADOW_CI)} = {e.ci}",
				f"{quote(name + SHADOW_LIKE)} = {e.like}",
			]
			if spec.has_integrity_hash:
				# P1.15 fail-closed: the @hash witness of a computed string cannot be produced here; only
				# a plain column of another shadowed text column carries a copyable witness.
				if e.hash is None:
					unsupported(
						"storing a computed string in a hashed text column (its @hash witness cannot be copied)",
						P1_6C,
					)
				out.append(f"{quote(name + SHADOW_HASH)} = {e.hash}")
			return out
		if spec.kind in TEMPORAL_KINDS and e.kind == spec.kind:
			return [f"{ref} = {e.sql}"]
		unsupported(f"storing a {e.kind} expression in a {spec.logical} column", P1_6C)

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
			assignments += self._assignments(spec, term)
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


class _RowMode:
	"""Compile expressions against the rows of the FROM clause, not the outer level of an aggregate query."""

	def __init__(self, renderer: Renderer):
		self.renderer = renderer

	def __enter__(self):
		self.saved, self.renderer.outer = self.renderer.outer, None

	def __exit__(self, *exc):
		self.renderer.outer = self.saved


class Grouping:
	"""The inner level of an aggregate query: group keys, aggregates and 'first value' columns, and how the outer level reads them."""

	def __init__(self, renderer: Renderer):
		self.r = renderer
		self.inner: list[str] = []
		self.group_by: list[str] = []
		self.keys: dict = {}  # term identity -> outer Expr
		self.first: dict = {}
		self.aggs: dict = {}
		self._n = 0

	def _id(self) -> int:
		self._n += 1
		return self._n

	def add_key(self, term):
		r = self.r
		with r._row_mode():
			e = r.expr(term)
		e = r._materialize(e) if e.is_const else e
		if e.kind in ("text", "json") and not (e.spec is not None and e.spec.has_collation_shadow):
			unsupported(
				f"GROUP BY a {e.spec.logical} value (not in the text-collation-shadow allow-list)", "P1.6"
			)
		i = self._id()
		if e.spec.has_collation_shadow:
			if e.ci is None:
				unsupported(
					"GROUP BY a computed string (its collation key cannot be computed in SurrealQL)", P1_6C
				)
			self.inner += [f"{e.ci} AS `__k{i}`", f"array::group({e.sql}) AS `__v{i}`"]
			self.group_by.append(f"`__k{i}`")  # SurrealDB groups by the alias of a selected expression
			out = Expr(f"array::first(`__v{i}`)", e.spec, ci=f"`__k{i}`")
		else:
			self.inner.append(f"{e.sql} AS `__k{i}`")
			self.group_by.append(f"`__k{i}`")
			out = Expr(f"`__k{i}`", e.spec)
		self.keys[r._tkey(term)] = out

	def column(self, ctx: TableCtx, spec: ColumnSpec) -> Expr:
		"""A column in the outer level: a group key, else the first value of the group (MariaDB's non-strict GROUP BY)."""
		key = ("col", ctx.alias, spec.name)
		if key in self.keys:
			return self.keys[key]
		if key not in self.first:
			e = self.r._col(ctx, spec)
			i = self._id()
			self.inner.append(f"{e.sql} AS `__n{i}`")
			out = Expr(f"array::first(`__n{i}`)", spec)
			if spec.has_collation_shadow:
				self.inner += [f"{e.ci} AS `__n{i}c`", f"{e.like} AS `__n{i}l`"]
				out.ci, out.like = f"array::first(`__n{i}c`)", f"array::first(`__n{i}l`)"
			self.first[key] = out
		return self.first[key]

	def aggregate(self, term) -> Expr:
		r = self.r
		key = r._tkey(term)
		if key in self.aggs:
			return self.aggs[key]
		name = str(term.name).strip().upper()
		distinct = bool(_attr(term, "_distinct", False))
		arg = term.args[0] if term.args else None
		with r._row_mode():
			star = arg is None or _kind(arg) == "Star"
			e = None if star else r.expr(arg)
		if e is not None and e.is_const:
			if name == "COUNT" and e.const is not None:
				star, e = True, None
			else:
				unsupported(f"{name} of a constant", P1_6C)
		i = self._id()
		out = {
			"COUNT": self._count,
			"SUM": self._sum,
			"AVG": self._avg,
			"MIN": self._min_max,
			"MAX": self._min_max,
			"GROUP_CONCAT": self._group_concat,
		}.get(name)
		if out is None:
			unsupported(f"the aggregate {name}", P1_6C)
		self.aggs[key] = result = out(term, name, e, i, distinct, star)
		return result

	def _count(self, term, name, e, i, distinct, star):
		if star:
			self.inner.append(f"count() AS `__a{i}`")
			return Expr(f"`__a{i}`", _synthetic("bigint"))
		if distinct:
			if e.kind in ("text", "json") and not (e.spec is not None and e.spec.has_collation_shadow):
				unsupported(
					"COUNT(DISTINCT long text (not in the text-collation-shadow allow-list))", P1_6C
				)
			if e.spec.has_collation_shadow and e.ci is None:
				unsupported("COUNT(DISTINCT computed string)", P1_6C)
			self.inner.append(f"array::group({e.ci if e.spec.has_collation_shadow else e.sql}) AS `__a{i}`")
			return Expr(
				f"array::len(array::complement(array::distinct(`__a{i}`), [NULL, NONE]))",
				_synthetic("bigint"),
			)
		self.inner.append(f"count({_present(e.sql)}) AS `__a{i}`")
		return Expr(f"`__a{i}`", _synthetic("bigint"))

	def _numeric_inner(self, name, e, i, distinct):
		if distinct:
			unsupported(f"{name}(DISTINCT ...)", P1_6C)
		if e.kind not in NUMERIC_KINDS:
			unsupported(f"{name} of a {e.kind} value", "P1.6")

	def _sum(self, term, name, e, i, distinct, star):
		self._numeric_inner(name, e, i, distinct)
		self.inner += [f"math::sum({e.sql}) AS `__a{i}`", f"count({_present(e.sql)}) AS `__n{i}`"]
		return Expr(f"IF `__n{i}` = 0 THEN NULL ELSE `__a{i}` END", _synthetic("decimal", _scale_of(e.spec)))

	def _avg(self, term, name, e, i, distinct, star):
		self._numeric_inner(name, e, i, distinct)
		self.inner += [f"math::sum({e.sql}) AS `__a{i}`", f"count({_present(e.sql)}) AS `__n{i}`"]
		scale = _scale_of(e.spec) + 4
		mean = self.r._round_sql(f"<decimal>`__a{i}` / <decimal>`__n{i}`", scale)
		return Expr(f"IF `__n{i}` = 0 THEN NULL ELSE {mean} END", _synthetic("decimal", scale))

	def _min_max(self, term, name, e, i, distinct, star):
		if e.kind not in (*NUMERIC_KINDS, "time", "date", "datetime"):
			unsupported(
				f"{name} of a {e.kind} value (needs the value behind the smallest collation key)", "P1.6"
			)
		# Collect the group's values, drop NULL/NONE and take an end of the sorted ones. `math::max` skips NULLs in a
		# mixed group, but errors on a stored NULL and answers -inf/+inf when every value of the group is NULL/NONE -
		# MariaDB answers NULL (measured, P1.6c). Dates are stored as text, where math::min/max would compare lexically.
		self.inner.append(f"array::group({e.sql}) AS `__a{i}`")
		pick = "first" if name == "MIN" else "last"
		return Expr(f"array::{pick}(array::sort(array::complement(`__a{i}`, [NULL, NONE])))", e.spec)

	def _group_concat(self, term, name, e, i, distinct, star):
		if distinct or star:
			unsupported("GROUP_CONCAT(DISTINCT ...)", P1_6C)
		if e.kind in INT_KINDS:
			element = f"<string>{e.sql}"
		elif e.kind in ("varchar", "text", "date", "datetime"):
			element = e.sql
		else:
			unsupported(f"GROUP_CONCAT of a {e.kind} value", P1_6C)
		separator = _attr(term, "_separator", ",")
		if not isinstance(separator, str):
			unsupported("GROUP_CONCAT with a non-text separator", P1_6C)
		self.inner.append(f"{element} AS `__a{i}`")
		present = f"array::complement(`__a{i}`, [NULL, NONE])"
		sep = self.r.params.add(separator)
		return Expr(
			f"IF array::len({present}) = 0 THEN NULL ELSE array::join({present}, {sep}) END",
			_synthetic("varchar"),
		)


class OuterScope:
	"""The outer query's row, bound for a correlated scalar sub-query (P1.6c).

	SurrealDB evaluates the sub-query once per outer row, but a bare column name inside it resolves against the
	*inner* tables (measured: it does not reach the outer row), and `$parent.x` plans a TableScan instead of an
	index lookup (findings/P1.6-builder.md § 9). So each outer column the sub-query references becomes a key of a
	one-element object that an `array::map` closure passes to the sub-query as `$o` - the binding the join renderer
	uses, which keeps the inner column's index usable. A varchar outer column contributes its collation key, so the
	comparison inside the sub-query is case-insensitive like MariaDB's."""

	def __init__(self, host: "Renderer", child: "Renderer"):
		self.host = host  # the statement whose row the object carries (one level out)
		self.child = child
		self.bundle: list[str] = []  # `` `kN`: <SurrealQL of the outer value>`` entries of the closure object
		self.refs: dict = {}  # (table id, column name) -> the bound `$o.`kN`` expression
		self._n = 0

	@property
	def bound(self) -> bool:
		return bool(self.bundle)

	def try_resolve(self, field) -> "Expr | None":
		"""The bound reference for an outer column, or None when `field` belongs to the sub-query itself."""
		table = getattr(field, "table", None)
		if table is not None:
			if any(self.child._same_table(c.table, table) for c in self.child.ctxs):
				return None  # the sub-query binds this table itself: it shadows the outer one
			for ctx in self.host.ctxs:
				if self.host._same_table(ctx.table, table):
					return self._bind(ctx, field)
			return None  # not bound here (an outer query further out or unknown): `_owner` decides
		if any(c.schema.column(field.name) is not None for c in self.child.ctxs):
			return None  # an unqualified column of the sub-query
		matches = [c for c in self.host.ctxs if c.schema.column(field.name) is not None]
		if len(matches) == 1:
			return self._bind(matches[0], field)
		if len(matches) > 1:
			raise SurrealDBProgrammingError(1052, f"Column '{field.name}' in field list is ambiguous")
		return None  # no such column anywhere: the child's own error (1054)

	def _bind(self, ctx: TableCtx, field) -> Expr:
		if self.host.outer is not None:
			# a column of the outer row cannot be reached from the group level (it would need the Grouping's
			# `array::first` forms, which the sub-query's row context cannot see)
			unsupported("a correlated sub-query at the outer level of an aggregate query", P1_6C)
		spec = ctx.schema.column(field.name)
		if spec is None:
			raise SurrealDBProgrammingError(1054, f"Unknown column '{field.name}' in 'field list'")
		key = (id(ctx.table), field.name)
		ref = self.refs.get(key)
		if ref is not None:
			return ref
		e = self.host._col(ctx, spec)
		self._n += 1
		name = f"k{self._n}"
		if spec.has_collation_shadow:
			self.bundle += [f"{quote(name)}: {e.ci}", f"{quote(name + 'l')}: {e.like}"]
			ref = Expr(f"$o.{quote(name)}", spec, ci=f"$o.{quote(name)}", like=f"$o.{quote(name + 'l')}")
		else:
			self.bundle.append(f"{quote(name)}: {e.sql}")
			ref = Expr(f"$o.{quote(name)}", spec)
		self.refs[key] = ref
		return ref

	def carry(self, e: Expr) -> Expr:
		"""Carry an arbitrary outer expression (the left operand of a correlated IN) through the closure object,
		under a fresh key (the sub-query's own references took k1..kN while it was rendered)."""
		self._n += 1
		name = f"k{self._n}"
		if e.spec is not None and e.spec.has_collation_shadow:
			self.bundle += [f"{quote(name)}: {e.ci}", f"{quote(name + 'l')}: {e.like}"]
			return Expr(f"$o.{quote(name)}", e.spec, ci=f"$o.{quote(name)}", like=f"$o.{quote(name + 'l')}")
		self.bundle.append(f"{quote(name)}: {e.sql}")
		return Expr(f"$o.{quote(name)}", e.spec)


def render(query, param_wrapper=None, schema_loader=None):
	"""Render a PyPika query; returns (SurrealQL text, Params)."""
	params = Params(param_wrapper)
	return Renderer(params, schema_loader).render(query), params


__all__ = ["Params", "Renderer", "precision_scale", "render"]
