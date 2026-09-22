"""P1.6c: MariaDB-vs-SurrealDB parity for functions, expressions, joins, sub-queries, non-strict GROUP BY and upserts.

Same method as `test_surrealdb_parity_live`: the same PyPika objects run on both engines over identical data (a parent table and
a child table linked by `parent`), and the results are compared row by row."""

import datetime as dt
import random
import unittest
from decimal import Decimal

from pypika import Case, Order, Table
from pypika import functions as fn
from pypika.terms import CustomFunction

import frappe
from frappe.database.schema import DbColumn
from frappe.database.surrealdb import schema as S
from frappe.database.surrealdb.errors import SurrealDBError
from frappe.database.surrealdb.translator import render
from frappe.query_builder import functions as qf
from frappe.query_builder.builder import MariaDB
from frappe.query_builder.surrealdb_builder import SurrealDB
from frappe.tests.surrealdb_live import LIVE, SKIP_REASON
from frappe.tests.test_surrealdb_parity_live import (
	COLS,
	T,
	TestSurrealDBParityLive,
	canon_rows,
	col,
	describe,
)

CharLength = CustomFunction("CHAR_LENGTH", ["x"])
KID_TABLE = "tabParityKid"
K = Table(KID_TABLE)
KID_COLS = [
	"name",
	"parent",
	"parentfield",
	"parenttype",
	"item",
	"qty",
	"rate",
	"ship_date",
	"ship_time",
	"created",
	"idx",
]

KID_DDL = f"""create table `{KID_TABLE}` (
    name varchar(140) primary key, creation datetime(6), modified datetime(6), modified_by varchar(140), owner varchar(140),
    docstatus tinyint(4) not null default 0, idx int(11) not null default 0, parent varchar(140), parentfield varchar(140),
    parenttype varchar(140), item varchar(140), qty decimal(21,9), rate decimal(21,9), ship_date date, ship_time time(6),
    created datetime(6), index parent(parent))
    ENGINE=InnoDB ROW_FORMAT=DYNAMIC CHARACTER SET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""

AUTH_DDL = """create table `__Auth` (doctype varchar(140) not null, name varchar(255) not null, fieldname varchar(140) not null,
    password text not null, encrypted tinyint(1) not null default 0, primary key (doctype, name, fieldname))
    ENGINE=InnoDB ROW_FORMAT=DYNAMIC CHARACTER SET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""

RATES = [
	"2.5",
	"0.125",
	"1.005",
	"-2.5",
	"-0.125",
	"3.5",
	"0.5",
	"1.5",
	"0.0000001",
	"10.999999999",
	"0",
	"100.25",
	"-7.75",
]
ITEMS = ["Widget", "widget", "GADGET", "Ünï", "ünï", "résumé", "Resume", "a", "A", "日本", None]


def surreal_kid_specs():
	return [
		col("Data", "parent", set_index=1), col("Data", "parentfield"), col("Data", "parenttype"), col("Data", "item"),
		col("Duration", "qty"), col("Duration", "rate"), col("Date", "ship_date"), col("Time", "ship_time"),
		col("Datetime", "created"),
	]  # fmt: skip


def make_kid_rows(rng: random.Random, parent_names: list[str]) -> list[dict]:
	rows = []
	for i in range(320):
		roll = rng.random()
		if roll < 0.82:
			parent = rng.choice(parent_names)
			parent = rng.choice([parent, parent.lower(), parent.upper()])
		elif roll < 0.92:
			parent = f"NOPE-{rng.randint(1, 9)}"
		else:
			parent = None
		stamp = dt.datetime(2023, 12, 30) + dt.timedelta(
			seconds=rng.randint(0, 40_000_000), microseconds=rng.choice([0, 1, 500000, 999999])
		)
		rows.append(
			{
				"name": f"K-{i:04d}",
				"creation": stamp,
				"modified": stamp,
				"modified_by": "Administrator",
				"owner": "Administrator",
				"idx": rng.randint(0, 5),
				"parent": parent,
				"parentfield": rng.choice(["items", "taxes", None]),
				"parenttype": rng.choice(["Doc", "DOC", "Other"]),
				"item": rng.choice(ITEMS),
				"qty": None
				if rng.random() < 0.15
				else Decimal(rng.randint(-10, 200)) / rng.choice([1, 4, 8]),
				"rate": None if rng.random() < 0.1 else Decimal(rng.choice(RATES)),
				"ship_date": None
				if rng.random() < 0.2
				else dt.date(2024, 1, 1) + dt.timedelta(days=rng.randint(0, 400)),
				"ship_time": None
				if rng.random() < 0.3
				else dt.timedelta(seconds=rng.randint(0, 200000), microseconds=rng.choice([0, 7])),
				"created": None if rng.random() < 0.2 else stamp,
			}
		)
	return rows


