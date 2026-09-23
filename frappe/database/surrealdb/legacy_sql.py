"""Legacy interpolated-SELECT rewriter (chunk P1.6e).

Frappe's `DatabaseQuery` (frappe/model/db_query.py) and plain `frappe.db.sql` callers build
MariaDB-flavoured SQL text and interpolate their values. SurrealDB's SQL compatibility layer
accepts much of that text verbatim (measured probes: findings/P1.2, P1.6) but has no SurrealQL
meaning for a few MariaDB constructs: `LIKE`, `IN (..)`, `BETWEEN`, `LIMIT .. OFFSET ..`,
table-qualified ORDER BY columns, `count(*)` / `count(col)`, `select distinct` and
`DROP TABLE IF EXISTS` DDL.

`rewrite` turns exactly those constructs into SurrealQL. Every predicate leaf is parsed by the
P1.6d fragment parser and rendered by the ordinary predicate machinery, so the rewritten
predicates carry the same bound parameters, collation shadows and NULL guards a typed query
would render. Anything the machinery does not understand (placeholder parameters, joins,
correlated EXISTS, `coalesce` in the WHERE, GROUP BY, functions in the projection) is left
verbatim: the statement reaches the server unchanged and fails with the same 1064 the server
always raised - fail-closed, never approximated.
"""

import re
from itertools import pairwise

from pypika.queries import Table

from frappe.database.surrealdb.errors import SurrealDBProgrammingError
from frappe.database.surrealdb.schema import physical, quote, quote_table, table_schema, unquote
from frappe.database.surrealdb.translator import Params, Renderer, _FragmentParser

_CLAUSE_KW = re.compile(r"(?i)\b(where|group\s+by|order\s+by|limit)\b")
_DROP_TABLE = re.compile(r"(?is)^\s*drop\s+table\s+(?:if\s+exists\s+)?`?([\w@ ]+?)`?\s*;?\s*$")
_NOT = re.compile(r"(?is)^not\s+")


def rewrite(query, db=None, schema_loader=None) -> tuple[str, dict | None] | None:
	"""A legacy statement → (SurrealQL text, bound params); `None` = pass through verbatim.

	The pass-through contract: whatever this module does not fully understand is handed to the
	server untouched, so the server keeps raising the error it always raised (1064 / 1146 / 1054).
	Nothing is approximated and no construct is half-rewritten."""
	if not isinstance(query, str):
		return None
	m = _DROP_TABLE.match(query.strip())
	if m:
		try:
			return f"REMOVE TABLE IF EXISTS {quote_table(unquote(m[1]))}", None
		except Exception:
			return None
	try:
		return _rewrite_select(query.strip(), db, schema_loader)
	except SurrealDBProgrammingError:
		raise  # 1054/1052: MariaDB's own verdict, never replace it with a silently-NULLing query
	except Exception:
		return None


def rewrite_ddl(query, db=None) -> str | None:
	"""DDL variant of `rewrite`: only the `DROP TABLE [IF EXISTS]` shape is rewritten; the schema
	module's own `DEFINE`/`REMOVE` texts and every other DDL pass through unchanged."""
	m = _DROP_TABLE.match(query.strip()) if isinstance(query, str) else None
	if not m:
		return None
	try:
		return f"REMOVE TABLE IF EXISTS {quote_table(unquote(m[1]))}"
	except Exception:
		return None


# --- lexical helpers ----------------------------------------------------------------------------------------------
def _mask(text: str) -> str:
	"""Blank out single-quoted strings and anything inside parentheses: what remains shows the
	statement's top-level shape (keywords and separators) with positions preserved."""
	out: list[str] = []
	depth = 0
	in_str = False
	i = 0
	n = len(text)
	while i < n:
		c = text[i]
		if in_str:
			if c == "\\":
				out.append(" ")
				i += 2
				continue
			if c == "'":
				if text[i + 1 : i + 2] == "'":
					out.append("  ")
					i += 2
					continue
				in_str = False
			out.append(" ")
		elif c == "'":
			in_str = True
			out.append(" ")
		elif c == "(":
			depth += 1
			out.append(" ")
		elif c == ")":
			depth = max(0, depth - 1)
			out.append(" ")
		elif depth == 0:
			out.append(c)
		else:
			out.append(" ")
		i += 1
	return "".join(out)


