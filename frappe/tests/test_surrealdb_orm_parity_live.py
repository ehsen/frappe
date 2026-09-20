"""P1.7: the `frappe.db` API and `frappe.get_all/get_list` on SurrealDB, compared with MariaDB on identical data.

The reference is a full MariaDB site (`p17_ref`, seeded by `spike/P1.7-orm/seed_p17.py`) with real DocTypes: User + Has Role,
ToDo, Note + Note Seen By, Comment, Role. Their tables and rows are cloned into a throw-away SurrealDB database through the schema
layer (P1.4) and the translator (P1.6). The DocType metadata is loaded (and cached) while MariaDB is current; the *same*
`frappe.db.*` call is then made with `frappe.local.db` / `frappe.local.qb` switched to the SurrealDB backend. Every row of the
conformance table is one such call; a row passes when both engines return the same result (or both raise).

Rows that fail closed on purpose are listed in `EXPECTED_UNSUPPORTED` with the chunk that owns them."""

import contextlib
import datetime as dt
import os
import unittest
from decimal import Decimal

from pypika import Table
from pypika import functions as sqlf
from pypika.terms import Field, LiteralValue

import frappe
from frappe.database.surrealdb import schema as S
from frappe.database.surrealdb.errors import SurrealDBNotImplementedError
from frappe.database.surrealdb.translator import render
from frappe.query_builder.surrealdb_builder import SurrealDB
from frappe.tests import UnitTestCase
from frappe.tests.surrealdb_live import LIVE, SKIP_REASON, LiveSurrealDB

DOCTYPES = ["Role", "User", "Has Role", "ToDo", "Note", "Note Seen By", "Comment"]
REPORT = os.environ.get("P17_REPORT", "/tmp/p17-conformance.md")


def canon(value):
	if isinstance(value, frappe._dict | dict):
		return {k: canon(v) for k, v in sorted(value.items())}
	if isinstance(value, list | tuple):
		return [canon(v) for v in value]
	if isinstance(value, float | Decimal):
		return round(float(value), 9)
	return value


def key(value):
	return repr(value)


