"""MariaDB-vs-SurrealDB parity: the same PyPika query objects run on both engines over identical data.

Needs a MariaDB site (the reference) and a SurrealDB server (skipped otherwise). Every difference is reported with the query."""

import datetime as dt
import random
import unittest
from decimal import Decimal

from pypika import Table

import frappe
from frappe.database.schema import DbColumn
from frappe.database.surrealdb import schema as S
from frappe.database.surrealdb.collation import ci_key as collation_key
from frappe.database.surrealdb.errors import SurrealDBError
from frappe.database.surrealdb.translator import render
from frappe.query_builder.builder import MariaDB
from frappe.query_builder.surrealdb_builder import SurrealDB
from frappe.tests import UnitTestCase
from frappe.tests.surrealdb_live import LIVE, SKIP_REASON, LiveSurrealDB

TABLE = "tabParityZz"
T = Table(TABLE)
COLS = ["name", "title", "note", "qty", "amount", "flag", "posting_date", "stamp", "at", "notes"]

STRINGS = [
    "Apple", "apple", "APPLE", "äpple", "Äpple", "Zebra", "zebra", "ábc", "abc", "Abd", "Éclair", "eclair", "straße",
    "strasse", "STRASSE", "a", "a  ", "b", "B", "é", "e", "日本", "日本 ", "😀", "😁", "O'Brien", "Apple pie",
    "apple tart", "50% off", "50 off", "a_b", "aXb", "a\\b", "ﬁne", "fine", "x​y", "xy", "résumé", "RESUME",
    "Resume", "", "a\tb", "Ünï", "CAFÉ", "cafe",
]  # fmt: skip
DATES = [
	dt.date(2023, 12, 31),
	dt.date(2024, 1, 1),
	dt.date(2024, 1, 5),
	dt.date(2024, 2, 29),
	dt.date(2024, 12, 31),
]