def _split_top_commas(text: str) -> list[str]:
	parts: list[str] = []
	depth = 0
	in_str = False
	start = 0
	i = 0
	n = len(text)
	while i < n:
		c = text[i]
		if in_str:
			if c == "\\":
				i += 2
				continue
			if c == "'":
				if text[i + 1 : i + 2] == "'":
					i += 2
					continue
				in_str = False
			i += 1
			continue
		if c == "'":
			in_str = True
		elif c == "(":
			depth += 1
		elif c == ")":
			depth = max(0, depth - 1)
		elif c == "," and depth == 0:
			parts.append(text[start:i])
			start = i + 1
		i += 1
	parts.append(text[start:])
	return parts
# --- the statement shape ------------------------------------------------------------------------------------------
_PROJ_COLUMN = re.compile(r"(?i)^(?:`[\w@ ]+`\.)?`?([\w@]+)`?(?:\s+as\s+`?([\w@]+)`?)?$")
_PROJ_COUNT_STAR = re.compile(r"(?i)^count\s*\(\s*\*\s*\)(?:\s+as\s+`?([\w@]+)`?)?$")
_PROJ_COUNT_COL = re.compile(r"(?i)^count\s*\(\s*(?:`[\w@ ]+`\.)?`?([\w@]+)`?\s*\)(?:\s+as\s+`?([\w@]+)`?)?$")
_ORDER_TERM = re.compile(r"(?i)^(?:`[\w@ ]+`\.)?`?([\w@]+)`?\s*(asc|desc)?$")
_LIMIT = re.compile(r"(?i)^(?:limit\s+)?(\d+)\s*(?:,\s*(\d+)|offset\s+(\d+))?$")
_CLAUSE_ORDER = ("where", "group_by", "order_by", "limit")


