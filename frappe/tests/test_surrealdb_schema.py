"""Schema generation, checked against what MariaDB's own DbColumn / type_map decide (no SurrealDB server needed)."""

import itertools
import unittest

import frappe
from frappe.database.schema import DbColumn
from frappe.database.surrealdb import schema as S
from frappe.database.surrealdb.database import build_type_map
from frappe.database.surrealdb.errors import SurrealDBProgrammingError
from frappe.tests import UnitTestCase

FIELDTYPES = [
    "Data", "Link", "Dynamic Link", "Select", "Read Only", "Color", "Icon", "Phone", "Autocomplete", "Int", "Long Int",
    "Check", "Currency", "Float", "Percent", "Rating", "Duration", "Date", "Datetime", "Time", "Small Text", "Text",
    "Long Text", "Code", "Text Editor", "Markdown Editor", "HTML Editor", "Password", "Attach", "Attach Image",
    "Signature", "Barcode", "Geolocation", "JSON",
]  # fmt: skip


def column(fieldtype, **kw):
	args = dict(
        table=None, fieldname="f", fieldtype=fieldtype, length=None, default=None, set_index=0, options=None,
        unique=0, precision=None, not_nullable=0,
    )  # fmt: skip
	args.update(kw)
	return DbColumn(**args)


def mariadb_text(spec: S.ColumnSpec) -> str:
	"""Rebuild MariaDB's column definition text from a spec (the comparison target of DbColumn.get_definition)."""
	out = spec.logical
	if not spec.nullable:
		out += " NOT NULL"
	if spec.default is not None:
		# DDL text: strings are quoted, numbers print as Python prints them (information_schema pads decimals instead)
		if isinstance(spec.default, str):
			shown = "'" + "".join(S._MYSQL_ESCAPES.get(ch, ch) for ch in spec.default) + "'"
		else:
			shown = str(spec.default)
		out += f" DEFAULT {shown}"
	if spec.unique:
		out += " UNIQUE"
	return out


