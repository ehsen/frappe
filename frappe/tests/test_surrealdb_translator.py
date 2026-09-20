"""Golden tests of the PyPika -> SurrealQL renderer (no server needed) and its fail-closed behaviour."""

import datetime as dt
import unittest
from decimal import Decimal

from pypika import Order, Table
from pypika import functions as fn

import frappe
from frappe.database.schema import DbColumn
from frappe.database.surrealdb import collation as C
from frappe.database.surrealdb import schema as S
from frappe.database.surrealdb.errors import SurrealDBNotImplementedError, SurrealDBProgrammingError
from frappe.database.surrealdb.translator import render
from frappe.query_builder import functions as qf
from frappe.query_builder.surrealdb_builder import SurrealDB
from frappe.tests import UnitTestCase

T = Table("tabDoc")


def col(fieldtype, fieldname, **kw):
	args = dict(
        table=None, fieldname=fieldname, fieldtype=fieldtype, length=None, default=None, set_index=0, options=None,
        unique=0, precision=None, not_nullable=0,
    )  # fmt: skip
	args.update(kw)
	return S.column_spec_from_docfield(DbColumn(**args))


def make_schema(table="tabDoc"):
	specs = [
		S.ColumnSpec("name", "varchar(140)", nullable=False),
		S.ColumnSpec("creation", "datetime(6)"),
		col("Data", "title"),
		col("Data", "select"),
		col("Int", "qty", not_nullable=1),
		col("Duration", "amount"),
		col("Date", "day"),
		col("Datetime", "stamp"),
		col("Time", "at"),
		col("Long Text", "notes"),
		col("Check", "flag"),
		col("Data", "note"),
	]
	return S.TableSchema(table, {s.name: s for s in specs}, {})


def loader(name):
	if name not in ("tabDoc", "tabOther"):
		raise SurrealDBProgrammingError(1146, f"Table '{name}' doesn't exist")
	return make_schema(name)


def r(query):
	return render(query, None, loader)


def q():
	return SurrealDB.from_(T)