@unittest.skipUnless(LIVE, SKIP_REASON)
class OrmParity(LiveSurrealDB, UnitTestCase):
	"""Clones the reference data into SurrealDB once per class; write tests re-clone per test (`per_test = True`)."""

	per_test = False
	results: dict  # row label -> "ok" | reason

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if frappe.db.db_type != "mariadb" or not frappe.db.table_exists("ToDo"):
			raise unittest.SkipTest("needs the seeded MariaDB reference site (p17_ref)")
		if not frappe.db.count("ToDo") or not frappe.db.count("Has Role"):
			raise unittest.SkipTest("the reference site is not seeded (spike/P1.7-orm/seed_p17.py)")
		cls.results = {}
		for doctype in DOCTYPES:
			frappe.get_meta(doctype)  # warm the metadata cache while MariaDB is current
		cls.rows = {dt_: frappe.db.sql(f"select * from `tab{dt_}`", as_dict=True) for dt_ in DOCTYPES}
		if not cls.per_test:
			cls._clone()

	@classmethod
	def _clone(cls):
		inst = cls.__new__(cls)  # the LiveSurrealDB helpers use only class state
		name = user = f"_s{os.urandom(4).hex()}"
		password = f"pw-{os.urandom(4).hex()}"
		inst.provision(name, user, password)
		cls.sdb = inst.connect(name, user, password)
		cls._db_creds = (name, user, password)
		with cls.on_surreal():
			S.clear_schema_cache()
			for doctype in DOCTYPES:
				S.SurrealDBTable(doctype, frappe.get_meta(doctype)).create()
			S.clear_schema_cache()
		cls._copy_rows()

	@classmethod
	def _copy_rows(cls, doctypes=None):
		for doctype in doctypes or DOCTYPES:
			rows = cls.rows[doctype]
			if not rows:
				continue
			table = Table(f"tab{doctype}")
			schema = S.table_schema(f"tab{doctype}", db=cls.sdb)
			columns = [c for c in rows[0] if c in schema.columns]
			for start in range(0, len(rows), 100):
				query = SurrealDB.into(table).columns(*columns)
				for row in rows[start : start + 100]:
					query = query.insert(*[row[c] for c in columns])
				sql, params = render(query, None, lambda n: S.table_schema(n, db=cls.sdb))
				cls.sdb.sql(sql, params.values)
		cls.sdb.commit()

	@classmethod
	def tearDownClass(cls):
		with contextlib.suppress(Exception):
			cls.sdb.close()
		with contextlib.suppress(Exception):
			with cls._site_conf(*cls._db_creds):
				from frappe.database.surrealdb import setup_db

				setup_db.drop_user_and_database(cls._db_creds[0], cls._db_creds[1])
		if getattr(cls, "results", None) is not None and cls.results:
			cls._write_report()
		super().tearDownClass()

	@classmethod
	def _write_report(cls):
		lines = [f"## {cls.__name__}", "", "| row | result |", "|---|---|"]
		lines += [f"| {label} | {'ok' if res == 'ok' else res} |" for label, res in cls.results.items()]
		with open(REPORT, "a") as f:
			f.write("\n".join(lines) + "\n\n")

	# --- engine switch -----------------------------------------------------------------------------------------------------
	@classmethod
	@contextlib.contextmanager
	def on_surreal(cls):
		saved = (frappe.local.db, frappe.local.qb, frappe.local.conf.get("db_type"))
		frappe.local.db, frappe.local.qb = cls.sdb, SurrealDB
		frappe.local.conf["db_type"] = "surrealdb"
		try:
			yield
		finally:
			frappe.local.db, frappe.local.qb = saved[0], saved[1]
			frappe.local.conf["db_type"] = saved[2]

	def call(self, fn, surreal: bool):
		try:
			if surreal:
				with self.on_surreal():
					result = fn()
					self.sdb.rollback()
					return ("ok", canon(result))
			result = fn()
			return ("ok", canon(result))
		except SurrealDBNotImplementedError as e:
			with contextlib.suppress(Exception):
				self.sdb.rollback()
			return ("unsupported", str(e)[:160])
		except Exception as e:
			with contextlib.suppress(Exception):
				self.sdb.rollback()
			frappe.db.rollback()
			return ("error", f"{type(e).__name__}: {str(e)[:140]}")

	def row(self, label: str, fn, ordered: bool = True):
		expected = self.call(fn, surreal=False)
		got = self.call(fn, surreal=True)
		if expected[0] == "ok" and got[0] == "ok":
			a, b = expected[1], got[1]
			if not ordered and isinstance(a, list) and isinstance(b, list):
				a, b = sorted(a, key=key), sorted(b, key=key)
			if a == b:
				size = len(a) if isinstance(a, list) else ("None" if a is None else "scalar")
				self.results[label] = f"ok ({size} rows)" if isinstance(a, list) else f"ok ({size})"
				return
			self.results[label] = f"MISMATCH {self._diff(a, b)}"
		elif expected[0] == got[0] == "error":
			self.results[label] = (
				f"ok (Frappe refuses on both engines: {expected[1][:60]})"
				if label in EXPECTED_BOTH_RAISE
				else f"BOTH RAISE (the call itself is invalid?): {expected[1][:70]} | {got[1][:70]}"
			)
		elif got[0] == "unsupported" and label in EXPECTED_UNSUPPORTED:
			self.results[label] = f"unsupported ({EXPECTED_UNSUPPORTED[label]})"
		else:
			self.results[label] = (
				f"MariaDB {expected[0]}: {str(expected[1])[:80]} | SurrealDB {got[0]}: {str(got[1])[:110]}"
			)

	@staticmethod
	def _diff(a, b) -> str:
		if isinstance(a, list) and isinstance(b, list):
			if len(a) != len(b):
				return f"{len(a)} vs {len(b)} rows"
			for x, y in zip(a, b, strict=True):
				if x != y:
					return f"first differing row: MariaDB={str(x)[:110]} SurrealDB={str(y)[:110]}"
		return f"MariaDB={str(a)[:120]} SurrealDB={str(b)[:120]}"

	def assert_all_ok(self, prefix: str):
		bad = {
			k: v
			for k, v in self.results.items()
			if k.startswith(prefix) and not v.startswith(("ok", "unsupported"))
		}
		self.assertEqual(bad, {}, "\n" + "\n".join(f"{k}: {v}" for k, v in bad.items()))