KID_INSERT = ["name", "creation", "modified", "modified_by", "owner", *[c for c in KID_COLS if c != "name"]]


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestSurrealDBParityP16c(TestSurrealDBParityLive):
	# do not repeat the P1.6a/b tests of the parent class
	test_where_parity = test_order_and_pagination_parity = test_write_parity = test_aggregate_parity = None
	test_constraint_error_parity = test_insert_ignore_skips_existing_names = test_loaded_data_is_identical = (
		None
	)

	def setUp(self):
		super().setUp()
		frappe.db.sql_ddl(f"drop table if exists `{KID_TABLE}`")
		frappe.db.sql_ddl(KID_DDL)
		self.addCleanup(lambda: frappe.db.sql_ddl(f"drop table if exists `{KID_TABLE}`"))
		for stmt in S.create_statements(KID_TABLE, surreal_kid_specs()):
			self.sdb.sql_ddl(stmt)
		S.clear_schema_cache()
		self.kids = make_kid_rows(random.Random(7), [r["name"] for r in self.rows])
		for row in self.kids:
			frappe.db.sql(
				f"insert into `{KID_TABLE}` ({', '.join(KID_INSERT)}) values ({', '.join(['%s'] * len(KID_INSERT))})",
				tuple(row[c] for c in KID_INSERT),
			)
		frappe.db.commit()
		query = SurrealDB.into(K).columns(*KID_INSERT)
		for row in self.kids:
			query = query.insert(*[row[c] for c in KID_INSERT])
		sql, params = render(query, None, self.loader)
		self.sdb.sql(sql, params.values)
		self.sdb.commit()

	# --- helpers ----------------------------------------------------------------------------------------------------------
	def select_terms(self, terms: dict, failures: list, where=None, ordered=True):
		for label, term in terms.items():

			def build(Q, term=term):
				q = Q.from_(T).select(T.name, term())
				if where is not None:
					q = q.where(where())
				return q.orderby(T.name)

			self.both(label, build, failures, ordered)

	def report(self, failures, what):
		self.assertEqual(failures, [], f"{len(failures)} {what} disagree:\n" + "\n".join(failures[:30]))

	# --- expressions and functions ------------------------------------------------------------------------------------------
	def test_functions_in_select(self):
		t = T
		terms = {
			"ifnull(title,'')": lambda: fn.IfNull(t.title, ""),
			"ifnull(note,title)": lambda: fn.IfNull(t.note, t.title),
			"coalesce(note,title,'z')": lambda: fn.Coalesce(t.note, t.title, "z"),
			"ifnull(amount,0)": lambda: fn.IfNull(t.amount, 0),
			"ifnull(qty,0)": lambda: fn.IfNull(t.qty, 0),
			"ifnull(posting_date,'0001-01-01')": lambda: fn.IfNull(t.posting_date, "0001-01-01"),
			"ifnull(stamp,'2000-01-01 00:00:00')": lambda: fn.IfNull(t.stamp, "2000-01-01 00:00:00"),
			"amount+1": lambda: t.amount + 1,
			"amount+1.5": lambda: t.amount + 1.5,
			"qty*amount": lambda: t.qty * t.amount,
			"qty-flag": lambda: t.qty - t.flag,
			"qty*flag": lambda: t.qty * t.flag,
			"amount/3": lambda: t.amount / 3,
			"qty/4": lambda: t.qty / 4,
			"qty/0": lambda: t.qty / 0,
			"qty/flag (zero divisor)": lambda: t.qty / t.flag,
			"(amount*qty)/7": lambda: (t.amount * t.qty) / 7,
			"amount*-1": lambda: t.amount * -1,
			"round(amount,2)": lambda: qf.Round(t.amount, 2),
			"round(amount)": lambda: qf.Round(t.amount),
			"round(amount*qty/8,1)": lambda: qf.Round(t.amount * t.qty / 8, 1),
			"round(qty)": lambda: qf.Round(t.qty),
			"truncate(amount,1)": lambda: qf.Truncate(t.amount, 1),
			"abs(amount)": lambda: fn.Abs(t.amount),
			"abs(qty)": lambda: fn.Abs(t.qty),
			"ceil(amount)": lambda: CustomFunction("CEIL", ["x"])(t.amount),
			"floor(amount)": lambda: fn.Floor(t.amount),
			"concat(title,' x')": lambda: fn.Concat(t.title, " x"),
			"concat(note,'-',title)": lambda: fn.Concat(t.note, "-", t.title),
			"concat(qty,'a')": lambda: fn.Concat(t.qty, "a"),
			"concat(posting_date,'a')": lambda: fn.Concat(t.posting_date, "a"),
			"case qty": lambda: Case().when(t.qty > 5, "hi").else_("lo"),
			"case no else": lambda: Case().when(t.title.like("a%"), 1).when(t.qty < 0, 2),
			"case amount": lambda: Case().when(t.note.isnull(), t.amount).else_(0),
			"date(stamp)": lambda: fn.Date(t.stamp),
			"timestamp(posting_date,at)": lambda: qf.Timestamp(t.posting_date, t.at),
			"timestamp(posting_date)": lambda: qf.Timestamp(t.posting_date),
			"unix_timestamp(stamp)": lambda: qf.UnixTimestamp(t.stamp),
			"unix_timestamp(posting_date)": lambda: qf.UnixTimestamp(t.posting_date),
			"year(posting_date)": lambda: fn.Extract("year", t.posting_date),
			"date_format(stamp,'%Y-%m')": lambda: qf.DateFormat(t.stamp, "%Y-%m"),
			"date_format(posting_date,'%d/%m/%y %b %W')": lambda: qf.DateFormat(
				t.posting_date, "%d/%m/%y %b %W"
			),
			"date_format(stamp,'%H:%i:%s %M %j')": lambda: qf.DateFormat(t.stamp, "%H:%i:%s %M %j"),
			"char_length(title)": lambda: CharLength(t.title),
			"substring(title,2,3)": lambda: fn.Substring(t.title, 2, 3),
			"nullif(title,'apple')": lambda: fn.NullIf(t.title, "apple"),
			"nullif(qty,3)": lambda: fn.NullIf(t.qty, 3),
		}
		failures = []
		self.select_terms(terms, failures)
		self.report(failures, "select expressions")

	def test_functions_in_where_and_order(self):
		t = T
		where = {
			"ifnull(title,'')=''": lambda: fn.IfNull(t.title, "") == "",
			"ifnull(note,'')!=''": lambda: fn.IfNull(t.note, "") != "",
			"ifnull(note,'')='a'": lambda: fn.IfNull(t.note, "") == "A",
			"ifnull(amount,0)>10": lambda: fn.IfNull(t.amount, 0) > 10,
			"ifnull(amount,'')=''": lambda: fn.IfNull(t.amount, "") == "",
			"ifnull(amount,'')!=''": lambda: fn.IfNull(t.amount, "") != "",
			"ifnull(posting_date,'')=''": lambda: fn.IfNull(t.posting_date, "") == "",
			"ifnull(qty,'')!=''": lambda: fn.IfNull(t.qty, "") != "",
			"ifnull(note,title)='apple'": lambda: fn.IfNull(t.note, t.title) == "apple",
			"ifnull(note,title) like": lambda: fn.IfNull(t.note, t.title).like("a%"),
			"coalesce in": lambda: fn.Coalesce(t.note, t.title, "z").isin(["resume", "Z", "APPLE"]),
			"qty+flag>5": lambda: t.qty + t.flag > 5,
			"amount*2<100": lambda: t.amount * 2 < 100,
			"amount/3 > 1": lambda: t.amount / 3 > 1,
			"round(amount,1)=0.1": lambda: qf.Round(t.amount, 1) == 0.1,
			"round(amount,0)>5": lambda: qf.Round(t.amount) > 5,
			"timestamp>=": lambda: qf.Timestamp(t.posting_date, t.at) >= "2024-01-05 01:00:00",
			"timestamp between": lambda: qf.Timestamp(t.posting_date, t.at).between(
				"2024-01-01", "2024-02-01 12:00:00"
			),
			"date(stamp)=": lambda: fn.Date(t.stamp) == "2024-01-05",
			"date(stamp)>=date col": lambda: fn.Date(t.stamp) >= t.posting_date,
			"unix_timestamp>": lambda: qf.UnixTimestamp(t.stamp) > 1704448800,
			"case = ": lambda: Case().when(t.qty > 5, "hi").else_("lo") == "HI",
			"not (case)": lambda: ~(Case().when(t.qty > 5, "hi").else_("lo") == "hi"),
			"nullif is null": lambda: fn.NullIf(t.title, "apple").isnull(),
			"char_length>3": lambda: CharLength(t.title) > 3,
			"year=": lambda: fn.Extract("year", t.posting_date) == 2024,
		}
		failures = []
		for label, w in where.items():
			self.both(f"WHERE {label}", self.select_all(w), failures)
		order = {
			"ifnull(note,'')": lambda q: q.orderby(fn.IfNull(T.note, "")).orderby(T.name),
			"qty*flag desc": lambda q: q.orderby(T.qty * T.flag, order=Order.desc).orderby(T.name),
			"timestamp": lambda q: q.orderby(qf.Timestamp(T.posting_date, T.at)).orderby(T.name),
			"case": lambda q: q.orderby(Case().when(T.qty > 5, "b").else_("a")).orderby(T.name),
			"amount/3 desc": lambda q: q.orderby(T.amount / 3, order=Order.desc).orderby(T.name),
			"coalesce limit": lambda q: q.orderby(fn.Coalesce(T.note, T.title, ""))
			.orderby(T.name)
			.limit(9)
			.offset(3),
		}
		for label, o in order.items():
			self.both(
				f"ORDER {label}",
				lambda Q, o=o: o(Q.from_(T).select(*[T[c] for c in COLS])),
				failures,
			)
		self.report(failures, "expression predicates/orderings")

	def test_now_is_statement_time(self):
		def build(Q):
			return Q.from_(T).select(fn.Count("*")).where(fn.Now() > "2000-01-01")

		self.assertEqual(self.run_maria(build), self.run_surreal(build))
		sql, params = render(SurrealDB.from_(T).select(T.name, fn.Now()).limit(1), None, self.loader)
		((_, now),) = self.sdb.sql(sql, params.values)
		utc = dt.datetime.now(dt.UTC).replace(tzinfo=None)
		self.assertLess(abs((utc - now).total_seconds()), 5)

	def test_aggregates_over_expressions(self):
		def by_key(rows):
			return sorted(rows, key=repr)

		t = T
		cases = {
			"sum(qty*flag)": lambda Q: Q.from_(t).select(fn.Sum(t.qty * t.flag)),
			"sum(ifnull(amount,0))": lambda Q: Q.from_(t).select(fn.Sum(fn.IfNull(t.amount, 0))),
			"avg(qty)": lambda Q: Q.from_(t).select(fn.Avg(t.qty)),
			"avg(amount)": lambda Q: Q.from_(t).select(fn.Avg(t.amount)),
			"avg empty": lambda Q: Q.from_(t).select(fn.Avg(t.amount)).where(t.title == "no such value"),
			"count distinct title": lambda Q: Q.from_(t).select(fn.Count(t.title).distinct()),
			"count distinct posting_date": lambda Q: Q.from_(t).select(fn.Count(t.posting_date).distinct()),
			"count distinct qty": lambda Q: Q.from_(t).select(fn.Count(t.qty).distinct()),
			"sum/count": lambda Q: Q.from_(t).select(fn.Sum(t.qty) / fn.Count("*")),
			"sum+1": lambda Q: Q.from_(t).select(fn.Sum(t.qty) + 1),
			"group by year": lambda Q: Q.from_(t)
			.select(fn.Extract("year", t.posting_date), fn.Count("*"), fn.Sum(t.qty))
			.groupby(fn.Extract("year", t.posting_date))
			.orderby(fn.Extract("year", t.posting_date)),
			"group by ifnull(note,'')": lambda Q: Q.from_(t)
			.select(fn.IfNull(t.note, ""), fn.Count("*"))
			.groupby(fn.IfNull(t.note, "")),
			"group by date(stamp)": lambda Q: Q.from_(t)
			.select(fn.Date(t.stamp), fn.Count("*"))
			.groupby(fn.Date(t.stamp))
			.orderby(fn.Date(t.stamp)),
			"group by case": lambda Q: Q.from_(t)
			.select(Case().when(t.qty > 5, "hi").else_("lo"), fn.Count("*"), fn.Max(t.qty))
			.groupby(Case().when(t.qty > 5, "hi").else_("lo"))
			.orderby(Case().when(t.qty > 5, "hi").else_("lo")),
			"non-strict: flag + first title": lambda Q: Q.from_(t)
			.select(t.flag, fn.Count("*"))
			.groupby(t.flag)
			.orderby(t.flag),
			"select expr of key": lambda Q: Q.from_(t)
			.select(t.qty + 1, fn.Count("*"))
			.groupby(t.qty)
			.orderby(t.qty),
			"having sum": lambda Q: Q.from_(t)
			.select(t.flag, fn.Sum(t.qty * 2))
			.groupby(t.flag)
			.having(fn.Sum(t.qty * 2) > 10)
			.orderby(t.flag),
			"having count vs col": lambda Q: Q.from_(t)
			.select(t.flag, fn.Count("*"))
			.groupby(t.flag)
			.having(fn.Count("*") > 3)
			.orderby(t.flag),
			"order by expr agg": lambda Q: Q.from_(t)
			.select(t.qty, fn.Count("*"))
			.groupby(t.qty)
			.orderby(fn.Count("*") * -1)
			.orderby(t.qty),
		}
		failures = []
		for label, build in cases.items():
			self.both(label, build, failures)
		self.report(failures, "aggregate-over-expression cases")

		# GROUP_CONCAT: element order is unspecified in MariaDB, so compare the sorted elements per group
		from frappe.query_builder.custom import GROUP_CONCAT

		def build(Q):
			return Q.from_(t).select(t.flag, GROUP_CONCAT(t.qty)).groupby(t.flag).orderby(t.flag)

		def normal(rows):
			return [(r[0], None if r[1] is None else sorted(r[1].split(","))) for r in rows]

		self.assertEqual(normal(self.run_maria(build)), normal(self.run_surreal(build)))

	def test_non_strict_group_by_shows_a_member(self):
		"""A column that is not grouped shows some row of the group (which one is unspecified in MariaDB)."""

		def build(Q):
			return Q.from_(T).select(T.flag, T.qty, fn.Count("*")).groupby(T.flag)

		got = self.run_surreal(build)
		members = {}
		for row in self.run_maria(lambda Q: Q.from_(T).select(T.flag, T.qty)):
			members.setdefault(row[0], set()).add(row[1])
		self.assertEqual({r[0] for r in got}, set(members))
		for flag, qty, _n in got:
			self.assertIn(qty, members[flag])
		self.assertEqual(
			sorted((r[0], r[2]) for r in got), sorted((r[0], r[2]) for r in self.run_maria(build))
		)

	def test_rounding_and_division_on_ties(self):
		"""Half-way values (0.125, 1.005, 2.5, -2.5 ...) are where SurrealDB (half to even) and MariaDB (half away from zero) differ."""
		k = K
		terms = {
			"round(rate)": lambda: qf.Round(k.rate),
			"round(rate,1)": lambda: qf.Round(k.rate, 1),
			"round(rate,2)": lambda: qf.Round(k.rate, 2),
			"round(rate,3)": lambda: qf.Round(k.rate, 3),
			"round(rate*qty,1)": lambda: qf.Round(k.rate * k.qty, 1),
			"truncate(rate,1)": lambda: qf.Truncate(k.rate, 1),
			"truncate(rate,2)": lambda: qf.Truncate(k.rate, 2),
			"rate/8": lambda: k.rate / 8,
			"rate/16": lambda: k.rate / 16,
			"qty/rate": lambda: k.qty / k.rate,
			"idx/8 (int division is decimal)": lambda: k.idx / 8,
			"idx/3": lambda: k.idx / 3,
			"rate*qty": lambda: k.rate * k.qty,
			"ceil/floor": lambda: fn.Floor(k.rate) + CustomFunction("CEIL", ["x"])(k.rate),
			"abs": lambda: fn.Abs(k.rate),
		}
		failures = []
		for label, term in terms.items():
			self.both(label, lambda Q, term=term: Q.from_(k).select(k.name, term()).orderby(k.name), failures)
		self.both(
			"sum(round(rate,2))",
			lambda Q: Q.from_(k).select(fn.Sum(qf.Round(k.rate, 2)), fn.Avg(k.rate), fn.Avg(k.idx)),
			failures,
		)
		self.report(failures, "rounding/division cases")

	def test_kid_functions_and_null_handling(self):
		k = K
		terms = {
			"ifnull(item,'')": lambda: fn.IfNull(k.item, ""),
			"ifnull(qty,0)*rate": lambda: fn.IfNull(k.qty, 0) * k.rate,
			"concat(parent,'/',item)": lambda: fn.Concat(k.parent, "/", k.item),
			"timestamp(ship_date,ship_time)": lambda: qf.Timestamp(k.ship_date, k.ship_time),
			"date(created)": lambda: fn.Date(k.created),
			"unix_timestamp(created)": lambda: qf.UnixTimestamp(k.created),
			"date_format(ship_date,'%M %e, %Y')": lambda: qf.DateFormat(k.ship_date, "%M %e, %Y"),
			"case item": lambda: Case().when(k.item == "widget", 1).when(k.item.like("g%"), 2).else_(0),
			"nullif(item,'a')": lambda: fn.NullIf(k.item, "a"),
		}
		failures = []
		for label, term in terms.items():
			self.both(label, lambda Q, term=term: Q.from_(k).select(k.name, term()).orderby(k.name), failures)
		self.report(failures, "child-table function cases")

	def test_empty_string_compares_as_zero_of_the_column_type(self):
		"""Frappe's `is set` on a Date/Int field is `col <> ''`; MariaDB reads '' as 0000-00-00 / 0 / 00:00:00."""

		def insert(Q):
			return (
				Q.into(T)
				.columns("name", "posting_date", "stamp", "at", "qty", "amount", "flag")
				.insert("ZERO-1", "0000-00-00", "0000-00-00 00:00:00", "00:00:00", 0, 0, 0)
				.insert("ZERO-2", "2024-01-01", "2024-01-01 00:00:00", "00:00:01", 3, 1, 1)
			)

		insert(MariaDB).run()
		frappe.db.commit()
		sql, params = render(insert(SurrealDB), None, self.loader)
		self.sdb.sql(sql, params.values)
		self.sdb.commit()
		failures = []
		for col_ in ("posting_date", "stamp", "at", "qty", "amount", "flag"):
			numeric = col_ in ("qty", "amount", "flag")
			for label, crit in {
				"= ''": lambda c=col_: T[c] == "",
				"!= ''": lambda c=col_: T[c] != "",
				"NOT (= '')": lambda c=col_: ~(T[c] == ""),
				"IN ('', x)": lambda c=col_, n=numeric: T[c].isin(["", "1"] if n else [""]),
			}.items():
				self.both(f"{col_} {label}", self.select_all(crit), failures)
		self.report(failures, "empty-string comparisons")

	def test_long_text_is_set_is_exact_without_a_shadow(self):
		"""`long_text = ''` / `<> ''` (Frappe's "is not set" / "is set") needs no collation shadow: a string equals '' under
		utf8mb4_unicode_ci PAD SPACE when every character weighs nothing or only the space weight."""

		def insert(Q):
			q = Q.into(T).columns("name", "notes")
			for i, v in enumerate(
				["", "   ", "\t", "\u200b", " a ", "\u00a0", "\u3000 ", "\x01", "x", "\u200b \u200b"]
			):
				q = q.insert(f"EMP-{i}", v)
			return q

		insert(MariaDB).run()
		frappe.db.commit()
		sql, params = render(insert(SurrealDB), None, self.loader)
		self.sdb.sql(sql, params.values)
		self.sdb.commit()
		failures = []
		for label, crit in {
			"notes = ''": lambda: T.notes == "",
			"notes != ''": lambda: T.notes != "",
			"NOT (notes = '')": lambda: ~(T.notes == ""),
			"notes = '  ' (equal to '')": lambda: T.notes == "  ",
			"ifnull(notes,'') = ''": lambda: fn.IfNull(T.notes, "") == "",
			"ifnull(notes,'') != ''": lambda: fn.IfNull(T.notes, "") != "",
			"and with title": lambda: (T.notes != "") & (T.qty > 3),
		}.items():
			self.both(label, self.select_all(crit), failures)
		self.both(
			"select ifnull(notes,'')",
			lambda Q: Q.from_(T).select(T.name, fn.IfNull(T.notes, "")).orderby(T.name),
			failures,
		)
		self.report(failures, "long-text emptiness")

	def test_date_column_against_datetime_literals(self):
		"""MariaDB compares a DATE with a datetime literal as a DATETIME (the date at midnight)."""
		failures = []
		for label, crit in {
			"= midnight": lambda: T.posting_date == "2024-01-05 00:00:00",
			"= 10:00": lambda: T.posting_date == "2024-01-05 10:00:00",
			"< 10:00": lambda: T.posting_date < "2024-01-05 10:00:00",
			"<= midnight": lambda: T.posting_date <= "2024-01-05 00:00:00",
			"> 10:00": lambda: T.posting_date > "2024-01-05 10:00:00",
			">= 10:00": lambda: T.posting_date >= "2024-01-05 10:00:00.5",
			"!= 10:00": lambda: T.posting_date != "2024-01-05 10:00:00",
			"between": lambda: T.posting_date.between("2024-01-01 00:00:00", "2024-02-29 12:00:00"),
			"not between": lambda: ~T.posting_date.between("2024-01-01 00:00:00", "2024-02-29 12:00:00"),
			"in": lambda: T.posting_date.isin(["2024-01-05 00:00:00", "2023-12-31 00:00:00"]),
			"datetime object": lambda: T.posting_date >= dt.datetime(2024, 1, 5, 0, 0, 1),
		}.items():
			self.both(f"posting_date {label}", self.select_all(crit), failures)
		self.report(failures, "date-vs-datetime comparisons")

	def test_paged_left_join_pages_the_first_table_before_joining(self):
		"""LEFT JOIN + ORDER BY/LIMIT on the first table alone: only the page's parents are joined (ids first, then records)."""
		p, k = T, K

		def base(Q):
			return (
				Q.from_(p)
				.left_join(k)
				.on(k.parent == p.name)
				.select(p.name, k.name, k.qty)
				.where(p.flag == 1)
			)

		failures = []
		for label, order in {
			"name": lambda q: q.orderby(p.name),
			"title, name": lambda q: q.orderby(p.title).orderby(p.name),
			"qty desc, name": lambda q: q.orderby(p.qty, order=Order.desc).orderby(p.name),
			"ifnull(note,'') , name": lambda q: q.orderby(fn.IfNull(p.note, "")).orderby(p.name),
		}.items():
			full = self.run_maria(lambda Q, order=order: order(base(Q)))
			counts, last = [], object()
			for row in full:
				if row[0] != last:
					counts.append(0)
					last = row[0]
				counts[-1] += 1
			for first, n in [(0, 5), (3, 7), (10, 4), (0, 1)]:
				offset, limit = sum(counts[:first]), sum(counts[first : first + n])
				build = lambda Q, order=order, o=offset, li=limit: order(base(Q)).limit(li).offset(o)  # noqa: E731
				self.both(
					f"{label}: parents {first}..{first + n} (rows {offset}+{limit})",
					build,
					failures,
					ordered=False,
				)
		self.report(failures, "paged left joins")

		# a window that ends inside a parent: the right number of rows, all of them real rows, and the page pushdown is in the SQL
		full = self.run_maria(lambda Q: base(Q).orderby(p.name))
		build = lambda Q: base(Q).orderby(p.name).limit(7).offset(2)  # noqa: E731
		got = self.run_surreal(build)
		self.assertEqual(len(got), 7)
		self.assertTrue({repr(r) for r in got} <= {repr(r) for r in full})
		sql, _ = render(build(SurrealDB), None, self.loader)
		self.assertIn("SELECT VALUE id FROM (SELECT id, ", sql)
		self.assertIn("LIMIT 9)", sql)  # offset + limit parents at most
		# no pushdown when the ordering uses the joined table, a WHERE touches it, or the join is an INNER JOIN
		for other in (
			lambda Q: base(Q).orderby(k.name).limit(7),
			lambda Q: base(Q).where(k.qty > 5).orderby(p.name).limit(7),
			lambda Q: Q.from_(p)
			.inner_join(k)
			.on(k.parent == p.name)
			.select(p.name, k.name)
			.orderby(p.name)
			.limit(7),
		):
			sql, _ = render(other(SurrealDB), None, self.loader)
			self.assertNotIn("SELECT VALUE id FROM (SELECT id, ", sql)

	# --- joins ------------------------------------------------------------------------------------------------------------
	def test_joins(self):
		p, k = T, K
		k2 = Table(KID_TABLE).as_("k2")
		cases = {
			"left join names": lambda Q: Q.from_(p)
			.left_join(k)
			.on(k.parent == p.name)
			.select(p.name, k.name, k.qty)
			.orderby(p.name)
			.orderby(k.name),
			"inner join": lambda Q: Q.from_(p)
			.inner_join(k)
			.on(k.parent == p.name)
			.select(p.name, k.name, k.rate)
			.orderby(k.name),
			"join star": lambda Q: Q.from_(p)
			.left_join(k)
			.on(k.parent == p.name)
			.select(p.star, k.star)
			.orderby(p.name)
			.orderby(k.name),
			"join extra on": lambda Q: Q.from_(p)
			.left_join(k)
			.on((k.parent == p.name) & (k.parenttype == "doc") & (k.qty > 5))
			.select(p.name, k.name, k.qty)
			.orderby(p.name)
			.orderby(k.name),
			"where both sides": lambda Q: Q.from_(p)
			.left_join(k)
			.on(k.parent == p.name)
			.select(p.name, k.item, p.title)
			.where((p.qty > 3) & (k.item == "WIDGET"))
			.orderby(p.name)
			.orderby(k.name),
			"where on joined is null": lambda Q: Q.from_(p)
			.left_join(k)
			.on(k.parent == p.name)
			.select(p.name)
			.where(k.name.isnull())
			.orderby(p.name),
			"where or across": lambda Q: Q.from_(p)
			.left_join(k)
			.on(k.parent == p.name)
			.select(p.name, k.name)
			.where((p.flag == 1) | (k.qty > 100))
			.orderby(p.name)
			.orderby(k.name),
			"order by joined": lambda Q: Q.from_(p)
			.inner_join(k)
			.on(k.parent == p.name)
			.select(p.name, k.name)
			.orderby(k.item)
			.orderby(k.name)
			.limit(25)
			.offset(5),
			"ifnull over joined": lambda Q: Q.from_(p)
			.left_join(k)
			.on(k.parent == p.name)
			.select(p.name, fn.IfNull(k.qty, 0) * p.qty)
			.orderby(p.name)
			.orderby(k.name),
			"group by joined": lambda Q: Q.from_(p)
			.left_join(k)
			.on(k.parent == p.name)
			.select(p.name, fn.Count(k.name), fn.Sum(k.qty * k.rate))
			.groupby(p.name)
			.orderby(p.name),
			"group by item over join": lambda Q: Q.from_(p)
			.inner_join(k)
			.on(k.parent == p.name)
			.select(k.item, fn.Count("*"), fn.Max(p.qty))
			.groupby(k.item),
			"having over join": lambda Q: Q.from_(p)
			.inner_join(k)
			.on(k.parent == p.name)
			.select(p.name, fn.Sum(k.qty))
			.groupby(p.name)
			.having(fn.Sum(k.qty) > 50)
			.orderby(p.name),
			"count distinct over join": lambda Q: Q.from_(p)
			.inner_join(k)
			.on(k.parent == p.name)
			.select(fn.Count(p.name).distinct()),
			"three tables (self join by alias)": lambda Q: Q.from_(p)
			.left_join(k)
			.on(k.parent == p.name)
			.left_join(k2)
			.on((k2.parent == p.name) & (k2.idx == k.idx) & (k2.name != k.name))
			.select(p.name, k.name, k2.name)
			.orderby(p.name)
			.orderby(k.name)
			.orderby(k2.name),
			"join column compare": lambda Q: Q.from_(p)
			.inner_join(k)
			.on(k.parent == p.name)
			.select(p.name, k.name)
			.where(k.qty > p.qty)
			.orderby(k.name),
			"join without an equality (nested loop)": lambda Q: Q.from_(p)
			.inner_join(k)
			.on(k.qty > p.qty)
			.select(p.name, k.name)
			.where(p.qty > 15)
			.orderby(p.name)
			.orderby(k.name),
			"join on integer equality": lambda Q: Q.from_(p)
			.inner_join(k)
			.on(k.idx == p.flag)
			.select(p.name, k.name)
			.where(p.qty > 17)
			.orderby(p.name)
			.orderby(k.name),
			"join on equality plus range": lambda Q: Q.from_(p)
			.left_join(k)
			.on((k.parent == p.name) & (k.ship_date > "2024-06-01") & (k.item != "widget"))
			.select(p.name, k.name, k.ship_date)
			.orderby(p.name)
			.orderby(k.name),
			"join on NULL keys": lambda Q: Q.from_(k)
			.left_join(p)
			.on(p.name == k.parent)
			.select(k.name, p.title)
			.orderby(k.name),
			"join date functions": lambda Q: Q.from_(p)
			.inner_join(k)
			.on(k.parent == p.name)
			.select(k.name, qf.Timestamp(k.ship_date, k.ship_time))
			.orderby(k.name),
		}
		failures = []
		for label, build in cases.items():
			self.both(label, build, failures)
		self.report(failures, "join cases")

	# --- sub-queries --------------------------------------------------------------------------------------------------------
	def test_subqueries(self):
		p, k = T, K
		sub_names = lambda Q: Q.from_(k).select(k.parent).where(k.qty > 50)  # noqa: E731 - has NULL parents
		sub_nonnull = lambda Q: Q.from_(k).select(k.parent).where((k.qty > 50) & k.parent.notnull())  # noqa: E731
		cases = {
			"name in (sub)": lambda Q: Q.from_(p)
			.select(p.name)
			.where(p.name.isin(sub_names(Q)))
			.orderby(p.name),
			"name not in (sub, with NULL)": lambda Q: Q.from_(p)
			.select(p.name)
			.where(p.name.notin(sub_names(Q)))
			.orderby(p.name),
			"name not in (sub, no NULL)": lambda Q: Q.from_(p)
			.select(p.name)
			.where(p.name.notin(sub_nonnull(Q)))
			.orderby(p.name),
			"not name in": lambda Q: Q.from_(p)
			.select(p.name)
			.where(~p.name.isin(sub_nonnull(Q)))
			.orderby(p.name),
			"qty in (sub numbers)": lambda Q: Q.from_(p)
			.select(p.name)
			.where(p.qty.isin(Q.from_(k).select(k.idx)))
			.orderby(p.name),
			"qty not in (empty sub)": lambda Q: Q.from_(p)
			.select(p.name)
			.where(p.qty.notin(Q.from_(k).select(k.idx).where(k.idx > 99)))
			.orderby(p.name),
			"title in (sub titles)": lambda Q: Q.from_(p)
			.select(p.name)
			.where(p.title.isin(Q.from_(k).select(k.item)))
			.orderby(p.name),
			"sub with group by": lambda Q: Q.from_(p)
			.select(p.name)
			.where(p.name.isin(Q.from_(k).select(k.parent).groupby(k.parent).having(fn.Count("*") > 2)))
			.orderby(p.name),
			"sub with join": lambda Q: Q.from_(p)
			.select(p.name)
			.where(
				p.name.isin(
					Q.from_(k)
					.inner_join(Table("tabParityZz").as_("z"))
					.on(k.parent == Table("tabParityZz").as_("z").name)
					.select(k.parent)
				)
			)
			.orderby(p.name),
			"nested sub": lambda Q: Q.from_(p)
			.select(p.name)
			.where(p.name.isin(Q.from_(k).select(k.parent).where(k.item.isin(Q.from_(p).select(p.title)))))
			.orderby(p.name),
			"scalar sub compare": lambda Q: Q.from_(p)
			.select(p.name)
			.where(p.qty > Q.from_(k).select(fn.Max(k.idx)))
			.orderby(p.name),
			"scalar sub select": lambda Q: Q.from_(p)
			.select(p.name, Q.from_(k).select(fn.Count("*")))
			.orderby(p.name)
			.limit(5),
			"in (sub) and in list": lambda Q: Q.from_(p)
			.select(p.name)
			.where(p.name.isin(sub_nonnull(Q)) & p.qty.isin([1, 2, 3, 4, 5, 6]))
			.orderby(p.name),
			"in (sub) on join": lambda Q: Q.from_(p)
			.left_join(k)
			.on(k.parent == p.name)
			.select(p.name, k.name)
			.where(k.parent.isin(sub_nonnull(Q)))
			.orderby(k.name),
		}
		failures = []
		for label, build in cases.items():
			self.both(label, build, failures)
		self.report(failures, "sub-query cases")

	def test_exists(self):
		from pypika.terms import ExistsCriterion as Exists

		p, k = T, K
		cases = {
			"exists": lambda Q: Q.from_(p)
			.select(p.name)
			.where(Exists(Q.from_(k).select(1).where(k.qty > 190)))
			.orderby(p.name),
			"not exists": lambda Q: Q.from_(p)
			.select(p.name)
			.where(Exists(Q.from_(k).select(1).where(k.qty > 190)).negate())
			.orderby(p.name),
			"exists empty": lambda Q: Q.from_(p)
			.select(p.name)
			.where(Exists(Q.from_(k).select(1).where(k.qty > 9999)))
			.orderby(p.name),
			"not(exists empty)": lambda Q: Q.from_(p)
			.select(p.name)
			.where(~Exists(Q.from_(k).select(1).where(k.qty > 9999)))
			.orderby(p.name),
		}
		failures = []
		for label, build in cases.items():
			self.both(label, build, failures)
		self.report(failures, "EXISTS cases")

	def test_correlated_subquery_fails_closed(self):
		from frappe.database.surrealdb.errors import SurrealDBNotImplementedError

		p, k = T, K
		# correlated IN needs a per-row truth value, not a per-row value: still fail closed (P1.6c)
		query = (
			SurrealDB.from_(p)
			.select(p.name)
			.where(p.name.isin(SurrealDB.from_(k).select(k.parent).where(k.parent == p.name)))
		)
		with self.assertRaises(SurrealDBNotImplementedError):
			render(query, None, self.loader)

	def test_correlated_scalar_subquery(self):
		# P1.6c: a scalar sub-query that reads the outer row is evaluated once per row (the `issingle` shape of
		# get_link_fields). The kid rows carry case-variant, misspelled and NULL parents, so this also checks the
		# collation-shadow comparison and the NULL/empty semantics against MariaDB.
		p, k = T, K
		cases = {
			"kids per parent": lambda Q: Q.from_(p)
			.select(p.name, Q.from_(k).select(fn.Count("*")).where(k.parent == p.name).as_("nk"))
			.orderby(p.name),
			"max kid idx per parent": lambda Q: Q.from_(p)
			.select(p.name, Q.from_(k).select(fn.Max(k.idx)).where(k.parent == p.name).as_("mx"))
			.orderby(p.name),
			"max kid qty per parent": lambda Q: Q.from_(p)
			.select(p.name, Q.from_(k).select(fn.Max(k.qty)).where(k.parent == p.name).as_("mq"))
			.orderby(p.name),
			"correlated plus uncorrelated conjunct": lambda Q: Q.from_(p)
			.select(
				p.name,
				Q.from_(k)
				.select(fn.Count("*"))
				.where((k.parent == p.name) & (k.idx > 2))
				.as_("nk"),
			)
			.orderby(p.name),
			"correlated scalar in where": lambda Q: Q.from_(p)
			.select(p.name)
			.where(p.qty > Q.from_(k).select(fn.Max(k.idx)).where(k.parent == p.name))
			.orderby(p.name),
			"correlated scalar on another column": lambda Q: Q.from_(p)
			.select(p.name, Q.from_(k).select(fn.Max(k.idx)).where(k.item == p.title).as_("mi"))
			.orderby(p.name),
		}
		failures = []
		for label, build in cases.items():
			self.both(label, build, failures)
		self.report(failures, "correlated scalar sub-query cases")

	# --- upserts on a system table ---------------------------------------------------------------------------------------------
	def test_upsert_system_table(self):
		from pypika.terms import Values

		from frappe.query_builder.terms import ParameterizedValueWrapper

		frappe.db.sql_ddl("drop table if exists `__Auth`")
		frappe.db.sql_ddl(AUTH_DDL)
		self.addCleanup(lambda: frappe.db.sql_ddl("drop table if exists `__Auth`"))
		for stmt in S.system_table_statements("__Auth"):
			self.sdb.sql_ddl(stmt)
		S.clear_schema_cache()
		A = Table("__Auth")
		steps = [
			lambda Q: Q.into(A)
			.columns("doctype", "name", "fieldname", "password", "encrypted")
			.insert("User", "a@x.com", "password", "h1", 0)
			.on_duplicate_key_update(A.password, "h1")
			.on_duplicate_key_update(A.encrypted, 0),
			lambda Q: Q.into(A)
			.columns("doctype", "name", "fieldname", "password", "encrypted")
			.insert("User", "a@x.com", "api_secret", "s1", 1)
			.on_duplicate_key_update(A.password, "s1")
			.on_duplicate_key_update(A.encrypted, 1),
			lambda Q: Q.into(A)
			.columns("doctype", "name", "fieldname", "password", "encrypted")
			.insert("USER", "A@X.COM", "PASSWORD", "h2", 0)
			.on_duplicate_key_update(A.password, "h2")
			.on_duplicate_key_update(A.encrypted, 0),
			lambda Q: Q.into(A)
			.columns("doctype", "name", "fieldname", "password", "encrypted")
			.insert("User", "b@x.com", "password", "h3", 1)
			.on_duplicate_key_update(A.password, Values(A.password)),
			lambda Q: Q.into(A)
			.columns("doctype", "name", "fieldname", "password", "encrypted")
			.insert("user", "B@x.com", "Password", "h4", 0)
			.on_duplicate_key_update(A.password, Values(A.password))
			.on_duplicate_key_update(A.encrypted, Values(A.encrypted)),
		]
		failures = []
		for i, build in enumerate(steps):
			try:
				build(MariaDB).run()
				frappe.db.commit()
			except Exception as e:
				failures.append(f"step {i}: MariaDB failed: {e}")
				continue
			try:
				sql, params = render(build(SurrealDB), None, self.loader)
				self.sdb.sql(sql, params.values)
				self.sdb.commit()
			except Exception as e:
				failures.append(f"step {i}: SurrealDB raised {type(e).__name__}: {str(e)[:200]}")
				self.sdb.rollback()
				continue
			self.both(
				f"after step {i}",
				lambda Q: Q.from_(A)
				.select(A.doctype, A.name, A.fieldname, A.password, A.encrypted)
				.orderby(A.name)
				.orderby(A.fieldname),
				failures,
			)
		self.report(failures, "upsert steps")

	# --- writes with expressions -------------------------------------------------------------------------------------------------
	def test_writes_with_expressions(self):
		steps = [
			("qty = qty*2 + flag", lambda Q: Q.update(T).set(T.qty, T.qty * 2 + T.flag).where(T.flag == 1)),
			(
				"amount = amount/3 (rounds to scale)",
				lambda Q: Q.update(T).set(T.amount, T.amount / 3).where(T.amount.notnull()),
			),
			(
				"qty = round(amount)",
				lambda Q: Q.update(T).set(T.qty, qf.Round(T.amount)).where(T.amount < 100),
			),
			("title = ifnull(note, title)", lambda Q: Q.update(T).set(T.title, fn.IfNull(T.note, T.title))),
			("note = title", lambda Q: Q.update(T).set(T.note, T.title).where(T.qty > 10)),
			(
				"posting_date = date(stamp)",
				lambda Q: Q.update(T).set(T.posting_date, fn.Date(T.stamp)).where(T.stamp.notnull()),
			),
			(
				"stamp = timestamp(posting_date, at)",
				lambda Q: Q.update(T)
				.set(T.stamp, qf.Timestamp(T.posting_date, T.at))
				.where(T.at.notnull() & T.posting_date.notnull()),
			),
			("amount = ifnull(amount,0)+1", lambda Q: Q.update(T).set(T.amount, fn.IfNull(T.amount, 0) + 1)),
			(
				"where with expression",
				lambda Q: Q.update(T).set(T.flag, 0).where(fn.IfNull(T.amount, 0) > 50),
			),
			("delete with expression", lambda Q: Q.from_(T).delete().where(T.qty * T.flag > 20)),
			(
				"update where name in (sub-query)",
				lambda Q: Q.update(T)
				.set(T.flag, 1)
				.where(T.name.isin(Q.from_(K).select(K.parent).where(K.qty > 100))),
			),
			(
				"delete where title not in (sub-query)",
				lambda Q: Q.from_(T)
				.delete()
				.where(T.title.notin(Q.from_(K).select(K.item).where(K.item.notnull()))),
			),
		]
		failures = []
		for label, build in steps:
			try:
				build(MariaDB).run()
				frappe.db.commit()
			except Exception as e:
				failures.append(f"{label}: MariaDB failed: {e}")
				continue
			try:
				sql, params = render(build(SurrealDB), None, self.loader)
				self.sdb.sql(sql, params.values)
				self.sdb.commit()
			except Exception as e:
				failures.append(f"{label}: SurrealDB raised {type(e).__name__}: {str(e)[:200]}")
				self.sdb.rollback()
				continue
			self.both(f"after {label}", self.select_all(), failures)
			self.both(
				f"after {label} (title via shadow)", self.select_all(lambda: T.title == "eclair"), failures
			)
		self.report(failures, "expression writes")