MARIADB_DDL = f"""create table `{TABLE}` (
    name varchar(140) primary key, creation datetime(6), modified datetime(6), modified_by varchar(140), owner varchar(140),
    docstatus tinyint(4) not null default 0, idx int(11) not null default 0, title varchar(140), note varchar(140),
    qty int(11) not null default 0, amount decimal(21,9), flag tinyint(4) not null default 0, posting_date date,
    stamp datetime(6), at time(6), notes longtext, index title(title))
    ENGINE=InnoDB ROW_FORMAT=DYNAMIC CHARACTER SET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""


def col(fieldtype, fieldname, **kw):
	args = dict(
        table=None, fieldname=fieldname, fieldtype=fieldtype, length=None, default=None, set_index=0, options=None,
        unique=0, precision=None, not_nullable=0,
    )  # fmt: skip
	args.update(kw)
	return S.column_spec_from_docfield(DbColumn(**args))


def surreal_specs():
	return [
        col("Data", "title", set_index=1), col("Data", "note"), col("Int", "qty", not_nullable=1), col("Duration", "amount"),
        col("Check", "flag"), col("Date", "posting_date"), col("Datetime", "stamp"), col("Time", "at"), col("Long Text", "notes"),
    ]  # fmt: skip


def make_time(rng: random.Random) -> dt.timedelta:
	"""TIME(6) value. MySQLdb formats a *negative* timedelta wrongly (even without microseconds: a driver quirk, not an engine
	one), so the fixture uses non-negative times; negative ones are covered by the value-encoder unit tests."""
	return dt.timedelta(seconds=rng.randint(0, 150000), microseconds=rng.choice([0, 5]))


def make_rows(rng: random.Random) -> list[dict]:
	rows = []
	names = [f"PZ-{i:04d}" for i in range(170)] + [
		"Résumé Doc",
		"straße",
		"日本語",
		"a b",
		"Tab\tName",
		"x​y",
		"Ünï",
		"O'Neil",
	]
	for _i, name in enumerate(names):
		stamp = dt.datetime(2023, 12, 30) + dt.timedelta(
			seconds=rng.randint(0, 40_000_000), microseconds=rng.choice([0, 0, 1, 500000, 999999])
		)
		rows.append(
			{
				"name": name,
				"creation": stamp,
				"modified": stamp,
				"modified_by": rng.choice(["Administrator", "user@example.com", None]),
				"owner": rng.choice(["Administrator", "USER@example.com"]),
				"title": None if rng.random() < 0.1 else rng.choice(STRINGS),
				"note": None if rng.random() < 0.4 else rng.choice(STRINGS),
				"qty": rng.randint(-5, 20),
				"amount": None if rng.random() < 0.3 else Decimal(rng.randint(-50000, 500000)) / 100,
				"flag": rng.randint(0, 1),
				"posting_date": None if rng.random() < 0.2 else rng.choice(DATES),
				"stamp": None if rng.random() < 0.2 else stamp,
				"at": None if rng.random() < 0.3 else make_time(rng),
				"notes": None
				if rng.random() < 0.5
				else "long text " + rng.choice(STRINGS) * rng.randint(1, 30),
			}
		)
	rows[0]["amount"] = Decimal("999999999999.999999999")
	rows[1]["amount"] = Decimal("0.100000000")
	rows[2]["title"] = None
	return rows


INSERT_COLS = ["name", "creation", "modified", "modified_by", "owner", *COLS[1:]]


def describe(expected, got) -> str:
	"""Compact difference of two result sets: per-column diffs for rows with the same first value, else the row counts."""
	by_e = {r[0]: r for r in expected}
	by_g = {r[0]: r for r in got}
	out = []
	for key, row in by_e.items():
		other = by_g.get(key)
		if other is not None and other != row and len(other) == len(row):
			wrong = [(i, a, b) for i, (a, b) in enumerate(zip(row, other, strict=True)) if a != b]
			out.append(
				f"{key!r}: " + ", ".join(f"col{i} MariaDB={a!r} SurrealDB={b!r}" for i, a, b in wrong[:3])
			)
		if len(out) >= 3:
			break
	missing = [k for k in by_e if k not in by_g][:3]
	extra = [k for k in by_g if k not in by_e][:3]
	if not out and expected != got and not (missing or extra):
		return "same rows, different order"
	return f"{'; '.join(out)} | only MariaDB: {missing} | only SurrealDB: {extra}"


def canon(value):
	if isinstance(value, float | Decimal):
		return round(float(value), 9)
	return value


def canon_rows(rows):
	return [tuple(canon(v) for v in row) for row in rows]


def where_cases():
	"""(label, criterion builder) — each is evaluated on both engines."""
	t = T
	c = {}
	for v in ["resume", "STRASSE", "", "a", "日本", "Ünï", "x​y", "eclair", "O'Brien", "no such value"]:
		c[f"title = {v!r}"] = lambda v=v: t.title == v
		c[f"title != {v!r}"] = lambda v=v: t.title != v
		c[f"NOT title = {v!r}"] = lambda v=v: ~(t.title == v)
	c["title IN"] = lambda: t.title.isin(["resume", "APPLE", "日本 ", "", "zebra"])
	c["title NOT IN"] = lambda: t.title.notin(["resume", "APPLE", "日本 ", ""])
	c["NOT title IN"] = lambda: ~t.title.isin(["resume", "APPLE"])
	for op, v in [("<", "b"), ("<=", "Apple"), (">", "z"), (">=", "é"), (">", ""), ("<", "a  ")]:
		c[f"title {op} {v!r}"] = lambda op=op, v=v: {
			"<": t.title < v,
			"<=": t.title <= v,
			">": t.title > v,
			">=": t.title >= v,
		}[op]
	c["title BETWEEN"] = lambda: t.title.between("a", "b")
	c["NOT title BETWEEN"] = lambda: ~t.title.between("a", "b")
	for p in [
		"app%",
		"%PLE",
		"%pp%",
		"a_c",
		"_pple",
		"50\\%%",
		"a\\_b",
		"%",
		"",
		"a",
		"a%",
		"%é%",
		"strass_",
		"%ß",
		"x_y",
		"日本%",
		"%\\\\%",
		"O'%",
		"_",
		"__",
	]:
		c[f"title LIKE {p!r}"] = lambda p=p: t.title.like(p)
		c[f"title NOT LIKE {p!r}"] = lambda p=p: t.title.not_like(p)
		c[f"NOT title LIKE {p!r}"] = lambda p=p: ~t.title.like(p)
	c["note IS NULL"] = lambda: t.note.isnull()
	c["note IS NOT NULL"] = lambda: t.note.notnull()
	c["NOT note IS NULL"] = lambda: ~t.note.isnull()
	c["note = a"] = lambda: t.note == "a"
	c["note != a"] = lambda: t.note != "a"
	c["NOT note = a"] = lambda: ~(t.note == "a")
	c["note NOT IN"] = lambda: t.note.notin(["a", "b"])
	c["NOT note LIKE"] = lambda: ~t.note.like("a%")
	c["note = None"] = lambda: t.note == None  # noqa: E711
	for op, v in [
		("=", 3),
		("!=", 3),
		(">", 5),
		(">=", 5),
		("<", 0),
		("<=", 0),
		(">", 2.5),
		(">", "5"),
		("=", "3"),
		("<=", -1.5),
	]:
		c[f"qty {op} {v!r}"] = lambda op=op, v=v: {
			"=": t.qty == v,
			"!=": t.qty != v,
			">": t.qty > v,
			">=": t.qty >= v,
			"<": t.qty < v,
			"<=": t.qty <= v,
		}[op]
	c["qty BETWEEN"] = lambda: t.qty.between(1, 10)
	c["NOT qty BETWEEN"] = lambda: ~t.qty.between(1, 10)
	c["qty IN"] = lambda: t.qty.isin([1, 2, 3])
	c["qty NOT IN"] = lambda: t.qty.notin([1, 2, 3])
	c["amount IS NULL"] = lambda: t.amount.isnull()
	c["amount IS NOT NULL"] = lambda: t.amount.notnull()
	for op, v in [
		(">", 10.5),
		("<", 0),
		("=", 0),
		("!=", 0),
		("=", 0.1),
		(">=", 999999999999.0),
		("<", -100.25),
	]:
		c[f"amount {op} {v!r}"] = lambda op=op, v=v: {
			"=": t.amount == v,
			"!=": t.amount != v,
			">": t.amount > v,
			">=": t.amount >= v,
			"<": t.amount < v,
		}[op]
	c["amount BETWEEN"] = lambda: t.amount.between(-1, 100)
	c["NOT amount BETWEEN"] = lambda: ~t.amount.between(-1, 100)
	c["flag = 1"] = lambda: t.flag == 1
	c["NOT flag = 1"] = lambda: ~(t.flag == 1)
	c["flag != 1"] = lambda: t.flag != 1
	c["date = 2024-01-05"] = lambda: t.posting_date == "2024-01-05"
	c["date >= date obj"] = lambda: t.posting_date >= dt.date(2024, 1, 1)
	c["date < str"] = lambda: t.posting_date < "2024-02-29"
	c["date BETWEEN"] = lambda: t.posting_date.between("2024-01-01", "2024-02-29")
	c["date IS NULL"] = lambda: t.posting_date.isnull()
	c["date != "] = lambda: t.posting_date != "2024-01-05"
	c["date IN"] = lambda: t.posting_date.isin(["2024-01-05", "2023-12-31"])
	c["stamp > date literal"] = lambda: t.stamp > "2024-01-05"
	c["stamp <= datetime literal"] = lambda: t.stamp <= "2024-01-05 10:00:00.5"
	c["stamp BETWEEN"] = lambda: t.stamp.between("2024-01-01", "2024-03-01 00:00:00")
	c["stamp >= datetime obj"] = lambda: t.stamp >= dt.datetime(2024, 1, 5, 0, 0, 0, 1)
	c["time > str"] = lambda: t.at > "01:00:00"
	c["time < timedelta"] = lambda: t.at < dt.timedelta(hours=2)
	c["time = 0"] = lambda: t.at == "00:00:00"
	c["time BETWEEN"] = lambda: t.at.between("00:00:00", "10:00:00")
	c["and"] = lambda: (t.qty > 5) & (t.title == "apple")
	c["or"] = lambda: (t.qty > 15) | (t.note == "a")
	c["NOT (or)"] = lambda: ~((t.qty > 5) | (t.note == "a"))
	c["NOT (and)"] = lambda: ~((t.qty > 5) & (t.title == "resume"))
	c["nested"] = lambda: ((t.qty > 5) | ~(t.note.like("a%"))) & ~((t.amount < 0) | t.title.isnull())
	c["deep NOT"] = lambda: ~~((t.qty > 5) & ~(t.title == "apple"))
	c["title and note"] = lambda: (t.title == t.note)
	c["qty > flag columns"] = lambda: t.qty > t.flag
	c["name = pk ci"] = lambda: t.name == "pz-0001"
	c["name IN"] = lambda: t.name.isin(["PZ-0001", "pz-0002", "RÉSUMÉ DOC", "STRASSE", "日本語"])
	c["name LIKE"] = lambda: t.name.like("PZ-00%")
	c["name >"] = lambda: t.name > "PZ-0100"
	c["name BETWEEN"] = lambda: t.name.between("PZ-0010", "PZ-0020")
	c["name != "] = lambda: t.name != "pz-0001"
	return c


ORDER_CASES = {
	"title,name": lambda q: q.orderby(T.title).orderby(T.name),
	"title desc,name": lambda q: q.orderby(T.title, order=_desc()).orderby(T.name),
	"note,name (nulls)": lambda q: q.orderby(T.note).orderby(T.name),
	"note desc,name (nulls)": lambda q: q.orderby(T.note, order=_desc()).orderby(T.name),
	"qty desc,name": lambda q: q.orderby(T.qty, order=_desc()).orderby(T.name),
	"amount,name (nulls)": lambda q: q.orderby(T.amount).orderby(T.name),
	"amount desc,name": lambda q: q.orderby(T.amount, order=_desc()).orderby(T.name),
	"posting_date,name (nulls)": lambda q: q.orderby(T.posting_date).orderby(T.name),
	"stamp desc,name": lambda q: q.orderby(T.stamp, order=_desc()).orderby(T.name),
	"at,name": lambda q: q.orderby(T.at).orderby(T.name),
	"name": lambda q: q.orderby(T.name),
	"name desc": lambda q: q.orderby(T.name, order=_desc()),
	"title,name limit 10": lambda q: q.orderby(T.title).orderby(T.name).limit(10),
	"qty,name limit 7 offset 5": lambda q: q.orderby(T.qty).orderby(T.name).limit(7).offset(5),
	"name limit 5 offset 170": lambda q: q.orderby(T.name).limit(5).offset(170),
}


def _desc():
	from pypika import Order

	return Order.desc


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestSurrealDBParityLive(LiveSurrealDB, UnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if frappe.db.db_type != "mariadb":
			raise unittest.SkipTest("MariaDB is the reference engine")

	def setUp(self):
		db_name, db_user, password = self.new_site()
		self.provision(db_name, db_user, password)
		self.addCleanup(self._drop_site, db_name, db_user, password)
		self.sdb = self.connect(db_name, db_user, password)
		self.addCleanup(self.sdb.close)
		frappe.db.sql_ddl(f"drop table if exists `{TABLE}`")
		frappe.db.sql_ddl(MARIADB_DDL)
		self.addCleanup(lambda: frappe.db.sql_ddl(f"drop table if exists `{TABLE}`"))
		for stmt in S.create_statements(TABLE, surreal_specs()):
			self.sdb.sql_ddl(stmt)
		S.clear_schema_cache()
		self.rows = make_rows(random.Random(20260920))
		self.loader = lambda name: S.table_schema(name, db=self.sdb)
		self.load(self.rows)

	def _drop_site(self, db_name, db_user, password):
		from frappe.database.surrealdb import setup_db

		with self._site_conf(db_name, db_user, password):
			setup_db.drop_user_and_database(db_name, db_user)
		S.clear_schema_cache()

	# --- both engines ------------------------------------------------------------------------------------------------------
	def load(self, rows):
		for row in rows:
			frappe.db.sql(
				f"insert into `{TABLE}` ({', '.join(INSERT_COLS)}) values ({', '.join(['%s'] * len(INSERT_COLS))})",
				tuple(row[c] for c in INSERT_COLS),
			)
		frappe.db.commit()
		query = SurrealDB.into(T).columns(*INSERT_COLS)
		for row in rows:
			query = query.insert(*[row[c] for c in INSERT_COLS])
		sql, params = render(query, None, self.loader)
		self.sdb.sql(sql, params.values)
		self.sdb.commit()

	def run_maria(self, build):
		return canon_rows(build(MariaDB).run())

	def run_surreal(self, build):
		sql, params = render(build(SurrealDB), None, self.loader)
		result = self.sdb.sql(sql, params.values)
		self.sdb.commit()
		return canon_rows(result) if isinstance(result, tuple | list) else result

	def both(self, label, build, failures, ordered=True):
		try:
			expected = self.run_maria(build)
		except Exception as e:
			failures.append(f"{label}: MariaDB itself failed: {e}")
			return
		try:
			got = self.run_surreal(build)
		except Exception as e:
			self.sdb.rollback()
			failures.append(
				f"{label}: SurrealDB raised {type(e).__name__}: {str(getattr(e, 'raw', e))[:200]}"
			)
			return
		if not ordered:
			expected, got = sorted(expected, key=repr), sorted(got, key=repr)
		if expected != got:
			failures.append(f"{label}: {len(expected)} vs {len(got)} rows; {describe(expected, got)}")

	def select_all(self, criterion=None):
		def build(Q):
			q = Q.from_(T).select(*[T[c] for c in COLS])
			if criterion is not None:
				q = q.where(criterion())
			return q.orderby(T.name)

		return build

	# --- tests --------------------------------------------------------------------------------------------------------------
	def test_loaded_data_is_identical(self):
		failures = []
		self.both("all rows", self.select_all(), failures)
		self.both("select *", lambda Q: Q.from_(T).select("*").orderby(T.name), failures)
		self.assertEqual(failures, [])
		self.assertEqual(len(self.run_surreal(self.select_all())), len(self.rows))

	def test_where_parity(self):
		failures = []
		cases = where_cases()
		for label, criterion in cases.items():
			self.both(f"WHERE {label}", self.select_all(criterion), failures)
		self.assertEqual(
			failures, [], f"{len(failures)} of {len(cases)} predicates disagree:\n" + "\n".join(failures[:25])
		)

	def test_order_and_pagination_parity(self):
		failures = []
		for label, order in ORDER_CASES.items():
			self.both(
				f"ORDER {label}",
				lambda Q, order=order: order(Q.from_(T).select(*[T[c] for c in COLS])),
				failures,
			)
		self.both(
			"aliased projection",
			lambda Q: Q.from_(T).select(T.title.as_("t"), T.qty.as_("q")).orderby(T.name),
			failures,
		)
		self.assertEqual(
			failures, [], f"{len(failures)} ordering cases disagree:\n" + "\n".join(failures[:20])
		)

	def test_write_parity(self):
		failures = []
		steps = [
			("update title (shadows)", lambda Q: Q.update(T).set(T.title, "Éclair").where(T.qty > 15)),
			("update note NULL", lambda Q: Q.update(T).set(T.note, None).where(T.qty < 0)),
			("update qty + 1", lambda Q: Q.update(T).set(T.qty, T.qty + 1).where(T.flag == 1)),
			(
				"update amount + 1.5 (NULL stays NULL)",
				lambda Q: Q.update(T).set(T.amount, T.amount + 1.5).where(T.amount < 1000),
			),
			("update qty - 2", lambda Q: Q.update(T).set(T.qty, T.qty - 2).where(T.title.like("a%"))),
			(
				"update dates",
				lambda Q: Q.update(T)
				.set(T.posting_date, "2024-03-01")
				.set(T.stamp, "2024-03-01 12:00")
				.set(T.at, "02:03:04")
				.where(T.name == "pz-0003"),
			),
			("update all rows", lambda Q: Q.update(T).set(T.flag, 1)),
			("delete", lambda Q: Q.from_(T).delete().where(T.title.like("app%"))),
			("delete none", lambda Q: Q.from_(T).delete().where(T.title == "no such")),
			(
				"insert extra",
				lambda Q: Q.into(T)
				.columns("name", "title", "qty")
				.insert("ZZ-1", "Été", 4)
				.insert("zz-2", None, -1),
			),
		]
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
				failures.append(f"{label}: SurrealDB raised {type(e).__name__}: {str(e)[:160]}")
				self.sdb.rollback()
				continue
			self.both(f"after {label}", self.select_all(), failures)
			self.both(
				f"after {label} (title = eclair via shadow)",
				self.select_all(lambda: T.title == "eclair"),
				failures,
			)
		self.assertEqual(failures, [], f"{len(failures)} write steps disagree:\n" + "\n".join(failures[:20]))

	def test_aggregate_parity(self):
		from pypika import Order
		from pypika import functions as fn

		failures = []
		none = [
			None,
			lambda: T.title == "no such value",
			lambda: T.qty > 5,
			lambda: T.note.isnull(),
			lambda: (T.flag == 1) & (T.qty < 0),
		]
		for w in none:
			label = "all" if w is None else "filtered"

			def scalar(select, w=w):
				return lambda Q: (lambda q: q if w is None else q.where(w()))(Q.from_(T).select(*select))

			for name, select in {
				"count(*)": [fn.Count("*")],
				"count cols": [
					fn.Count(T.title),
					fn.Count(T.note),
					fn.Count(T.amount),
					fn.Count(T.posting_date),
				],
				"sum": [fn.Sum(T.qty), fn.Sum(T.amount), fn.Sum(T.flag)],
				"min/max numbers": [fn.Min(T.qty), fn.Max(T.qty), fn.Min(T.amount), fn.Max(T.amount)],
				"min/max temporal": [
					fn.Min(T.posting_date),
					fn.Max(T.posting_date),
					fn.Min(T.stamp),
					fn.Max(T.stamp),
					fn.Min(T.at),
					fn.Max(T.at),
				],
			}.items():
				self.both(f"{name} ({label})", scalar(select), failures)

		# GROUP BY. Group values on varchar keys are compared by collation key: MariaDB shows one (arbitrary) member of the group
		def by_key(rows):
			return sorted(
				(tuple(collation_key(v) if isinstance(v, str) else v for v in row) for row in rows), key=repr
			)

		groups = {
			"group by flag": lambda Q: Q.from_(T)
			.select(T.flag, fn.Count("*"))
			.groupby(T.flag)
			.orderby(T.flag),
			"group by flag sum/min/max": lambda Q: Q.from_(T)
			.select(T.flag, fn.Sum(T.qty), fn.Min(T.amount), fn.Max(T.stamp))
			.groupby(T.flag)
			.orderby(T.flag),
			"group by date (NULL key)": lambda Q: Q.from_(T)
			.select(T.posting_date, fn.Count("*"), fn.Sum(T.amount))
			.groupby(T.posting_date)
			.orderby(T.posting_date),
			"group by qty": lambda Q: Q.from_(T)
			.select(T.qty, fn.Count(T.note))
			.groupby(T.qty)
			.orderby(T.qty),
			"group by two": lambda Q: Q.from_(T)
			.select(T.flag, T.qty, fn.Count("*"))
			.groupby(T.flag, T.qty)
			.orderby(T.flag)
			.orderby(T.qty),
			"group by where": lambda Q: Q.from_(T)
			.select(T.flag, fn.Count("*"))
			.where(T.qty > 3)
			.groupby(T.flag)
			.orderby(T.flag),
			"having count": lambda Q: Q.from_(T)
			.select(T.qty, fn.Count("*"))
			.groupby(T.qty)
			.having(fn.Count("*") > 8)
			.orderby(T.qty),
			"having sum": lambda Q: Q.from_(T)
			.select(T.flag, fn.Sum(T.qty))
			.groupby(T.flag)
			.having(fn.Sum(T.qty) >= 100)
			.orderby(T.flag),
			"having not": lambda Q: Q.from_(T)
			.select(T.qty, fn.Count("*"))
			.groupby(T.qty)
			.having(~(fn.Count("*") > 8))
			.orderby(T.qty),
			"order by aggregate": lambda Q: Q.from_(T)
			.select(T.qty, fn.Count("*"))
			.groupby(T.qty)
			.orderby(fn.Count("*"), order=Order.desc)
			.orderby(T.qty),
			"group limit offset": lambda Q: Q.from_(T)
			.select(T.qty, fn.Count("*"))
			.groupby(T.qty)
			.orderby(T.qty)
			.limit(4)
			.offset(3),
			"distinct flag,qty": lambda Q: Q.from_(T)
			.select(T.flag, T.qty)
			.distinct()
			.orderby(T.flag)
			.orderby(T.qty),
			"distinct posting_date": lambda Q: Q.from_(T)
			.select(T.posting_date)
			.distinct()
			.orderby(T.posting_date),
		}
		for label, build in groups.items():
			self.both(label, build, failures)

		# collation-equal strings form one group; the shown value is one of the members
		for label, build in {
			"group by title": lambda Q: Q.from_(T)
			.select(T.title, fn.Count("*"), fn.Sum(T.qty))
			.groupby(T.title),
			"distinct title": lambda Q: Q.from_(T).select(T.title).distinct(),
			"group by note,flag": lambda Q: Q.from_(T)
			.select(T.note, T.flag, fn.Count("*"))
			.groupby(T.note, T.flag),
		}.items():
			expected = by_key(self.run_maria(build))
			try:
				got = by_key(self.run_surreal(build))
			except Exception as e:
				self.sdb.rollback()
				failures.append(
					f"{label}: SurrealDB raised {type(e).__name__}: {str(getattr(e, 'raw', e))[:200]}"
				)
				continue
			if expected != got:
				failures.append(f"{label}: {len(expected)} vs {len(got)} groups; {describe(expected, got)}")
		self.assertEqual(
			failures, [], f"{len(failures)} aggregate cases disagree:\n" + "\n".join(failures[:20])
		)

	def test_constraint_error_parity(self):
		"""The same statements must fail on both engines with the same MariaDB error number."""
		cases = {
			"duplicate name (collation)": lambda Q: Q.into(T)
			.columns("name", "qty")
			.insert("PZ-0001", 1)
			.insert("pz-0001", 2),
			"duplicate existing name": lambda Q: Q.into(T).columns("name").insert("pz-0005"),
			"NULL into NOT NULL": lambda Q: Q.into(T).columns("name", "qty").insert("QQ-1", None),
			"title too long": lambda Q: Q.into(T).columns("name", "title").insert("QQ-2", "x" * 141),
			"missing name": lambda Q: Q.into(T).columns("title").insert("x"),
			"unknown column": lambda Q: Q.into(T).columns("name", "nope").insert("QQ-3", 1),
		}
		failures = []
		for label, build in cases.items():
			try:
				build(MariaDB).run()
				frappe.db.commit()
				m_code = None
			except Exception as e:
				frappe.db.rollback()
				m_code = e.args[0] if e.args else None
			try:
				sql, params = render(build(SurrealDB), None, self.loader)
				self.sdb.sql(sql, params.values)
				self.sdb.commit()
				s_code = None
			except SurrealDBError as e:
				self.sdb.rollback()
				s_code = e.code
			if m_code != s_code:
				failures.append(f"{label}: MariaDB error {m_code} vs SurrealDB error {s_code}")
		self.assertEqual(failures, [])

	def test_insert_ignore_skips_existing_names(self):
		def build(Q):
			return (
				Q.into(T)
				.columns("name", "title", "qty")
				.insert("PZ-0001", "changed", 99)
				.insert("NEW-1", "x", 1)
				.ignore()
			)

		build(MariaDB).run()
		frappe.db.commit()
		sql, params = render(build(SurrealDB), None, self.loader)
		self.sdb.sql(sql, params.values)
		self.sdb.commit()
		failures = []
		self.both("after insert ignore", self.select_all(), failures)
		self.assertEqual(failures, [])