# labels whose failure to render is intended and owned elsewhere
EXPECTED_BOTH_RAISE = {"get_all raw SQL fragment as filter"}
EXPECTED_UNSUPPORTED = {
	"get_all raw SQL fragment as field": "raw SQL text never reaches SurrealDB (P1.9 rewrites the sites that need it)",
	"get_all raw SQL fragment as filter": "raw SQL text never reaches SurrealDB (P1.9)",
	"get_all ToDo description like (long text)": "long-text collation, P1.6d (owner decision)",
	"get_all Comment content like (long text)": "long-text collation, P1.6d (owner decision)",
	"get_all ToDo description = (long text)": "long-text collation, P1.6d (owner decision)",
}


def first(dt_, **kw):
	return frappe.get_all(
		dt_, fields=["name"], order_by="creation asc, name asc", limit=1, pluck="name", **kw
	)[0]


class TestOrmReads(OrmParity):
	def test_get_value_get_values(self):
		u = "p17u00@example.com"
		todo = first("ToDo")
		rows = {
			"get_value name+field": lambda: frappe.db.get_value("User", u, "first_name"),
			"get_value name case variant": lambda: frappe.db.get_value("User", u.upper(), "first_name"),
			"get_value list of fields": lambda: frappe.db.get_value(
				"User", u, ["first_name", "enabled", "user_type"]
			),
			"get_value as_dict": lambda: frappe.db.get_value(
				"User", u, ["first_name", "enabled"], as_dict=True
			),
			"get_value missing": lambda: frappe.db.get_value("User", "nobody@example.com", "first_name"),
			"get_value filters dict": lambda: frappe.db.get_value(
				"ToDo", {"status": "Open", "priority": "High"}, "name", order_by="creation asc, name asc"
			),
			"get_value filters list": lambda: frappe.db.get_value(
				"ToDo", [["ToDo", "status", "=", "Closed"]], "name", order_by="name asc"
			),
			"get_value filter operators": lambda: frappe.db.get_value(
				"ToDo",
				{"status": ["in", ["Open", "Closed"]], "date": ["is", "set"]},
				"name",
				order_by="name asc",
			),
			"get_value like": lambda: frappe.db.get_value(
				"User", {"email": ["like", "p17u0%"]}, "email", order_by="name asc"
			),
			"get_value date field": lambda: frappe.db.get_value(
				"ToDo", todo, ["date", "creation", "modified"]
			),
			"get_value ifnull": lambda: frappe.db.get_value("ToDo", todo, ["description", "allocated_to"]),
			"get_value aggregate": lambda: frappe.db.get_value(
				"ToDo", {"status": "Open"}, [{"COUNT": "name"}]
			),
			"get_value sum": lambda: frappe.db.get_value("ToDo", {"status": "Open"}, [{"SUM": "idx"}]),
			"get_value max date": lambda: frappe.db.get_value("ToDo", {}, [{"MAX": "date"}]),
			"get_value max creation (pypika)": lambda: frappe.db.get_value(
				"ToDo", {}, sqlf.Max(Field("creation"))
			),
			"get_values *": lambda: frappe.db.get_values(
				"ToDo", {"status": "Open"}, "*", order_by="name asc", as_dict=True
			),
			"get_values fields": lambda: frappe.db.get_values(
				"ToDo", {"status": "Open"}, ["name", "priority", "date"], order_by="name asc"
			),
			"get_values pluck": lambda: frappe.db.get_values(
				"ToDo", {"status": "Open"}, "name", order_by="name asc", pluck="name"
			),
			"get_values list filter names": lambda: frappe.db.get_values(
				"User",
				["p17u01@example.com", "P17U02@EXAMPLE.COM"],
				["name", "first_name"],
				order_by="name asc",
			),
			"get_values limit": lambda: frappe.db.get_values(
				"ToDo", {}, ["name"], order_by="name asc", limit=5
			),
			"get_values distinct (Frappe drops ORDER BY)": lambda: sorted(
				frappe.db.get_values("ToDo", {}, ["status"], distinct=True, order_by="status asc")
			),
		}
		for label, fn in rows.items():
			self.row(label, fn)
		self.assert_all_ok("get_value")

	def test_get_all_fields_order_pagination(self):
		fields = {
			"get_all default": lambda: frappe.get_all("ToDo", limit=10),
			"get_all fields": lambda: frappe.get_all(
				"ToDo", fields=["name", "status", "priority"], order_by="name asc"
			),
			"get_all *": lambda: frappe.get_all("ToDo", fields=["*"], order_by="name asc"),
			"get_all alias": lambda: frappe.get_all(
				"ToDo", fields=["name as n", "status as s"], order_by="name asc"
			),
			"get_all pluck": lambda: frappe.get_all("ToDo", pluck="name", order_by="name asc"),
			"get_all as_list": lambda: frappe.get_all(
				"ToDo", fields=["name", "status"], as_list=True, order_by="name asc"
			),
			"get_all distinct": lambda: frappe.get_all(
				"ToDo", fields=["status", "priority"], distinct=True, order_by="status asc, priority asc"
			),
			"get_all order desc": lambda: frappe.get_all(
				"ToDo", fields=["name"], order_by="creation desc, name asc"
			),
			"get_all order two": lambda: frappe.get_all(
				"ToDo", fields=["name", "status"], order_by="status asc, name desc"
			),
			"get_all order by modified (default)": lambda: frappe.get_all("ToDo", fields=["name"], limit=15),
			"get_all page": lambda: frappe.get_all(
				"ToDo", fields=["name"], order_by="name asc", limit_start=5, limit_page_length=7
			),
			"get_all limit offset": lambda: frappe.get_all(
				"ToDo", fields=["name"], order_by="name asc", limit=4, offset=6
			),
			"get_all count": lambda: frappe.get_all("ToDo", fields=[{"COUNT": "*", "as": "c"}]),
			"get_all count group": lambda: frappe.get_all(
				"ToDo",
				fields=["status", {"COUNT": "name", "as": "c"}],
				group_by="status",
				order_by="status asc",
			),
			"get_all sum group": lambda: frappe.get_all(
				"ToDo",
				fields=["priority", {"SUM": "idx", "as": "s"}, {"MAX": "date", "as": "d"}],
				group_by="priority",
				order_by="priority asc",
			),
			"get_all avg min": lambda: frappe.get_all(
				"ToDo",
				fields=["status", {"AVG": "idx", "as": "a"}, {"MIN": "creation", "as": "m"}],
				group_by="status",
				order_by="status asc",
			),
			"get_all count distinct": lambda: frappe.get_all(
				"ToDo", fields=[sqlf.Count(Field("status")).distinct().as_("c")]
			),
			"get_all ifnull field": lambda: frappe.get_all(
				"ToDo", fields=["name", {"IFNULL": ["allocated_to", "'x'"], "as": "who"}], order_by="name asc"
			),
			"get_all nullif": lambda: frappe.get_all(
				"ToDo", fields=["name", {"NULLIF": ["priority", "'Low'"], "as": "p"}], order_by="name asc"
			),
			"get_all concat": lambda: frappe.get_all(
				"ToDo", fields=["name", {"CONCAT": ["status", "priority"], "as": "sp"}], order_by="name asc"
			),
			"get_all year month": lambda: frappe.get_all(
				"ToDo",
				fields=[
					"name",
					{"YEAR": "date", "as": "y"},
					{"MONTH": "date", "as": "m"},
					{"QUARTER": "date", "as": "q"},
					{"MONTHNAME": "date", "as": "mn"},
				],
				order_by="name asc",
			),
			"get_all extract (pypika)": lambda: frappe.get_all(
				"ToDo", fields=["name", sqlf.Extract("year", Field("date")).as_("y")], order_by="name asc"
			),
			"get_all abs": lambda: frappe.get_all(
				"ToDo", fields=["name", {"ABS": "idx", "as": "a"}], order_by="name asc"
			),
			"get_all timestamp": lambda: frappe.get_all(
				"ToDo", fields=["name", {"TIMESTAMP": "date", "as": "t"}], order_by="name asc"
			),
			"get_all arithmetic": lambda: frappe.get_all(
				"ToDo",
				fields=[
					"name",
					{"ADD": ["idx", 5], "as": "s"},
					{"MUL": ["idx", 2], "as": "m"},
					{"DIV": ["idx", 4], "as": "d"},
				],
				order_by="name asc",
			),
			"get_all count children": lambda: frappe.get_all(
				"Has Role",
				fields=["parent", {"COUNT": "name", "as": "c"}],
				group_by="parent",
				order_by="parent asc",
				parent_doctype="User",
			),
			"get_all date fields": lambda: frappe.get_all(
				"User", fields=["name", "creation", "modified", "last_login"], order_by="name asc"
			),
			"get_all User *": lambda: frappe.get_all(
				"User", fields=["*"], filters={"name": ["like", "p17u%"]}, order_by="name asc"
			),
			"get_all Comment": lambda: frappe.get_all(
				"Comment", fields=["name", "comment_type", "reference_name"], order_by="name asc"
			),
			"get_all Role": lambda: frappe.get_all("Role", fields=["name", "disabled"], order_by="name asc"),
		}
		for label, fn in fields.items():
			self.row(label, fn)
		self.assert_all_ok("get_all")

	def test_filters(self):
		users = [f"p17u{i:02d}@example.com" for i in range(4)]
		owner = frappe.get_all(
			"ToDo", filters={"allocated_to": ["is", "set"]}, pluck="allocated_to", order_by="name asc"
		)[0]
		some_date = frappe.db.get_value("ToDo", {"date": ["is", "set"]}, "date", order_by="name asc")
		flt = {
			"filter dict eq": {"status": "Open"},
			"filter dict eq case": {"status": "open"},
			"filter two eq": {"status": "Open", "priority": "High"},
			"filter !=": {"status": ["!=", "Open"]},
			"filter in": {"status": ["in", ["Open", "Closed"]]},
			"filter in string": {"status": ["in", "Open,Closed"]},
			"filter not in": {"status": ["not in", ["Open"]]},
			"filter like": {"priority": ["like", "%ow"]},
			"filter not like": {"priority": ["not like", "hi%"]},
			"filter >": {"idx": [">", -1]},
			"filter <=": {"idx": ["<=", 0]},
			"filter between dates": {"date": ["between", ["2024-02-01", "2024-04-30"]]},
			"filter date >": {"date": [">", "2024-03-01"]},
			"filter date =": {"date": str(some_date)},
			"filter date = datetime": {"date": some_date.strftime("%Y-%m-%d") + " 10:00:00"},
			"filter is set": {"allocated_to": ["is", "set"]},
			"filter is not set": {"allocated_to": ["is", "not set"]},
			"filter date is not set": {"date": ["is", "not set"]},
			"filter link eq": {"allocated_to": owner},
			"filter link in": {"allocated_to": ["in", users]},
			"filter link eq case": {"allocated_to": owner.upper()},
			"filter reference_name not set": {"reference_name": ["is", "not set"]},
			"filter name like": {"name": ["like", "%a%"]},
			"filter creation >": {"creation": [">", "2020-01-01"]},
			"filter creation between": {"creation": ["between", ["2020-01-01", "2100-01-01"]]},
			"filter modified <": {"modified": ["<", "2100-01-01 00:00:00"]},
			"filter None value": {"reference_type": None},
			"filter empty string": {"reference_type": ""},
			"filter Select eq set": {"reference_type": ["=", "User"]},
			"filter dynamic link": {"reference_type": "User", "reference_name": ["like", "p17u0%"]},
		}
		for label, filters in flt.items():
			self.row(
				f"get_all ToDo {label}",
				lambda f=filters: frappe.get_all("ToDo", fields=["name"], filters=f, order_by="name asc"),
			)
		# list-style and or_filters
		lists = {
			"list filter": [["ToDo", "status", "=", "Open"], ["ToDo", "priority", "in", ["High", "Low"]]],
			"list filter short": [["status", "=", "Open"]],
			"list filter like": [["description", "is", "set"]],
		}
		for label, filters in lists.items():
			self.row(
				f"get_all ToDo {label}",
				lambda f=filters: frappe.get_all("ToDo", fields=["name"], filters=f, order_by="name asc"),
			)
		self.row(
			"get_all ToDo or_filters",
			lambda: frappe.get_all(
				"ToDo",
				fields=["name"],
				filters={"status": "Open"},
				or_filters={"priority": "High", "allocated_to": users[1]},
				order_by="name asc",
			),
		)
		self.row(
			"get_all ToDo or_filters list",
			lambda: frappe.get_all(
				"ToDo",
				fields=["name"],
				or_filters=[["priority", "=", "Low"], ["status", "=", "Closed"]],
				order_by="name asc",
			),
		)
		for label, spec in {
			"timespan last 7 days": ["timespan", "last 7 days"],
			"timespan this year": ["timespan", "this year"],
			"timespan last month": ["timespan", "last month"],
		}.items():
			self.row(
				f"get_all ToDo filter creation {label}",
				lambda spec=spec: frappe.get_all(
					"ToDo", filters={"creation": spec}, pluck="name", order_by="name asc"
				),
			)
		self.row(
			"get_all raw SQL fragment as field",
			lambda: frappe.get_all("ToDo", fields=["name", LiteralValue("1 as one")], order_by="name asc"),
		)
		self.row(
			"get_all raw SQL fragment as filter",
			lambda: frappe.get_all("ToDo", filters=[LiteralValue("1=1")], pluck="name", order_by="name asc"),
		)
		self.row(
			"get_all pypika criterion filter",
			lambda: frappe.get_all(
				"ToDo",
				filters=(Field("status") == "Open") | (Field("priority") == "Low"),
				pluck="name",
				order_by="name asc",
			),
		)
		self.row(
			"get_all User enabled",
			lambda: frappe.get_all("User", filters={"enabled": 1}, pluck="name", order_by="name asc"),
		)
		self.row(
			"get_all User enabled str",
			lambda: frappe.get_all("User", filters={"enabled": "1"}, pluck="name", order_by="name asc"),
		)
		self.row(
			"get_all User user_type",
			lambda: frappe.get_all(
				"User",
				filters={"user_type": "Website User", "name": ["like", "p17u%"]},
				pluck="name",
				order_by="name asc",
			),
		)
		self.row(
			"get_all User name in accents",
			lambda: frappe.get_all(
				"User",
				filters={"first_name": ["in", ["zoe", "ANA", "jose"]]},
				pluck="name",
				order_by="name asc",
			),
		)
		self.row(
			"get_all User first_name like accents",
			lambda: frappe.get_all(
				"User", filters={"first_name": ["like", "%oe"]}, pluck="name", order_by="name asc"
			),
		)
		self.row(
			"get_all User last_name is set",
			lambda: frappe.get_all(
				"User",
				filters={"last_name": ["is", "set"], "name": ["like", "p17u%"]},
				pluck="name",
				order_by="name asc",
			),
		)
		self.row(
			"get_all ToDo description like (long text)",
			lambda: frappe.get_all(
				"ToDo", filters={"description": ["like", "%alpha%"]}, pluck="name", order_by="name asc"
			),
		)
		self.row(
			"get_all ToDo description = (long text)",
			lambda: frappe.get_all(
				"ToDo", filters={"description": "p17-alpha task 3"}, pluck="name", order_by="name asc"
			),
		)
		self.row(
			"get_all Comment content like (long text)",
			lambda: frappe.get_all(
				"Comment", filters={"content": ["like", "%resume%"]}, pluck="name", order_by="name asc"
			),
		)
		self.row(
			"get_all Comment comment_type",
			lambda: frappe.get_all(
				"Comment",
				filters={"comment_type": ["in", ["Comment", "Info"]], "reference_doctype": "User"},
				pluck="name",
				order_by="name asc",
			),
		)
		self.assert_all_ok("get_all")

	def test_child_tables_and_links(self):
		role_user = (
			"Administrator"  # the seeded users get no roles from the User controller; Administrator has ~70
		)
		rows = {
			"get_all User child field": lambda: frappe.get_all(
				"User",
				fields=["name", "`tabHas Role`.role"],
				filters={"name": ["like", "p17u%"]},
				order_by="name asc, `tabHas Role`.role asc",
			),
			"get_all User filter on child": lambda: frappe.get_all(
				"User", fields=["name"], filters=[["Has Role", "role", "=", "Desk User"]], order_by="name asc"
			),
			"get_all Note child filter": lambda: frappe.get_all(
				"Note",
				fields=["name"],
				filters=[["Note Seen By", "user", "like", "p17u0%"]],
				order_by="name asc",
				distinct=True,
			),
			"get_all Has Role by parent": lambda: frappe.get_all(
				"Has Role",
				fields=["parent", "role"],
				filters={"parenttype": "User", "parent": role_user},
				order_by="idx asc, role asc",
				parent_doctype="User",
			),
			"get_all Has Role parent in": lambda: frappe.get_all(
				"Has Role",
				fields=["parent", "role"],
				filters={"parent": ["in", [role_user, role_user.upper()]]},
				order_by="parent asc, role asc",
				parent_doctype="User",
			),
			"get_all link field join": lambda: frappe.get_all(
				"ToDo", fields=["name", "allocated_to.first_name as who"], order_by="name asc"
			),
			"get_all link field filter": lambda: frappe.get_all(
				"ToDo", fields=["name"], filters={"allocated_to.enabled": 1}, order_by="name asc"
			),
			"get_all children of doc": lambda: frappe.get_all(
				"Note Seen By",
				fields=["parent", "user"],
				order_by="parent asc, user asc",
				parent_doctype="Note",
			),
		}
		for label, fn in rows.items():
			self.row(label, fn)
		self.assert_all_ok("get_all")

	def test_exists_count(self):
		u = "p17u00@example.com"
		rows = {
			"exists name": lambda: frappe.db.exists("User", u),
			"exists name upper": lambda: frappe.db.exists("User", u.upper()),
			"exists missing": lambda: frappe.db.exists("User", "nobody@example.com"),
			"exists dict": lambda: frappe.db.exists("ToDo", {"status": "Open"}) is not None,
			"exists dict none": lambda: frappe.db.exists("ToDo", {"status": "no such"}),
			"exists doctype-only": lambda: frappe.db.exists("User"),
			"exists list filters": lambda: frappe.db.exists("User", [["email", "like", "p17u0%"]])
			is not None,
			"a_row_exists": lambda: bool(frappe.db.a_row_exists("ToDo")),
			"count all": lambda: frappe.db.count("ToDo"),
			"count filters": lambda: frappe.db.count("ToDo", {"status": "Open"}),
			"count filters like": lambda: frappe.db.count("User", {"name": ["like", "p17u%"]}),
			"count distinct": lambda: frappe.db.count("ToDo", distinct=True),
			"count child": lambda: frappe.db.count("Has Role", {"parent": u}),
			"get_creation_count": lambda: frappe.db.get_creation_count("ToDo", 1000000),
			"has_column": lambda: (
				frappe.db.has_column("ToDo", "status"),
				frappe.db.has_column("ToDo", "nope"),
			),
			"table_exists": lambda: (frappe.db.table_exists("ToDo"), frappe.db.table_exists("Nope")),
			"get_table_columns": lambda: sorted(frappe.db.get_table_columns("ToDo")),
		}
		for label, fn in rows.items():
			self.row(label, fn)
		self.assert_all_ok("")


