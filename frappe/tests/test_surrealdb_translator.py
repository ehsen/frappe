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


def make_schema():
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
	return S.TableSchema("tabDoc", {s.name: s for s in specs}, {})


def loader(name):
	if name != "tabDoc":
		raise SurrealDBProgrammingError(1146, f"Table '{name}' doesn't exist")
	return make_schema()


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
			"`qty` = IF `qty` = NULL OR `qty` = NONE THEN NULL ELSE `qty` + $param4 END WHERE (`name@ci` = $param5) RETURN NONE",
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
			"join": q().join(other).on(T.name == other.name).select(T.name),
			"function": q().select(fn.Upper(T.title)),
			"avg": q().select(fn.Avg(T.qty)),
			"sum of varchar": q().select(fn.Sum(T.title)),
			"min of varchar": q().select(fn.Min(T.title)),
			"non-grouped column": q().select(T.title, fn.Count("*")),
			"having against a column": q().select(fn.Count("*")).having(fn.Count("*") > T.qty),
			"group by long text": q().select(T.notes, fn.Count("*")).groupby(T.notes),
			"order by non-grouped": q().select(T.flag, fn.Count("*")).groupby(T.flag).orderby(T.title),
			"subquery in": q().select(T.name).where(T.name.isin(SurrealDB.from_(other).select(other.name))),
			"long text compare": q().select(T.name).where(T.notes == "x"),
			"string vs number": q().select(T.name).where(T.title == 5),
			"number vs bad string": q().select(T.name).where(T.qty == "abc"),
			"date vs datetime literal": q().select(T.name).where(T.day == "2024-01-05 10:00:00"),
			"regex": q().select(T.name).where(T.title.regex("a")),
			"for update": q().select(T.name).for_update(),
			"order by long text": q().select(T.name).orderby(T.notes),
			"rename via update": SurrealDB.update(T).set(T.name, "x"),
			"expression set": SurrealDB.update(T).set(T.qty, T.qty * T.flag),
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
			"SELECT `__a0` AS `n` FROM (SELECT count() AS `__a0` FROM `tabDoc` GROUP ALL) /*cols:n*/ /*kinds:int*/",
		)
		sql, _ = r(q().select(fn.Sum(T.qty), fn.Count(T.note), fn.Max(T.day)).where(T.flag == 1))
		self.assertIn("math::sum(`qty`) AS `__a0`, count(`qty` != NULL AND `qty` != NONE) AS `__n0`", sql)
		self.assertIn("count(`note` != NULL AND `note` != NONE) AS `__a1`", sql)
		self.assertIn(
			"IF `__n0` = 0 THEN NULL ELSE `__a0` END AS `sum_0`", sql
		)  # SUM of nothing is NULL in MariaDB
		self.assertIn("WHERE (`flag` = $param1) GROUP ALL", sql)
		self.assertTrue(sql.endswith("/*cols:sum_0,count_1,max_2*/ /*kinds:int,int,date*/"), sql)
		# varchar keys group by the collation shadow and show one member of the group
		sql, _ = r(q().select(T.title, fn.Count("*").as_("n")).groupby(T.title).orderby(T.title))
		self.assertIn("`title@ci` AS `__k0`, array::group(`title`) AS `__v0`", sql)
		self.assertIn("GROUP BY `title@ci`", sql)
		self.assertIn("array::first(`__v0`) AS `title`", sql)
		self.assertIn("ORDER BY `__o0` ASC", sql)
		sql, params = r(
			q()
			.select(T.flag, fn.Count("*").as_("n"))
			.groupby(T.flag)
			.having(fn.Count("*") > 3)
			.orderby(fn.Count("*"), order=Order.desc)
			.limit(2)
		)
		self.assertIn("WHERE (`__a0` != NULL AND `__a0` != NONE AND `__a0` > $param1)", sql)
		self.assertIn("ORDER BY `__o0` DESC LIMIT 2", sql)
		self.assertEqual(params.values["param1"], 3)
		sql, _ = r(q().select(T.flag, T.qty).distinct())
		self.assertIn("GROUP BY `flag`, `qty`", sql)

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