def _rewrite_select(text, db, schema_loader) -> tuple[str, dict | None] | None:
	masked = _mask(text)
	from_m = re.search(r"(?i)\bfrom\b", masked)
	if not from_m:
		return None
	proj_text = text[: from_m.start()].strip()
	proj_text = re.sub(r"(?is)^select\s+", "", proj_text)
	distinct = bool(re.match(r"(?is)^distinct\b", proj_text))
	if distinct:
		proj_text = re.sub(r"(?is)^distinct\s+", "", proj_text)
	rest = text[from_m.end() :]

	# clauses: at most one each, in legacy order, all at the top level
	found = [
		(_CLAUSE_ORDER.index(m[1].lower().replace(" ", "_")), m[1].lower().replace(" ", "_"), m.start(), m.end())
		for m in re.finditer(_CLAUSE_KW, _mask(rest))
	]
	if any(a[0] >= b[0] for a, b in pairwise(found)):
		return None  # clauses out of legacy order, or one repeated
	head = rest[: found[0][2]] if found else rest
	clauses = {}
	for idx, (_, kw, _, end) in enumerate(found):
		stop = found[idx + 1][2] if idx + 1 < len(found) else None
		clauses[kw] = rest[end:stop].strip()
	tm = re.match(r"(?i)^\s*`([\w@ ]+)`\s*$", head)  # exactly one backticked table: aliases / JOINs / comma-tables refuse
	if not tm:
		return None
	table = tm[1]
	if set(clauses) - {"where", "order_by", "limit"}:
		return None  # GROUP BY and friends stay verbatim (fail-closed)
	try:
		schema = schema_loader(table) if schema_loader else table_schema(table, db=db)
	except Exception:
		return None  # unknown table: the server reports it (1146)
	# projection ---------------------------------------------------------------------------------------------------
	terms = [_render_projection_term(t.strip()) for t in _split_top_commas(proj_text)]
	if not terms or any(t is None for t in terms):
		return None
	aggregate = any(t[0] in ("count", "count_col") for t in terms)
	star = len(terms) == 1 and terms[0][0] == "star"
	if aggregate and (distinct or star or "order_by" in clauses):
		return None
	projection = []
	for t in terms:
		if t[0] == "star":
			projection.append("*")
		elif t[0] == "count":
			projection.append("count()" + (f" AS {quote(t[1])}" if t[1] else ""))
		elif t[0] == "count_col":
			ref = quote(physical(spec.name)) if (spec := schema.column(t[1])) else quote(t[1])
			projection.append(f"count(({ref} != NULL AND {ref} != NONE))" + (f" AS {quote(t[2])}" if t[2] else ""))
		else:
			name, alias = t[1], t[2]
			spec = schema.column(name)
			if spec is None:
				# MariaDB's verdict for an unknown column - SurrealDB would silently select NULL
				raise SurrealDBProgrammingError(1054, f"Unknown column '{name}' in 'field list'")
			projection.append(quote(physical(spec.name)) + (f" AS {quote(alias)}" if alias else ""))
	parts = ["SELECT " + ", ".join(projection), "FROM " + quote_table(table)]

	# WHERE: every leaf through the P1.6d fragment parser; one shared Params set -----------------------------------
	params = Params()
	if "where" in clauses:
		renderer = Renderer(params, schema_loader or table_schema)
		renderer._bind_table(Table(table))
		try:
			where_sql = _render_bool(clauses["where"].strip(), renderer)
		except SurrealDBProgrammingError:
			raise  # 1054/1052: MariaDB's own verdict, never replace it with a silently-NULLing query
		except Exception:
			return None
		if not where_sql:
			return None
		parts.append("WHERE " + where_sql)

	if aggregate:
		parts.append("GROUP ALL")  # count() without GROUP ALL returns one row per record (measured)
	elif distinct:
		group_cols = [quote(t[1]) for t in terms if t[0] == "col" and not t[2]]
		if len(group_cols) != len(terms) or "order_by" in clauses:
			return None
		parts.append("GROUP BY " + ", ".join(group_cols))  # SurrealQL has no DISTINCT (measured)

	# ORDER BY: drop the table qualifier of plain columns (SurrealQL's ORDER BY takes bare fields) ------------------
	if "order_by" in clauses:
		out_terms = []
		for term in _split_top_commas(clauses["order_by"]):
			t = term.strip()
			mo = _ORDER_TERM.match(t)
			if mo and schema.column(mo[1]) is not None:
				out_terms.append(quote(mo[1]) + (f" {mo[2]}" if mo[2] else ""))
			else:  # an alias or a function (rand()): the compat layer accepts these verbatim
				out_terms.append(t)
		parts.append("ORDER BY " + ", ".join(out_terms))

	if "limit" in clauses:
		ml = _LIMIT.match(clauses["limit"].strip())
		if not ml:
			return None
		if ml[2]:
			parts.append(f"LIMIT {ml[2]} START {ml[1]}")  # MariaDB's `LIMIT a, b` = b rows after a
		elif ml[3]:
			parts.append(f"LIMIT {ml[1]} START {ml[3]}")
		else:
			parts.append(f"LIMIT {ml[1]}")

	query = " ".join(parts)

	# column-order hints for positional readers, in the shape the translator emits for its own queries -------------
	if star and not aggregate and not distinct:
		query += _star_hint(schema)
	elif not aggregate and not distinct and not star and "order_by" not in clauses and len(terms) > 1:
		resolved = [t[1] for t in terms if t[0] == "col" and schema.column(t[1]) is not None and not t[2]]
		if len(resolved) == len(terms):
			kinds = [schema.column(n).kind for n in resolved]
			query += f" /*cols:{','.join(resolved)}*/ /*kinds:{','.join(kinds)}*/"
	return query, (params.values or None)


