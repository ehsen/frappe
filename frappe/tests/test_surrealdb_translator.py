"""Golden tests of the PyPika -> SurrealQL renderer (no server needed) and its fail-closed behaviour."""

import datetime as dt
import unittest
from decimal import Decimal

from pypika import Order, Table
from pypika import functions as fn
from pypika.functions import Function
from pypika.terms import ExistsCriterion as Exists, Tuple, ValueWrapper

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
	if table == "tabDocType":
		specs = [
			S.ColumnSpec("name", "varchar(140)", nullable=False),
			col("Check", "issingle"),
			col("Int", "is_virtual"),
		]
	elif table == "tabCustom Field":
		specs = [
			S.ColumnSpec("name", "varchar(140)", nullable=False),
			col("Data", "dt"),
			col("Data", "fieldname"),
			col("Data", "options"),
			col("Check", "is_virtual"),
		]
	else:
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
	if name not in ("tabDoc", "tabOther", "tabDocType", "tabCustom Field"):
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

	def test_text_columns_compare_case_insensitively_inline(self):
		# Revised P1.6d contract: text-kind columns (including Long Text) compare with an inline
		# `string::lowercase()` on both sides - upstream test_db compares them directly. The stored
		# side is NULL-guarded: SurrealDB's string::lowercase(NULL) raises while MariaDB yields NULL.
		sql, params = r(q().select(T.name).where(T.notes == "X"))
		self.assertIn(
			"WHERE (IF `notes` = NULL OR `notes` = NONE THEN NULL ELSE (string::lowercase(`notes`)) END "
			"= string::lowercase($param1))",
			sql,
		)
		self.assertEqual(params.values, {"param1": "X"})

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
		# `INSERT INTO` always carries the `/*uq:*/` hint: the driver pre-checks the table's unique
		# indexes against the rows (P1.8) before it writes
		self.assertEqual(sql, "INSERT INTO `tabDoc` $param1 /*uq:tabDoc:param1*/ RETURN NONE")
		(row,) = params.values["param1"]
		self.assertEqual(row["id"], "doc-1")
		self.assertEqual(
			(row["name"], row["name@ci"], row["title"], row["title@ci"]),
			("Doc-1", C.ci_key("Doc-1"), "Été", C.ci_key("Été")),
		)
		self.assertEqual((row["qty"], row["amount"], row["day"]), (3, None, "2024-01-05"))
		self.assertEqual(row["title@like"], C.like_shadow("Été"))

	def test_for_update_renders_lock_hints(self):
		# key mode: a `name = <literal>` conjunct locks that one record before the read (single statement)
		sql, params = r(q().select(T.name).where(T.name == "Doc-1").for_update())
		self.assertEqual(
			sql,
			"SELECT `name`, id AS `__lk0` FROM `tabDoc` WHERE (`name@ci` = $param1) "
			"/*cols:name*/ /*kinds:varchar*/ /*lock:l:k:tabDoc:doc-1*/",
		)
		self.assertEqual(params.values["param1"], C.ci_key("Doc-1"))
		sql, _ = r(q().select(T.name).where(T.name == "Doc-1").for_update(nowait=True))
		self.assertTrue(sql.endswith("/*lock:n:k:tabDoc:doc-1*/"), sql)

		# general mode: a two-statement script - candidate ids first, then the same select
		sql, params = r(q().select(T.name, T.title).where(T.qty > 2).orderby(T.qty).limit(5).for_update())
		self.assertEqual(
			sql,
			"SELECT id AS `__lk0`, `qty` AS `__o0` FROM `tabDoc` "
			"WHERE (`qty` != NULL AND `qty` != NONE AND `qty` > $param1) ORDER BY `__o0` ASC LIMIT 5; "
			"SELECT `name`, `title`, `qty` AS `__o0`, id AS `__lk0` FROM `tabDoc` "
			"WHERE (`qty` != NULL AND `qty` != NONE AND `qty` > $param1) ORDER BY `__o0` ASC LIMIT 5 "
			"/*cols:name,title*/ /*kinds:varchar,varchar*/ /*lock:l:t:tabDoc:5*/",
		)
		self.assertEqual(params.values, {"param1": 2})
		sql, _ = r(q().select(T.name).where(T.qty > 2).limit(3).offset(6).for_update(nowait=True))
		self.assertIn("LIMIT 3 START 6", sql)
		self.assertTrue(sql.endswith("/*lock:n:t:tabDoc:3*/"), sql)

		# SKIP LOCKED: the ids statement runs unpaginated, the main select too (the cursor claims the
		# first `LIMIT` unlocked rows itself; documented deviation from MariaDB's limit-fill)
		sql, _ = r(q().select(T.name).where(T.qty > 2).limit(5).for_update(skip_locked=True))
		self.assertEqual(
			sql,
			"SELECT id AS `__lk0` FROM `tabDoc` WHERE (`qty` != NULL AND `qty` != NONE AND `qty` > $param1); "
			"SELECT `name`, id AS `__lk0` FROM `tabDoc` WHERE (`qty` != NULL AND `qty` != NONE AND `qty` > $param1) "
			"/*cols:name*/ /*kinds:varchar*/ /*lock:s:t:tabDoc:5*/",
		)
		sql, _ = r(q().select(T.name).where(T.qty > 2).for_update(skip_locked=True))
		self.assertTrue(sql.endswith("/*lock:s:t:tabDoc:*/"), sql)
		self.assertNotIn("LIMIT", sql)

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
		self.assertNotIn("/*uq:", sql)  # INSERT IGNORE skips rows that already exist on its own

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
			# Revised P1.6d contract (upstream test_db parity): text-kind columns (Small Text / Text /
			# Long / Medium) compare case-insensitively via an inline `string::lowercase()` on both
			# sides, so a long text compare is supported; LIKE / ORDER BY / GROUP BY on them stay refused.
			# patch_text_int_cmp (P2.1) casts an int through its canonical decimal string (`title = 1`), so
			# the remaining string-vs-number boundary is a non-integer number (MariaDB casts the column)
			"string vs non-integer number": q().select(T.name).where(T.title == 5.5),
			"number vs bad string": q().select(T.name).where(T.qty == "abc"),
			"regex": q().select(T.name).where(T.title.regex("a")),
			"for update with group by": q().select(fn.Count("*")).groupby(T.flag).for_update(),
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
	def test_join_step_is_a_closure_over_the_previous_rows(self):
		other = Table("tabOther")
		sql, _ = r(
			q()
			.left_join(other)
			.on((other.title == T.name) & (other.qty > 1))
			.select(T.name, other.qty)
			.where((T.flag == 1) & (other.note == "x"))
			.orderby(T.name)
		)
		# the first table pre-filtered by its own WHERE conjunct; one closure per join step; the closure parameter (not `$parent`,
		# which SurrealDB scans) carries the earlier row so the join column's index is used
		self.assertIn(
			"(array::flatten(array::map((SELECT VALUE { `t0`: $this } FROM `tabDoc` WHERE (`flag` = $param1)), |$r| {",
			sql,
		)
		self.assertIn("LET $m = (SELECT VALUE { `t0`: $r.`t0`, `t1`: $this } FROM `tabOther` WHERE", sql)
		self.assertIn("`title@ci` = $r.`t0`.`name@ci`", sql)
		self.assertNotIn("$parent", sql)
		self.assertIn("IF array::len($m) = 0 { [{ `t0`: $r.`t0`, `t1`: NONE }] } ELSE { $m }", sql)
		# the conjunct on the joined table is applied after joining (a LEFT JOIN row without a match must not vanish silently)
		self.assertRegex(sql, r"\) WHERE \(`t1`\.`note@ci` = \$param\d+\) ORDER BY `__o0` ASC")
		self.assertNotIn(
			"SELECT VALUE id FROM (SELECT id", sql
		)  # a WHERE on the joined table: no page pushdown
		self.assertTrue(
			sql.endswith('/*cols:__c0,__c1*/ /*names:["name", "qty"]*/ /*kinds:varchar,int*/'), sql
		)

	def test_paged_left_join_selects_the_page_ids_first(self):
		other = Table("tabOther")
		sql, _ = r(
			q()
			.left_join(other)
			.on(other.title == T.name)
			.select(T.name, other.qty)
			.where(T.flag == 1)
			.orderby(T.name)
			.limit(20)
			.offset(40)
		)
		self.assertIn(
			"(SELECT VALUE { `t0`: $this } FROM (SELECT VALUE id FROM (SELECT id, `name@ci` AS `__p0` FROM `tabDoc` "
			"WHERE (`flag` = $param1) ORDER BY `__p0` ASC LIMIT 60)))",
			sql,
		)
		self.assertIn(
			"ORDER BY `__o0` ASC LIMIT 20 START 40", sql
		)  # the final page is still cut from the joined rows

	def test_inner_join_and_star_keys(self):
		other = Table("tabOther")
		sql, _ = r(q().inner_join(other).on(other.title == T.name).select(T.name, other.name))
		self.assertIn("|$r| (SELECT VALUE { `t0`: $r.`t0`, `t1`: $this } FROM `tabOther`", sql)
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

	def test_uncorrelated_scalar_subquery_in_projection_is_hoisted(self):
		dt = Table("tabDocType")
		sql, _ = r(SurrealDB.from_(dt).select(SurrealDB.from_(Table("tabOther")).select(fn.Count("*"))))
		self.assertTrue(sql.startswith("LET $sq1 = (SELECT VALUE `__c0` FROM ("), sql)
		self.assertIn(
			"SELECT IF array::len($sq1) = 0 THEN 0 ELSE array::first($sq1) END AS `__c0` FROM `tabDocType`", sql
		)

	def test_correlated_scalar_subquery_in_projection(self):
		# `get_link_fields`'s shape: the sub-query reads `cf.dt`, a column of the outer row (chunk P1.6c)
		dt, cf = Table("tabDocType"), Table("tabCustom Field")
		issingle = SurrealDB.from_(dt).select(dt.issingle).where(dt.name == cf.dt)
		sql, params = r(
			SurrealDB.from_(cf)
			.select(cf.dt.as_("parent"), cf.fieldname, issingle.as_("issingle"))
			.where(cf.options == "Note")
		)
		self.assertIn(
			"SELECT `dt` AS `parent`, `fieldname`, (array::map([{`k1`: `dt@ci`, `k1l`: `dt@like`}], |$o| "
			"(SELECT VALUE `__c0` FROM (SELECT `issingle` AS `__c0` FROM `tabDocType` WHERE "
			"(`name@ci` != NULL AND `name@ci` != NONE AND $o.`k1` != NULL AND $o.`k1` != NONE "
			"AND `name@ci` = $o.`k1`)))[0]))[0] AS `__c2` FROM `tabCustom Field`",
			sql,
		)
		# the outer row's varchar column contributes its collation key, so the match is case-insensitive;
		# the uncorrelated conjunct keeps its bound parameter
		sql, params = r(
			SurrealDB.from_(cf)
			.select(cf.dt, issingle.as_("issingle"))
			.where((cf.options == "Note") & (cf.is_virtual == 0))
		)
		self.assertIn("(array::map([{`k1`: `dt@ci`, `k1l`: `dt@like`}],", sql)
		self.assertIn("WHERE ((`options@ci` = $param1) AND (`is_virtual` = $param2))", sql)
		self.assertEqual(params.values, {"param1": C.ci_key("Note"), "param2": 0})

	def test_correlated_scalar_subquery_binds_two_columns(self):
		dt, cf = Table("tabDocType"), Table("tabCustom Field")
		issingle = SurrealDB.from_(dt).select(dt.issingle).where((dt.name == cf.dt) & (dt.is_virtual == cf.is_virtual))
		sql, _ = r(SurrealDB.from_(cf).select(issingle.as_("issingle")))
		self.assertIn("(array::map([{`k1`: `dt@ci`, `k1l`: `dt@like`, `k2`: `is_virtual`}],", sql)
		self.assertIn("`name@ci` = $o.`k1`", sql)
		self.assertIn("`is_virtual` = $o.`k2`", sql)

	def test_correlated_scalar_subquery_like_over_the_outer_column(self):
		dt, cf = Table("tabDocType"), Table("tabCustom Field")
		issingle = SurrealDB.from_(dt).select(dt.issingle).where(cf.dt.like("a%"))
		sql, _ = r(SurrealDB.from_(cf).select(issingle.as_("issingle")))
		self.assertIn(
			"($o.`k1l` != NULL AND $o.`k1l` != NONE AND string::matches($o.`k1l`, $param1))", sql
		)

	def test_min_max_over_nullable_numbers_skips_null_exactly(self):
		# `math::max` skips NULLs in a mixed group, but errors on a stored NULL and answers -inf/+inf when every
		# value of the group is NULL/NONE; MariaDB answers NULL - so collect, drop and take an end of the sort (P1.6c)
		sql, _ = r(q().select(fn.Min(T.amount), fn.Max(T.amount)))
		self.assertIn("array::group(`amount`) AS `__a1`, array::group(`amount`) AS `__a2`", sql)
		self.assertIn(
			"array::first(array::sort(array::complement(`__a1`, [NULL, NONE]))) AS `__c0`", sql
		)
		self.assertIn(
			"array::last(array::sort(array::complement(`__a2`, [NULL, NONE]))) AS `__c1`", sql
		)

	def test_scalar_count_subquery_is_zero_for_an_empty_set(self):
		# MariaDB answers one row (0) for COUNT over an empty set; SurrealDB's GROUP ALL returns none over no
		# records, so a scalar COUNT sub-query needs the default (P1.6c)
		dt, cf = Table("tabDocType"), Table("tabCustom Field")
		nk = SurrealDB.from_(dt).select(fn.Count("*")).where(dt.name == cf.dt)
		sql, _ = r(SurrealDB.from_(cf).select(cf.dt, nk.as_("nk")))
		self.assertIn(
			"(array::map([{`k1`: `dt@ci`, `k1l`: `dt@like`}], |$o| { LET $sq1 = "
			"(SELECT VALUE `__c0` FROM (SELECT `__a1` AS `__c0` FROM (SELECT count() AS `__a1` FROM `tabDocType` "
			"WHERE (`name@ci` != NULL AND `name@ci` != NONE AND $o.`k1` != NULL AND $o.`k1` != NONE "
			"AND `name@ci` = $o.`k1`) GROUP ALL))); IF array::len($sq1) = 0 { 0 } ELSE { array::first($sq1) } }))[0]",
			sql,
		)
		# uncorrelated: the hoisted `LET` runs once, the default lives in the statement
		other = Table("tabOther")
		sql, _ = r(q().select(SurrealDB.from_(other).select(fn.Count("*")).where(other.qty > 3)))
		self.assertIn(
			"SELECT IF array::len($sq1) = 0 THEN 0 ELSE array::first($sq1) END AS `__c0` FROM `tabDoc`", sql
		)

	def test_correlated_subqueries_still_fail_closed(self):
		dt, cf = Table("tabDocType"), Table("tabCustom Field")
		# a reference to a query more than one level out is not bound (correlated IN/EXISTS are supported
		# since P1.13; see test_correlated_exists_answers_per_row)
		other = Table("tabOther")
		two_out = SurrealDB.from_(dt).select(dt.issingle).where(dt.name == other.name)
		mid = SurrealDB.from_(cf).select(two_out.as_("issingle"))
		with self.assertRaises(SurrealDBNotImplementedError):
			r(SurrealDB.from_(other).select(mid.as_("x")))
		# a correlated sub-query at the outer level of an aggregate query cannot reach the row
		issingle = SurrealDB.from_(dt).select(dt.issingle).where(dt.name == cf.dt)
		with self.assertRaises(SurrealDBNotImplementedError):
			r(SurrealDB.from_(cf).select(cf.dt, issingle.as_("issingle")).groupby(cf.dt))
		# long text has no collation shadow to compare with
		with self.assertRaises(SurrealDBNotImplementedError):
			r(q().select(SurrealDB.from_(dt).select(dt.issingle).where(dt.name == T.notes)))

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

	def test_concat_ws(self):
		# get_user_fullname's idiom: `CONCAT_WS(' ', first_name, last_name)` — a plain string projection
		sql, params = r(q().select(qf.Concat_ws(" ", T.name, T.title).as_("fullname")))
		self.assertEqual(
			sql,
			"SELECT string::concat($param1, IF `name` = NULL OR `name` = NONE THEN '' ELSE `name` END, "
			"IF `title` = NULL OR `title` = NONE THEN '' ELSE `title` END) AS `__c0` FROM `tabDoc` "
			'/*cols:__c0*/ /*names:["fullname"]*/ /*kinds:varchar*/',
		)
		self.assertEqual(params.values, {"param1": " "})
		# MariaDB skips NULL values (SurrealDB's string::concat would print them as 'NULL'), so every value
		# part carries a NULL/NONE guard; ints are cast, dates/times/long text join as their stored text
		sql, params = r(q().select(qf.Concat_ws("-", T.qty, T.day, T.notes, "z", 5)))
		self.assertIn(
			"string::concat($param1, IF `qty` = NULL OR `qty` = NONE THEN '' ELSE <string>`qty` END, "
			"IF `day` = NULL OR `day` = NONE THEN '' ELSE `day` END, "
			"IF `notes` = NULL OR `notes` = NONE THEN '' ELSE `notes` END, $param2, $param3) AS `__c0`",
			sql,
		)
		self.assertEqual(params.values, {"param1": "-", "param2": "z", "param3": "5"})
		# a NULL separator is NULL and `CONCAT_WS(sep)` without values is ''
		self.assertIn("SELECT NULL AS `__c0`", r(q().select(qf.Concat_ws(None, T.title, "x")))[0])
		self.assertIn("SELECT string::concat($param1) AS `__c0`", r(q().select(qf.Concat_ws(",")))[0])
		# fail closed: a column separator and MariaDB's own number formatting are not reproduced
		for query in (q().select(T.name).where(qf.Concat_ws(T.title, T.name) == "a"), q().select(qf.Concat_ws(",", T.amount, "x"))):
			with self.assertRaises(SurrealDBNotImplementedError):
				r(query)

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

	# --- P1.13: the six translation gaps surfaced by P2.1 ------------------------------------------------------------------
	def test_const_left_in_subquery(self):
		# `7 IN (SELECT ..)` - the note.py login check - meets the hoisted sub-query as a bound constant
		other = Table("tabOther")
		sql, params = r(q().select(T.name).where(ValueWrapper(7).isin(SurrealDB.from_(other).select(other.qty))))
		self.assertIn("(SELECT VALUE `__c0` FROM (SELECT `qty` AS `__c0` FROM `tabOther`))", sql)
		self.assertIn("WHERE ($param1 IN $sq1)", sql)
		self.assertEqual(params.values, {"param1": 7})
		# a varchar key meets the constant through its collation key
		sql, params = r(q().select(T.name).where(ValueWrapper("note").isin(SurrealDB.from_(other).select(other.title))))
		self.assertIn("(SELECT `title@ci` AS `__c0` FROM `tabOther`)", sql)
		self.assertIn("WHERE ($param2 IN $sq1)", sql)
		self.assertEqual(params.values["param2"], C.ci_key("note"))
		# NOT IN stays exact: empty is true for everything, a NULL in the list makes it UNKNOWN
		sql, _ = r(q().select(T.name).where(ValueWrapper(7).notin(SurrealDB.from_(other).select(other.qty))))
		self.assertIn(
			"(array::len($sq1) = 0 OR (NOT ($param1 IN $sq1) AND NOT (NULL IN $sq1) AND NOT (NONE IN $sq1)))", sql
		)
		# `NULL IN (..)` is UNKNOWN: never true, and its negation over a non-empty list as well
		sql, _ = r(q().select(T.name).where(ValueWrapper(None).isin(SurrealDB.from_(other).select(other.qty))))
		self.assertIn("WHERE false", sql)

	def test_const_left_in_a_tuple_folds_like_mariadb(self):
		# literal elements fold (strings meet under the *_ci collation); a NULL element is UNKNOWN
		sql, _ = r(q().select(T.name).where(ValueWrapper("b").isin(Tuple("a", "B", "c"))))
		self.assertIn("WHERE true", sql)
		sql, _ = r(q().select(T.name).where(ValueWrapper("b").notin(Tuple("a", "c"))))
		self.assertIn("WHERE true", sql)
		sql, _ = r(q().select(T.name).where(ValueWrapper("b").isin(Tuple("a", None))))
		self.assertIn("WHERE false", sql)
		# an element that is a column is one membership comparison
		sql, params = r(q().select(T.name).where(ValueWrapper("b").isin(Tuple("a", T.title))))
		self.assertIn("WHERE ((`title@ci` = $param1))", sql)
		self.assertEqual(params.values, {"param1": C.ci_key("b")})
		sql, _ = r(q().select(T.name).where(ValueWrapper("b").notin(Tuple("a", T.title))))
		self.assertIn("WHERE ((`title@ci` != NULL AND `title@ci` != NONE AND `title@ci` != $param1))", sql)

	def test_field_function(self):
		# constants fold like MariaDB (case-insensitive strings, numbers coerce strings)
		sql, params = r(q().select(Function("FIELD", "b", "a", "B", "c").as_("rank")))
		self.assertEqual(params.values, {"param1": 2})
		sql, params = r(q().select(Function("FIELD", T.title, "a", "b")))
		self.assertEqual(
			sql,
			"SELECT IF (`title@ci` = $param1) THEN 1 ELSE IF (`title@ci` = $param2) THEN 2 ELSE 0 END AS `__c0` "
			'FROM `tabDoc` /*cols:__c0*/ /*names:["FIELD(`title`,\'a\',\'b\')"]*/ /*kinds:bigint*/',
		)
		self.assertEqual(params.values["param1"], C.ci_key("a"))
		# FIELD in ORDER BY is a number, so it needs no collation shadow
		sql, _ = r(q().select(T.name).orderby(Function("FIELD", T.title, "a", "b")))
		self.assertIn(
			"IF (`title@ci` = $param1) THEN 1 ELSE IF (`title@ci` = $param2) THEN 2 ELSE 0 END AS `__o0` "
			"FROM `tabDoc` ORDER BY `__o0` ASC",
			sql,
		)

	def test_comma_join_becomes_inner_join_steps(self):
		# Frappe's legacy comma join: the cross-table equality conjuncts are the ON of the join steps
		other = Table("tabOther")
		sql, _ = r(SurrealDB.from_(T).from_(other).select(T.name, other.title).where(T.title == other.title))
		self.assertIn(
			"FROM (array::flatten(array::map((SELECT VALUE { `t0`: $this } FROM `tabDoc`), |$r| "
			"(SELECT VALUE { `t0`: $r.`t0`, `t1`: $this } FROM `tabOther` WHERE "
			"($r.`t0`.`title@ci` != NULL AND $r.`t0`.`title@ci` != NONE AND `title@ci` != NULL AND "
			"`title@ci` != NONE AND $r.`t0`.`title@ci` = `title@ci`)))))",
			sql,
		)
		# a conjunct of the first table alone is applied before joining, the rest is consumed
		dt = Table("tabDocType")
		sql, params = r(
			SurrealDB.from_(T)
			.from_(other)
			.from_(dt)
			.select(T.name)
			.where((T.title == other.title) & (other.qty == dt.issingle) & (T.flag == 1))
		)
		self.assertIn("(SELECT VALUE { `t0`: $this } FROM `tabDoc` WHERE (`flag` = $param1))", sql)
		self.assertIn("|$r| (SELECT VALUE { `t0`: $r.`t0`, `t1`: $this } FROM `tabOther`", sql)
		self.assertIn("|$r| (SELECT VALUE { `t0`: $r.`t0`, `t1`: $r.`t1`, `t2`: $this } FROM `tabDocType`", sql)
		self.assertEqual(params.values, {"param1": 1})
		# a table without an equality to connect it, or a comma join mixed with JOIN ... ON, fails closed
		with self.assertRaises(SurrealDBNotImplementedError):
			r(SurrealDB.from_(T).from_(other).from_(dt).select(T.name).where(T.title == other.title))
		with self.assertRaises(SurrealDBNotImplementedError):
			r(SurrealDB.from_(T).from_(other).join(dt).on(T.name == dt.name).select(T.name))

	def test_distinct_with_the_pk_is_a_noop(self):
		# every output row is one record: no dedup, no GROUP BY, no sorting
		sql, _ = r(q().select(T.name).distinct())
		self.assertEqual(sql, "SELECT `name` FROM `tabDoc` /*cols:name*/ /*kinds:varchar*/")
		sql, _ = r(q().select(T.name, T.title).distinct())
		self.assertEqual(sql, "SELECT `name`, `title` FROM `tabDoc` /*cols:name,title*/ /*kinds:varchar,varchar*/")
		sql, _ = r(q().select("*").distinct())
		self.assertIn("SELECT `name`, `creation`, `title`", sql)
		# a projection without the key still deduplicates through GROUP BY
		sql, _ = r(q().select(T.title).distinct())
		self.assertIn("GROUP BY `__k1`", sql)

	def test_order_by_a_select_alias(self):
		# MariaDB resolves ORDER BY names against the projection first (pypika qualifies a string
		# order_by with the FROM table)
		sql, _ = r(q().select(T.name.as_("who")).orderby("who").limit(3))
		self.assertEqual(
			sql,
			"SELECT `name` AS `who`, `name@ci` AS `__o0` FROM `tabDoc` ORDER BY `__o0` ASC LIMIT 3 "
			"/*cols:who*/ /*kinds:varchar*/",
		)
		# the alias of an aggregate in an aggregate query: the outer level orders by the aggregate
		sql, _ = r(q().select(fn.Count(T.name).as_("count")).groupby(T.flag).orderby("count"))
		self.assertIn(
			"SELECT `__a2` AS `__c0`, `__a2` AS `__o0` FROM "
			"(SELECT `flag` AS `__k1`, count(`name` != NULL AND `name` != NONE) AS `__a2` FROM `tabDoc` "
			"GROUP BY `__k1`) ORDER BY `__o0` ASC",
			sql,
		)

	def test_correlated_exists_answers_per_row(self):
		dt, cf = Table("tabDocType"), Table("tabCustom Field")
		issingle = SurrealDB.from_(dt).select(dt.issingle).where(dt.name == cf.dt)
		sql, _ = r(SurrealDB.from_(cf).select(cf.dt).where(Exists(issingle)))
		self.assertIn(
			"(array::map([{`k1`: `dt@ci`, `k1l`: `dt@like`}], |$o| array::len((SELECT VALUE `__c0` FROM "
			"(SELECT `issingle` AS `__c0` FROM `tabDocType` WHERE (`name@ci` != NULL AND `name@ci` != NONE "
			"AND $o.`k1` != NULL AND $o.`k1` != NONE AND `name@ci` = $o.`k1`))))))[0] > 0",
			sql,
		)
		sql, _ = r(SurrealDB.from_(cf).select(cf.dt).where(~Exists(issingle)))
		self.assertIn("))))[0] = 0", sql)

	def test_correlated_in_answers_per_row(self):
		dt = Table("tabDocType")
		linked = SurrealDB.from_(dt).select(dt.issingle).where(dt.is_virtual == T.qty)
		sql, _ = r(q().select(T.name).where(T.qty.isin(linked)))
		self.assertIn(
			"(array::map([{`k1`: `qty`, `k2`: `qty`}], |$o| ($o.`k2` != NULL AND $o.`k2` != NONE AND "
			"$o.`k2` IN (SELECT VALUE `__c0` FROM (SELECT `issingle` AS `__c0` FROM `tabDocType` WHERE "
			"(`is_virtual` != NULL AND `is_virtual` != NONE AND $o.`k1` != NULL AND $o.`k1` != NONE "
			"AND `is_virtual` = $o.`k1`))))))[0]",
			sql,
		)
		# a varchar left operand is carried with its collation key, so the match is case-insensitive
		cf = Table("tabCustom Field")
		sql, _ = r(
			q().select(T.name).where(T.title.isin(SurrealDB.from_(cf).select(cf.fieldname).where(cf.dt == T.title)))
		)
		self.assertIn(
			"[{`k1`: `title@ci`, `k1l`: `title@like`, `k2`: `title@ci`, `k2l`: `title@like`}], |$o| "
			"($o.`k2` != NULL AND $o.`k2` != NONE AND $o.`k2` IN (SELECT VALUE `__c0` FROM "
			"(SELECT `fieldname@ci` AS `__c0`",
			sql,
		)
		# a constant needle is bound and needs no binding of its own
		sql, params = r(
			q()
			.select(T.name)
			.where(ValueWrapper(3).isin(SurrealDB.from_(dt).select(dt.issingle).where(dt.is_virtual == T.qty)))
		)
		self.assertIn("(array::map([{`k1`: `qty`}], |$o| ($param1 IN (SELECT VALUE `__c0` FROM", sql)
		self.assertEqual(params.values, {"param1": 3})

	def test_varchar_column_meets_an_int_like_mariadb(self):
		# patch_text_int_cmp (P2.1, official-run blocker #3): the Property Setter's `value = 1` shape
		sql, params = r(q().select(T.name).where(T.title == 5))
		self.assertEqual(
			sql, "SELECT `name` FROM `tabDoc` WHERE (`title@ci` = $param1) /*cols:name*/ /*kinds:varchar*/"
		)
		self.assertEqual(params.values, {"param1": C.ci_key("5")})

	def test_frappe_parameter_wrapper_is_used(self):
		from frappe.query_builder.terms import NamedParameterWrapper

		wrapper = NamedParameterWrapper()
		sql, _ = render(q().select(T.name).where((T.qty > 5) & (T.title == "a")), wrapper, loader)
		self.assertIn("$param1", sql)
		self.assertIn("$param2", sql)
		self.assertEqual(set(wrapper.get_parameters()), {"param1", "param2"})