class TestOrmWrites(OrmParity):
	per_test = True

	def setUp(self):
		self.__class__._clone()  # a fresh clone for every test

	def tearDown(self):
		with contextlib.suppress(Exception):
			self._restore_maria()  # leave the reference site exactly as it was seeded
		with contextlib.suppress(Exception):
			self.sdb.close()

	def state(self):
		"""Everything the writes may have changed, from the engine that is current."""
		return {
			"todo": frappe.get_all(
				"ToDo",
				fields=["name", "status", "priority", "date", "allocated_to", "idx", "modified_by"],
				order_by="name asc",
			),
			"role": frappe.get_all("Has Role", fields=["name", "parent", "role"], order_by="name asc"),
		}

	def test_writes(self):
		todo = first("ToDo")
		todo2 = frappe.get_all("ToDo", pluck="name", order_by="creation asc, name asc", limit=2)[1]
		users = [f"p17u{i:02d}@example.com" for i in range(3)]
		steps = {
			"set_value one": lambda: frappe.db.set_value(
				"ToDo", todo, "status", "Closed", update_modified=False
			),
			"set_value dict": lambda: frappe.db.set_value(
				"ToDo", todo, {"status": "Cancelled", "priority": "Low"}, update_modified=False
			),
			"set_value by filters": lambda: frappe.db.set_value(
				"ToDo", {"status": "Open"}, "priority", "High", update_modified=False
			),
			"set_value None": lambda: frappe.db.set_value("ToDo", todo2, "date", None, update_modified=False),
			"set_value date": lambda: frappe.db.set_value(
				"ToDo", todo2, "date", "2025-05-05", update_modified=False
			),
			"set_value Link case": lambda: frappe.db.set_value(
				"ToDo", todo2, "allocated_to", users[1].upper(), update_modified=False
			),
			"set_value int": lambda: frappe.db.set_value("ToDo", todo2, "idx", 7, update_modified=False),
			"bulk_update": lambda: frappe.db.bulk_update(
				"ToDo",
				{todo: {"status": "Open", "priority": "Medium"}, todo2: {"status": "Closed"}},
				update_modified=False,
			),
			"delete by filters": lambda: frappe.db.delete("ToDo", {"status": "Cancelled"}),
			"delete like": lambda: frappe.db.delete("Has Role", {"parent": ["like", "p17u0%"]}),
			"delete in": lambda: frappe.db.delete("Has Role", {"parent": ["in", users]}),
			"delete all": lambda: frappe.db.delete("Has Role"),
			"delete by name case": lambda: frappe.db.delete("ToDo", {"name": todo.upper()}),
			"truncate": lambda: frappe.db.truncate("Has Role"),
		}
		for label, step in steps.items():
			if label.startswith("delete") or label == "truncate" or True:
				self._reset_if_needed()
			ok_m = self._run_write(step, surreal=False)
			ok_s = self._run_write(step, surreal=True)
			sm = self._state(False)
			ss = self._state(True)
			if ok_m[0] != ok_s[0] and not (ok_m[0] == "error" and ok_s[0] == "error"):
				self.results[f"write {label}"] = f"MariaDB {ok_m} | SurrealDB {ok_s}"
			elif sm != ss:
				self.results[f"write {label}"] = (
					f"MISMATCH {self._diff(sm['todo'], ss['todo']) if sm['todo'] != ss['todo'] else self._diff(sm['role'], ss['role'])}"
				)
			else:
				self.results[f"write {label}"] = "ok"
		self.assert_all_ok("write")

	def _restore_maria(self):
		frappe.db.rollback()
		cls = self.__class__
		for doctype in ("ToDo", "Has Role"):
			frappe.db.sql(f"delete from `tab{doctype}`")
			for row in cls.rows[doctype]:
				cols = list(row)
				frappe.db.sql(
					f"insert into `tab{doctype}` ({', '.join(f'`{c}`' for c in cols)}) values ({', '.join(['%s'] * len(cols))})",
					tuple(row[c] for c in cols),
				)
		frappe.db.commit()

	def _reset_if_needed(self):
		"""Restore both engines to the seeded state (MariaDB by re-inserting the cloned rows, SurrealDB by re-cloning)."""
		cls = self.__class__
		self._restore_maria()
		with cls.on_surreal():
			for doctype in ("ToDo", "Has Role"):
				cls.sdb.sql(f"DELETE `tab{doctype}` RETURN NONE")
		cls.sdb.commit()
		S.clear_schema_cache()
		cls._copy_rows(("ToDo", "Has Role"))

	def _run_write(self, step, surreal: bool):
		try:
			if surreal:
				with self.on_surreal():
					step()
					self.sdb.commit()
			else:
				step()
				frappe.db.commit()
			return ("ok", None)
		except SurrealDBNotImplementedError as e:
			with contextlib.suppress(Exception):
				self.sdb.rollback()
			return ("unsupported", str(e)[:120])
		except Exception as e:
			with contextlib.suppress(Exception):
				self.sdb.rollback()
			frappe.db.rollback()
			return ("error", f"{type(e).__name__}: {str(e)[:100]}")

	def _state(self, surreal: bool):
		if surreal:
			with self.on_surreal():
				return canon(self.state())
		return canon(self.state())