def _star_hint(schema) -> str:
	names = [spec.name for spec in schema.columns.values()]
	kinds = [spec.kind for spec in schema.columns.values()]
	return f" /*cols:{','.join(names)}*/ /*kinds:{','.join(kinds)}*/"


def _render_projection_term(t: str) -> tuple | None:
	if t == "*":
		return ("star",)
	mc = _PROJ_COUNT_STAR.match(t)
	if mc:
		return ("count", mc[1])
	mn = _PROJ_COUNT_COL.match(t)
	if mn:
		return ("count_col", mn[1], mn[2] or "count")
	mcol = _PROJ_COLUMN.match(t)
	if mcol:
		return ("col", mcol[1], mcol[2])
	return None

# --- the WHERE tree -----------------------------------------------------------------------------------------------
_BOOL_KW = re.compile(r"(?i)\b(and|or|between)\b")
_SQL_WORDS = frozenset(
	"""exists coalesce ifnull if concat cast date_format char_length substring locate group_concat
	convert binary match against sounds regexp rlike interval extract timestampdiff timestampadd
	distinct from select where is null like in between""".split()
)


class _Unsupported(Exception):
	"""The fragment parser met a MariaDB construct it has no meaning for (`exists`, `coalesce`,
	sub-queries): the statement passes through and the server keeps its own verdict."""


def _render_leaf(t: str, renderer: Renderer) -> str:
	try:
		return renderer.predicate(_FragmentParser(renderer, t).parse(), False)
	except SurrealDBProgrammingError as e:
		m = re.match(r"Unknown column '(\w+)'", e.args[1]) if len(e.args) > 1 else None
		if m and m[1].lower() in _SQL_WORDS:
			raise _Unsupported(t)
		raise


def _bool_ops(text: str) -> list[tuple[str, int, int]]:
	"""Top-level boolean operators of `text`, with a BETWEEN's own AND excluded from the splits."""
	masked = _mask(text)
	ops: list[tuple[str, int, int]] = []
	between_pending = False
	for m in re.finditer(_BOOL_KW, masked):
		w = m[1].lower()
		if w == "between":
			between_pending = True
		elif w == "or":
			between_pending = False
			ops.append(("or", m.start(), m.end()))
		elif between_pending:
			between_pending = False  # this AND closes a BETWEEN pair; it does not split
		else:
			ops.append(("and", m.start(), m.end()))
	return ops


def _render_bool(text: str, renderer: Renderer) -> str:
	"""Render a boolean tree: each leaf goes through the fragment parser and the ordinary
	predicate machinery (guards, shadows, negation push-down); the top level is string-composed."""
	ops = _bool_ops(text)
	if not ops:
		t = text.strip()
		while t.startswith("(") and _closes_outer_paren(t):  # redundant outer parens, one level at a time
			t = t[1:-1].strip()
		neg = _NOT.match(t)
		if neg:
			return f"NOT ({_render_bool(t[neg.end() :], renderer)})"
		return _render_leaf(t, renderer)
	op, pos, end = ops[-1]
	left = _render_bool(text[:pos].strip(), renderer)
	right = _render_bool(text[end:].strip(), renderer)
	joiner = "AND" if op == "and" else "OR"
	return f"({left} {joiner} {right})"


def _closes_outer_paren(t: str) -> bool:
	if not (t.startswith("(") and t.endswith(")")):
		return False
	depth = 0
	in_str = False
	i = 0
	while i < len(t):
		c = t[i]
		if in_str:
			if c == "\\":
				i += 2
				continue
			if c == "'":
				if t[i + 1 : i + 2] == "'":
					i += 2
					continue
				in_str = False
			i += 1
			continue
		if c == "'":
			in_str = True
		elif c == "(":
			depth += 1
		elif c == ")":
			depth -= 1
			if depth == 0 and i != len(t) - 1:
				return False
		i += 1
	return True