class TestSurrealDBSchema(UnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if frappe.db.db_type != "mariadb":
			raise unittest.SkipTest("compares against MariaDB's DbColumn")

	def test_type_map_is_identical_to_mariadbs(self):
		self.assertEqual(build_type_map(frappe.db.VARCHAR_LEN), frappe.db.type_map)

	def test_column_decisions_match_mariadbs_get_definition(self):
		defaults = [None, "", "abc", 'it\'s "q" \\ back', "0", "5", "1.5", "Today", ":Company"]
		grid = itertools.product(FIELDTYPES, defaults, (0, 1), (0, 1), (None, 50, 250), (None, 2))
		checked = 0
		for fieldtype, default, not_nullable, unique, length, precision in grid:
			col = column(
				fieldtype,
				default=default,
				not_nullable=not_nullable,
				unique=unique,
				length=length,
				precision=precision,
			)
			expected = col.get_definition()
			if not expected:
				self.assertIsNone(S.column_spec_from_docfield(col), fieldtype)
				continue
			spec = S.column_spec_from_docfield(col)
			self.assertEqual(
				mariadb_text(spec), expected, (fieldtype, default, not_nullable, unique, length, precision)
			)
			checked += 1
		self.assertGreater(checked, 5000)

	def test_layout_fieldtypes_have_no_column(self):
		for fieldtype in (
			"Section Break",
			"Column Break",
			"HTML",
			"Button",
			"Heading",
			"Tab Break",
			"Table",
			"Image",
		):
			self.assertIsNone(S.column_spec_from_docfield(column(fieldtype)), fieldtype)

	def test_physical_types(self):
		cases = {
            "Data": "string | null", "Text": "string | null", "JSON": "string | null", "Date": "string | null",
            "Datetime": "string | null", "Time": "int | null", "Int": "int", "Check": "int", "Long Int": "int | null",
            "Currency": "decimal", "Float": "decimal", "Percent": "decimal",
        }  # fmt: skip
		for fieldtype, expected in cases.items():
			self.assertEqual(S.column_spec_from_docfield(column(fieldtype)).surreal_type, expected, fieldtype)

	def test_identifiers(self):
		self.assertEqual(S.quote("Sales Order Item"), "`Sales Order Item`")
		self.assertEqual(S.quote_table("tabSales Order"), "`tabSales Order`")
		for bad in ("a`b", "a\\b", "", "x" * 201, "a\nb"):
			with self.assertRaises(SurrealDBProgrammingError, msg=repr(bad)):
				S.quote(bad)
		with self.assertRaises(SurrealDBProgrammingError):
			S.quote_table("tab`x")
		self.assertEqual(S.physical("id"), "id@f")
		self.assertEqual(S.logical_name("id@f"), "id")
		self.assertEqual(S.physical("type"), "type")
		self.assertTrue(S.is_shadow("name@ci") and S.is_shadow("name@like") and not S.is_shadow("name"))

	def test_reserved_names_are_stored_under_a_safe_name(self):
		for name in ("select", "function", "create", "delete", "id", "true", "Select"):
			self.assertEqual(S.physical(name), name + "@f", name)
			self.assertEqual(S.logical_name(S.physical(name)), name)
		for name in ("type", "value", "group", "order", "read", "submit", "title", "name", "in", "out"):
			self.assertEqual(
				S.physical(name), name, name
			)  # measured safe (spike/P1.4-schema/reserved_names_probe.py)
		specs = [
			S.column_spec_from_docfield(column("Check", fieldname="select", set_index=1)),
			S.column_spec_from_docfield(column("Data", fieldname="function", unique=1)),
		]
		text = "\n".join(S.create_statements("tabDocPerm", specs))
		self.assertIn("DEFINE FIELD `select@f` ON `tabDocPerm` TYPE int", text)
		self.assertIn("DEFINE FIELD `function@f@ci` ON `tabDocPerm`", text)
		self.assertIn("DEFINE INDEX `select@f` ON `tabDocPerm` FIELDS `select@f`", text)
		self.assertIn("DEFINE INDEX `function@f` ON `tabDocPerm` FIELDS `function@f@ci` UNIQUE", text)
		self.assertNotRegex(text, r"`select`|`function`")
		# introspection reports the Frappe names again
		info = {"fields": {}, "indexes": {}}
		for stmt in S.create_statements("tabDocPerm", specs):
			if stmt.startswith("DEFINE FIELD"):
				key = stmt.split("`")[1]
				info["fields"][f"`{key}`"] = stmt
			elif stmt.startswith("DEFINE INDEX"):
				info["indexes"][f"`{stmt.split('`')[1]}`"] = stmt
		parsed = S.parse_table_info(info)
		self.assertIn("select", parsed.columns)
		self.assertIn("function", parsed.columns)
		self.assertEqual(parsed.indexes["select"]["fields"], ["select@f"])
		self.assertEqual(S.base_field(parsed.indexes["function"]["fields"][0]), "function")
		self.assertEqual(parsed.column_flags("function"), (False, True))

	def test_string_literals(self):
		self.assertEqual(S.surql_string('a"b\\c\nd'), '"a\\"b\\\\c\\nd"')
		self.assertEqual(S.unescape('a\\"b\\\\c\\nd'), 'a"b\\c\nd')
		with self.assertRaises(SurrealDBProgrammingError):
			S.surql_string("bad\x00")

	def test_field_statement_shapes(self):
		spec = S.column_spec_from_docfield(column("Data", default="x", not_nullable=1))
		stmt = spec.define_field("tabT")
		self.assertTrue(
			stmt.startswith(
				'DEFINE FIELD `f` ON `tabT` TYPE string DEFAULT "x" ASSERT string::len($value) <= 140 COMMENT '
			)
		)
		nullable = S.column_spec_from_docfield(column("Date")).define_field("tabT")
		self.assertIn(
			"TYPE string | null DEFAULT NULL ASSERT $value = NULL OR (string::matches($value, /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/))",
			nullable,
		)
		check = S.column_spec_from_docfield(column("Check")).define_field("tabT")
		self.assertIn("TYPE int DEFAULT 0 ASSERT $value >= -128 AND $value <= 127", check)
		currency = S.column_spec_from_docfield(column("Currency")).define_field("tabT")
		self.assertIn("TYPE decimal DEFAULT 0.000000000dec", currency)

	def test_meta_round_trips_through_info_parsing(self):
		for default in (None, "", 'it\'s "q" \\ back', "日本 é 😀"):
			spec = S.column_spec_from_docfield(
				column("Data", default=default, not_nullable=1 if default == "" else 0)
			)
			info = {"fields": {"f": spec.define_field("tabT") + " PERMISSIONS FULL"}}
			meta = S.parse_table_info(info).columns["f"]
			self.assertEqual(meta["t"], spec.logical)
			self.assertEqual(meta["n"], int(spec.nullable))
			self.assertEqual(
				meta.get("d"),
				spec.display_default() if spec.default is not None else "NULL" if spec.nullable else None,
			)

	def test_shadows_only_for_varchar(self):
		self.assertEqual(len(S.column_spec_from_docfield(column("Data")).define_shadows("tabT")), 2)
		self.assertEqual(S.column_spec_from_docfield(column("Text")).define_shadows("tabT"), [])
		self.assertEqual(S.column_spec_from_docfield(column("Int")).define_shadows("tabT"), [])
		self.assertIn("`f@ci`", S.column_spec_from_docfield(column("Data")).define_shadows("tabT")[0])

	def test_create_statements_layout(self):
		specs = [
			S.column_spec_from_docfield(column("Data", fieldname="title", set_index=1)),
			S.column_spec_from_docfield(column("Link", fieldname="customer", unique=1)),
			S.column_spec_from_docfield(
				column("Long Text", fieldname="notes", set_index=1)
			),  # text is never indexed
			S.column_spec_from_docfield(column("Data", fieldname="id")),
		]
		stmts = S.create_statements("tabSales Order", specs, sort_modified=True)
		text = "\n".join(stmts)
		self.assertEqual(stmts[0], "DEFINE TABLE `tabSales Order` SCHEMAFULL")
		self.assertIn(
			"DEFINE FIELD `name` ON `tabSales Order` TYPE string ASSERT string::len($value) >= 1 AND string::len($value) <= 140",
			text,
		)
		for col in ("name", "title", "customer", "owner", "modified_by", "id@f"):
			self.assertIn(f"DEFINE FIELD `{col}@ci` ON", text.replace("`id@f@ci`", "`id@f@ci`"))
		self.assertNotIn("`notes@ci`", text)
		self.assertNotIn("DEFINE FIELD `id` ON", text)
		self.assertIn("DEFINE INDEX `creation` ON `tabSales Order` FIELDS `creation`", text)
		self.assertIn("DEFINE INDEX `modified` ON `tabSales Order` FIELDS `modified`", text)
		self.assertIn("DEFINE INDEX `title` ON `tabSales Order` FIELDS `title@ci`", text)
		self.assertIn("DEFINE INDEX `customer` ON `tabSales Order` FIELDS `customer@ci` UNIQUE", text)
		self.assertNotIn("INDEX `notes`", text)

	def test_child_and_special_names(self):
		child = "\n".join(S.create_statements("tabItem", [], is_child=True))
		for col in ("parent", "parentfield", "parenttype"):
			self.assertIn(f"DEFINE FIELD `{col}` ON `tabItem` TYPE string | null", child)
		self.assertIn("DEFINE INDEX `parent` ON `tabItem` FIELDS `parent@ci`", child)
		self.assertNotIn("INDEX `creation`", child)
		auto = "\n".join(S.create_statements("tabSeq", [], autoname="autoincrement"))
		self.assertIn("DEFINE FIELD `name` ON `tabSeq` TYPE int", auto)
		self.assertNotIn("`name@ci`", auto)
		uuid = "\n".join(S.create_statements("tabU", [], autoname="UUID"))
		self.assertIn("DEFINE FIELD `name` ON `tabU` TYPE string", uuid)

	def test_system_tables(self):
		for name in ("__Auth", "__global_search", "__UserSettings"):
			stmts = S.system_table_statements(name)
			self.assertEqual(stmts[0], f"DEFINE TABLE `{name}` SCHEMAFULL")
			self.assertTrue(any("UNIQUE" in s for s in stmts), name)
		with self.assertRaises(SurrealDBProgrammingError):
			S.system_table_statements("nope")

	def test_row_size_estimate_matches_mariadbs_rules(self):
		from frappe.database.surrealdb.database import estimated_column_size as size

		self.assertEqual(size("varchar(140)"), 2 + 560)
		self.assertEqual(size("tinyint(4)"), 1)
		self.assertEqual(size("int(11)"), 4)
		self.assertEqual(size("bigint(20)"), 8)
		self.assertEqual(
			size("decimal(21,9)"), 10
		)  # 12 integer digits -> 4+2 bytes, 9 scale digits -> 4 bytes
		self.assertEqual(size("datetime(6)"), 8)
		self.assertEqual(size("longtext"), 12)
		self.assertEqual(size("text"), 10)