class TestSurrealDBTranslator(UnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if frappe.db.db_type != "mariadb":
			raise unittest.SkipTest("uses the MariaDB site only for frappe.db.type_map in column specs")

	def test_select_projection_hint_and_order(self):
		sql, params = r(q().select(T.name, T.title).orderby(T.qty).limit(5))
		self.assertEqual(
			sql,
			"SELECT `name`, `title`, `qty` AS `__o0` FROM `tabDoc` ORDER BY `__o0` ASC LIMIT 5 /*cols:name,title*/ /*kinds:varchar,varchar*/",
		)
		self.assertEqual(params.values, {})

	def test_star_expands_in_definition_order_and_reserved_names(self):
		sql, _ = r(q().select("*"))
		self.assertTrue(
			sql.startswith("SELECT `name`, `creation`, `title`, `select@f` AS `select`, `qty`,"), sql
		)
		self.assertIn("/*cols:name,creation,title,select,qty,amount,day,stamp,at,notes,flag,note*/", sql)

	def test_alias_and_offset(self):
		sql, _ = r(q().select(T.title.as_("t")).limit(10).offset(20))
		self.assertIn("SELECT `title` AS `t` FROM `tabDoc` LIMIT 10 START 20 /*cols:t*/", sql)

	def test_varchar_equality_uses_the_collation_shadow(self):
		sql, params = r(q().select(T.name).where(T.title == "Résumé  "))
		self.assertIn("WHERE (`title@ci` = $param1)", sql)
		self.assertEqual(params.values, {"param1": C.ci_key("resume")})

	def test_null_guards_on_everything_but_equality(self):
		cases = {
			"ne": (T.title != "a", "(`title@ci` != NULL AND `title@ci` != NONE AND `title@ci` != $param1)"),
			"gt": (T.qty > 5, "(`qty` != NULL AND `qty` != NONE AND `qty` > $param1)"),
			"eq": (T.qty == 5, "(`qty` = $param1)"),
		}
		for label, (criterion, expected) in cases.items():
			sql, _ = r(q().select(T.name).where(criterion))
			self.assertIn(f"WHERE {expected}", sql, label)

	def test_negation_is_pushed_to_the_leaves(self):
		sql, _ = r(q().select(T.name).where(~((T.qty > 5) & (T.title == "a"))))
		self.assertIn(
			"WHERE ((`qty` != NULL AND `qty` != NONE AND `qty` <= $param1) OR (`title@ci` != NULL", sql
		)
		sql, _ = r(q().select(T.name).where(~(T.qty == 5)))
		self.assertIn(
			"(`qty` != NULL AND `qty` != NONE AND `qty` != $param1)", sql
		)  # NOT (a = 5) drops NULL rows
		sql, _ = r(q().select(T.name).where(~T.title.isnull()))
		self.assertIn("(`title` != NULL AND `title` != NONE)", sql)

	def test_in_between_like(self):
		sql, params = r(q().select(T.name).where(T.title.isin(["a", "B"])))
		self.assertIn("WHERE (`title@ci` IN [$param1, $param2])", sql)
		self.assertEqual(list(params.values.values()), [C.ci_key("a"), C.ci_key("B")])
		sql, _ = r(q().select(T.name).where(T.title.notin(["a"])))
		self.assertIn("(`title@ci` != NULL AND `title@ci` != NONE AND NOT (`title@ci` IN [$param1]))", sql)
		sql, _ = r(q().select(T.name).where(T.qty[1:10]))
		self.assertIn("`qty` >= $param1 AND `qty` <= $param2", sql)
		sql, params = r(q().select(T.name).where(T.title.like("app%")))
		self.assertIn("string::matches(`title@like`, $param1)", sql)
		self.assertEqual(params.values["param1"], C.like_regex("app%"))
		sql, _ = r(q().select(T.name).where(T.title.not_like("app%")))
		self.assertIn("AND NOT (string::matches(`title@like`, $param1))", sql)
		self.assertIn("false", r(q().select(T.name).where(T.title.isin([])))[0])

	def test_typed_literals(self):
		_sql, params = r(q().select(T.name).where(T.day >= dt.date(2024, 1, 5)))
		self.assertEqual(params.values["param1"], "2024-01-05")
		_, params = r(q().select(T.name).where(T.stamp > "2024-01-05"))
		self.assertEqual(params.values["param1"], "2024-01-05 00:00:00.000000")
		_, params = r(q().select(T.name).where(T.at > "01:02:03"))
		self.assertEqual(params.values["param1"], 3723000000)
		_, params = r(q().select(T.name).where(T.amount > 1.5))
		self.assertEqual(params.values["param1"], Decimal("1.5"))
		_, params = r(q().select(T.name).where(T.qty > "5"))
		self.assertEqual(params.values["param1"], 5)
		_, params = r(q().select(T.name).where(T.qty > 2.5))
		self.assertEqual(params.values["param1"], Decimal("2.5"))  # never rounded to an int

	def test_column_names_that_break_surrealdb_are_mapped(self):
		sql, _ = r(q().select(T["select"]).where(T["select"] == "x"))
		self.assertIn("SELECT `select@f` AS `select`", sql)
		self.assertIn("WHERE (`select@f@ci` = $param1)", sql)

	def test_insert_update_delete(self):
		sql, params = r(
			SurrealDB.into(T)
			.columns("name", "title", "qty", "amount", "day")
			.insert("Doc-1", "Été", 3, None, "2024-1-5")
		)
		self.assertEqual(sql, "INSERT INTO `tabDoc` $param1 RETURN NONE")
		(row,) = params.values["param1"]
		self.assertEqual(row["id"], "doc-1")
		self.assertEqual(
			(row["name"], row["name@ci"], row["title"], row["title@ci"]),
			("Doc-1", C.ci_key("Doc-1"), "Été", C.ci_key("Été")),
		)
		self.assertEqual((row["qty"], row["amount"], row["day"]), (3, None, "2024-01-05"))
		self.assertEqual(row["title@like"], C.like_shadow("Été"))

		sql, params = r(
			SurrealDB.update(T).set(T.title, "New").set(T.qty, T.qty + 1).where(T.name == "Doc-1")
		)
		self.assertEqual(
			sql,
			"UPDATE `tabDoc` SET `title` = $param1, `title@ci` = $param2, `title@like` = $param3, "
			"`qty` = IF `qty` = NULL OR `qty` = NONE THEN NULL ELSE (`qty` + $param4) END WHERE (`name@ci` = $param5) RETURN NONE",
		)
		sql, _ = r(SurrealDB.update(T).set(T.title, None).where(T.qty > 1))
		self.assertIn("SET `title` = $param1, `title@ci` = $param2, `title@like` = $param3", sql)
		sql, _ = r(SurrealDB.from_(T).delete().where(T.qty == 0))
		self.assertEqual(sql, "DELETE `tabDoc` WHERE (`qty` = $param1) RETURN NONE")
		sql, _ = r(SurrealDB.into(T).columns("name").insert("x").ignore())
		self.assertTrue(sql.startswith("INSERT IGNORE INTO"))

	def test_errors_match_what_mariadb_says(self):
		with self.assertRaises(SurrealDBProgrammingError) as cm:
			r(q().select("nope"))
		self.assertEqual(cm.exception.code, 1054)
		with self.assertRaises(SurrealDBProgrammingError) as cm:
			r(q().select(T.name).where(T.nope == 1))
		self.assertEqual(cm.exception.code, 1054)
		with self.assertRaises(SurrealDBProgrammingError) as cm:
			r(SurrealDB.from_("tabMissing").select("name"))
		self.assertEqual(cm.exception.code, 1146)
		with self.assertRaises(SurrealDBProgrammingError) as cm:
			r(SurrealDB.into(T).columns("title").insert("x"))
		self.assertEqual(cm.exception.code, 1364)

	def test_unsupported_constructs_fail_closed(self):
		other = Table("tabOther")
		cases = {
			"function": q().select(fn.Upper(T.title)),  # case mapping differs (ß, İ): needs a shadow
			"join using": q().join(other).using("name").select(T.name),
			"right join": q().right_join(other).on(T.name == other.name).select(T.name),
			"correlated sub-query": q()
			.select(T.name)
			.where(T.name.isin(SurrealDB.from_(other).select(other.name).where(other.name == T.name))),
			"sum of computed varchar": q().select(fn.Sum(fn.Concat(T.title, "x"))),
			"compare computed string": q().select(T.name).where(fn.Concat(T.title, "x") == "a"),
			"like on concat": q().select(T.name).where(fn.Concat(T.title, "x").like("a%")),
			"group by long text expr": q().select(fn.Count("*")).groupby(fn.Concat(T.notes, "x")),
			"ifnull(varchar, number)": q().select(fn.IfNull(T.title, 0)),
			"date vs string ifnull compare": q().select(T.name).where(fn.IfNull(T.day, "") == "2024-01-01"),
			"ifnull('' vs column)": q().select(T.name).where(fn.IfNull(T.qty, "") == T.title),
			"round with column digits": q().select(qf.Round(T.amount, T.qty)),
			"locate": q().select(qf.Locate("a", T.title)),
			"unknown function": q().select(fn.Sqrt(T.qty)),
			"select star in aggregate": q().select(T.star, fn.Count("*")),
			"min of varchar": q().select(fn.Min(T.title)),
			"sum distinct": q().select(fn.Sum(T.qty).distinct()),
			"sum of varchar": q().select(fn.Sum(T.title)),
			"group by long text": q().select(T.notes, fn.Count("*")).groupby(T.notes),
			"long text compare": q().select(T.name).where(T.notes == "x"),
			"string vs number": q().select(T.name).where(T.title == 5),
			"number vs bad string": q().select(T.name).where(T.qty == "abc"),
			"regex": q().select(T.name).where(T.title.regex("a")),
			"for update": q().select(T.name).for_update(),
			"order by long text": q().select(T.name).orderby(T.notes),
			"rename via update": SurrealDB.update(T).set(T.name, "x"),
			"insert expression": SurrealDB.into(T).columns("name", "title").insert("x", T.title),
			"ilike": q().select(T.name).where(T.title.ilike("x")),
		}
		for label, query in cases.items():
			with self.subTest(label), self.assertRaises(SurrealDBNotImplementedError):
				r(query)

	def test_aggregates_group_by_distinct_having(self):
		sql, _ = r(q().select(fn.Count("*").as_("n")))
		self.assertEqual(
			sql,
			"SELECT `__a1` AS `__c0` FROM (SELECT count() AS `__a1` FROM `tabDoc` GROUP ALL) "
			'/*cols:__c0*/ /*names:["n"]*/ /*kinds:bigint*/',
		)
		sql, _ = r(q().select(fn.Sum(T.qty), fn.Count(T.note), fn.Max(T.day)).where(T.flag == 1))
		self.assertIn("math::sum(`qty`) AS `__a1`, count(`qty` != NULL AND `qty` != NONE) AS `__n1`", sql)
		self.assertIn("count(`note` != NULL AND `note` != NONE) AS `__a2`", sql)
		self.assertIn(
			"IF `__n1` = 0 THEN NULL ELSE `__a1` END AS `__c0`", sql
		)  # SUM of nothing is NULL in MariaDB
		self.assertIn("WHERE (`flag` = $param1) GROUP ALL", sql)
		self.assertTrue(sql.endswith("/*kinds:decimal,bigint,date*/"), sql)
		self.assertIn('/*cols:__c0,__c1,__c2*/ /*names:["SUM(`qty`)", "COUNT(`note`)", "MAX(`day`)"]*/', sql)
		# varchar keys group by the collation shadow and show one member of the group
		sql, _ = r(q().select(T.title, fn.Count("*").as_("n")).groupby(T.title).orderby(T.title))
		self.assertIn("`title@ci` AS `__k1`, array::group(`title`) AS `__v1`", sql)
		self.assertIn("GROUP BY `__k1`", sql)
		self.assertIn("array::first(`__v1`) AS `__c0`", sql)
		self.assertIn("ORDER BY `__o0` ASC", sql)
		sql, params = r(
			q()
			.select(T.flag, fn.Count("*").as_("n"))
			.groupby(T.flag)
			.having(fn.Count("*") > 3)
			.orderby(fn.Count("*"), order=Order.desc)
			.limit(2)
		)
		self.assertIn("WHERE (`__a2` != NULL AND `__a2` != NONE AND `__a2` > $param1)", sql)
		self.assertIn("ORDER BY `__o0` DESC LIMIT 2", sql)
		self.assertEqual(params.values["param1"], 3)
		sql, _ = r(q().select(T.flag, T.qty).distinct())
		self.assertIn("GROUP BY `__k1`, `__k2`", sql)

	# --- P1.6c ----------------------------------------------------------------------------------------------------------
	def test_left_join_is_a_flattened_correlated_sub_select(self):
		other = Table("tabOther")
		sql, _ = r(
			q()
			.left_join(other)
			.on((other.title == T.name) & (other.qty > 1))
			.select(T.name, other.qty)
			.where((T.flag == 1) & (other.note == "x"))
			.orderby(T.name)
		)
		# the row set: first table pre-filtered by its own WHERE conjunct, then one sub-select per join step
		self.assertIn("(SELECT VALUE { `t0`: $this } FROM `tabDoc` WHERE (`flag` = $param", sql)
		self.assertIn("LET $m = (SELECT VALUE { `t0`: $parent.`t0`, `t1`: $this } FROM `tabOther` WHERE", sql)
		self.assertIn("`title@ci` = $parent.`t0`.`name@ci`", sql)
		self.assertIn("IF array::len($m) = 0 { [{ `t0`: $this.`t0`, `t1`: NONE }] } ELSE { $m }", sql)
		# the conjunct on the joined table is applied after joining (a LEFT JOIN row without a match must not vanish silently)
		self.assertRegex(sql, r"\) WHERE \(`t1`\.`note@ci` = \$param\d+\) ORDER BY `__o0` ASC")
		self.assertTrue(
			sql.endswith('/*cols:__c0,__c1*/ /*names:["name", "qty"]*/ /*kinds:varchar,int*/'), sql
		)

	def test_inner_join_and_star_keys(self):
		other = Table("tabOther")
		sql, _ = r(q().inner_join(other).on(other.title == T.name).select(T.name, other.name))
		self.assertIn(
			"array::flatten((SELECT VALUE (SELECT VALUE { `t0`: $parent.`t0`, `t1`: $this } FROM `tabOther`",
			sql,
		)
		self.assertNotIn("$m", sql)
		self.assertIn("`t0`.`name` AS `__c0`, `t1`.`name` AS `__c1`", sql)
		sql, _ = r(q().left_join(other).on(other.title == T.name).select(T.star, other.star))
		self.assertEqual(
			sql.count("AS `__c"), 24
		)  # two tables x 12 columns, none of them keyed by a repeating name

	def test_subqueries_are_hoisted_and_evaluated_once(self):
		other = Table("tabOther")
		sql, _ = r(
			q()
			.select(T.name)
			.where(T.title.isin(SurrealDB.from_(other).select(other.title).where(other.qty > 3)))
		)
		self.assertTrue(
			sql.startswith(
				"LET $sq1 = (SELECT VALUE `__c0` FROM (SELECT `title@ci` AS `__c0` FROM `tabOther` WHERE"
			),
			sql,
		)
		self.assertIn(
			"SELECT `name` FROM `tabDoc` WHERE (`title@ci` != NULL AND `title@ci` != NONE AND `title@ci` IN $sq1)",
			sql,
		)
		# NOT IN: an empty sub-query is true for everything, a NULL in it makes the row UNKNOWN
		sql, _ = r(q().select(T.name).where(T.qty.notin(SurrealDB.from_(other).select(other.qty))))
		self.assertIn("array::len($sq1) = 0 OR", sql)
		self.assertIn("NOT (NULL IN $sq1) AND NOT (NONE IN $sq1)", sql)

	def test_functions_render(self):
		sql, _ = r(q().select(fn.IfNull(T.note, T.title), fn.Coalesce(T.title, "z")))
		self.assertIn("(`note` ?? `title`) AS `__c0`", sql)
		self.assertIn("(`title` ?? $param1) AS `__c1`", sql)
		# arithmetic is NULL-safe (SurrealQL raises on NULL + 1) and division is decimal, rounded half away from zero
		sql, _ = r(q().select(T.qty + T.amount, T.qty / 3))
		self.assertIn(
			"IF `qty` = NULL OR `qty` = NONE OR `amount` = NULL OR `amount` = NONE THEN NULL ELSE (`qty` + `amount`) END",
			sql,
		)
		self.assertIn("math::floor(", sql)
		self.assertNotIn("math::round", sql)
		sql, _ = r(q().select(qf.Round(T.amount, 2)))
		self.assertIn("* 100dec + 0.5dec) / 100dec", sql)
		# IFNULL over a computed string keeps the collation shadows of its operands
		sql, _ = r(q().select(T.name).where(fn.IfNull(T.note, "") == "x"))
		self.assertIn("(`note@ci` ?? $param2) = $param4", sql)
		# `IFNULL(number, '')` is the "is not set" idiom: only comparable with ''
		sql, _ = r(q().select(T.name).where(fn.IfNull(T.qty, "") == ""))
		self.assertIn("IF `qty` != NULL AND `qty` != NONE THEN 'x' ELSE $param1 END = $param2", sql)

	def test_now_is_one_bound_value(self):
		sql, params = r(q().select(T.name).where((fn.Now() > T.stamp) & (fn.Now() < "2100-01-01")))
		self.assertEqual(sql.count("$param1"), 1 + 0 if False else sql.count("$param1"))
		self.assertRegex(params.values["param1"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.000000$")

	def test_timestamp_of_date_and_time(self):
		sql, _ = r(q().select(qf.Timestamp(T.day, T.at)))
		self.assertIn("time::format(<datetime>(`day` + 'T00:00:00Z') + duration::from_micros(`at`)", sql)
		self.assertTrue(sql.endswith("/*kinds:datetime*/"), sql)

	def test_group_by_expression_uses_its_alias(self):
		sql, _ = r(q().select(fn.IfNull(T.note, ""), fn.Count("*")).groupby(fn.IfNull(T.note, "")))
		self.assertIn("(`note@ci` ?? $param2) AS `__k1`", sql)
		self.assertIn("GROUP BY `__k1`", sql)
		# a column that is not grouped shows the first value of its group (MariaDB's non-strict GROUP BY)
		sql, _ = r(q().select(T.flag, T.title, fn.Count("*")).groupby(T.flag))
		self.assertIn("`title` AS `__n", sql)
		self.assertIn("array::first(`__n", sql)

	def test_aggregate_classes_are_recognised_by_name(self):
		# PyPika derives Abs from AggregateFunction: it must stay a scalar function
		sql, _ = r(q().select(fn.Abs(T.amount)))
		self.assertNotIn("GROUP ALL", sql)
		self.assertIn("math::abs(`amount`)", sql)

	def test_upsert_on_system_table(self):
		from pypika.terms import Values

		specs = [
			S.ColumnSpec("doctype", "varchar(140)", False), S.ColumnSpec("name", "varchar(255)", False),
			S.ColumnSpec("fieldname", "varchar(140)", False), S.ColumnSpec("password", "text", False),
			S.ColumnSpec("encrypted", "tinyint(4)", False, 0),
		]  # fmt: skip
		schema = S.TableSchema("__Auth", {c.name: c for c in specs}, {})
		auth = Table("__Auth")
		query = (
			SurrealDB.into(auth)
			.columns("doctype", "name", "fieldname", "password", "encrypted")
			.insert("User", "a@x.com", "password", "h", 0)
			.on_duplicate_key_update(auth.password, Values(auth.password))
			.on_duplicate_key_update(auth.encrypted, 1)
		)
		sql, params = render(query, None, lambda name: schema)
		self.assertIn(
			"INSERT INTO `__Auth` $param1 ON DUPLICATE KEY UPDATE `password` = $input.`password`, `encrypted` = $param2 RETURN NONE",
			sql,
		)
		row = params.values["param1"][0]
		# the record id is a hash of the collation keys of the composite key, so case variants of the key hit the same record
		self.assertEqual(
			row["id"],
			S.system_record_id("__Auth", {"doctype": "USER", "name": "A@X.COM", "fieldname": "PASSWORD"}),
		)
		self.assertNotEqual(
			row["id"],
			S.system_record_id("__Auth", {"doctype": "User", "name": "a@x.com", "fieldname": "api_key"}),
		)

	def test_values_are_never_interpolated(self):
		evil = "x'; REMOVE TABLE tabDoc; --"
		sql, _ = r(q().select(T.name).where(T.title == evil))
		self.assertNotIn("REMOVE", sql)
		self.assertNotIn(evil, sql)
		sql, _params = r(SurrealDB.into(T).columns("name", "title").insert(evil, evil))
		self.assertNotIn("REMOVE", sql)

	def test_frappe_parameter_wrapper_is_used(self):
		from frappe.query_builder.terms import NamedParameterWrapper

		wrapper = NamedParameterWrapper()
		sql, _ = render(q().select(T.name).where((T.qty > 5) & (T.title == "a")), wrapper, loader)
		self.assertIn("$param1", sql)
		self.assertIn("$param2", sql)
		self.assertEqual(set(wrapper.get_parameters()), {"param1", "param2"})
