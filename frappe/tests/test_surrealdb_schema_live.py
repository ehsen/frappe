"""P1.4 against a real SurrealDB: DDL, introspection, constraints, alteration, sequences (skipped without a server)."""

import unittest

import frappe
from frappe.database.surrealdb import collation as C
from frappe.database.surrealdb import errors as E
from frappe.database.surrealdb import schema as S
from frappe.tests import UnitTestCase
from frappe.tests.surrealdb_live import LIVE, SKIP_REASON, LiveSurrealDB


def col(fieldtype, fieldname, **kw):
	from frappe.database.schema import DbColumn

	args = dict(
        table=None, fieldname=fieldname, fieldtype=fieldtype, length=None, default=None, set_index=0, options=None,
        unique=0, precision=None, not_nullable=0,
    )  # fmt: skip
	args.update(kw)
	return S.column_spec_from_docfield(DbColumn(**args))


def put(db, table, name, **fields):
	"""Insert one row the way the translator will: record id from the name, shadows computed in Python."""
	sets = {"name": name, "name@ci": C.ci_key(name), "name@like": C.like_shadow(name), **fields}
	assignments = ", ".join(f"`{k}` = ${'p%d' % i}" for i, k in enumerate(sets))
	params = {"p%d" % i: v for i, v in enumerate(sets.values())}
	params["tb"], params["rid"] = table, C.record_id(name)
	db.sql(f"CREATE type::record($tb, $rid) SET {assignments}", params)


def shadowed(**fields):
	out = {}
	for key, value in fields.items():
		out[key] = value
		if isinstance(value, str):
			out[key + "@ci"], out[key + "@like"] = C.ci_key(value), C.like_shadow(value)
	return out


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestSurrealDBSchemaLive(LiveSurrealDB, UnitTestCase):
	def make_db(self):
		db_name, db_user, password = self.new_site()
		self.provision(db_name, db_user, password)
		self.addCleanup(self._drop, db_name, db_user, password)
		db = self.connect(db_name, db_user, password)
		self.addCleanup(db.close)
		return db

	def _drop(self, db_name, db_user, password):
		from frappe.database.surrealdb import setup_db

		with self._site_conf(db_name, db_user, password):
			setup_db.drop_user_and_database(db_name, db_user)

	def create(self, db, table, specs, **kw):
		for stmt in S.create_statements(table, specs, **kw):
			db.sql_ddl(stmt)

	def specs(self):
		return [
			col("Data", "title", set_index=1),
			col("Link", "customer", unique=1),
			col("Check", "enabled"),
			col("Currency", "amount"),
			col("Int", "qty", not_nullable=1),
			col("Date", "posting_date"),
			col("Datetime", "stamp"),
			col("Time", "at"),
			col("Long Text", "notes"),
			col("JSON", "extra"),
			col("Data", "id"),
			col("Data", "type", default='it\'s a "default" \\ é'),
		]

	# --- create + introspection -------------------------------------------------------------------------------------
	def test_create_table_and_introspect(self):
		db = self.make_db()
		self.create(db, "tabProbe Doc", self.specs(), sort_modified=True)
		db.commit()
		self.assertIn("tabProbe Doc", db.get_tables(cached=False))

		desc = {c.name: c for c in db.get_table_columns_description("tabProbe Doc")}
		for name, logical, not_nullable in (
            ("name", "varchar(140)", True), ("creation", "datetime(6)", False), ("docstatus", "tinyint(4)", True),
            ("idx", "int(11)", True), ("title", "varchar(140)", False), ("enabled", "tinyint(4)", True),
            ("amount", "decimal(21,9)", True), ("qty", "int(11)", True), ("posting_date", "date", False),
            ("stamp", "datetime(6)", False), ("at", "time(6)", False), ("notes", "longtext", False),
            ("extra", "json", False), ("id", "varchar(140)", False),
        ):  # fmt: skip
			self.assertEqual((desc[name].type, desc[name].not_nullable), (logical, not_nullable), name)
		self.assertNotIn("title@ci", desc)  # shadows are an implementation detail
		self.assertNotIn("id@f", desc)
		self.assertEqual(desc["type"].default, "'it\\'s a \\\"default\\\" \\\\ é'")
		self.assertEqual(desc["enabled"].default, "0")
		self.assertEqual(desc["title"].default, "NULL")
		self.assertEqual(set(db.get_db_table_columns("tabProbe Doc")), set(desc))
		self.assertEqual(db.get_column_type("Probe Doc", "amount"), "decimal(21,9)")

		self.assertTrue(db.has_index("tabProbe Doc", "creation"))
		self.assertTrue(db.has_index("tabProbe Doc", "modified"))
		self.assertFalse(db.has_index("tabProbe Doc", "nope"))
		self.assertEqual(db.get_column_index("tabProbe Doc", "title").Key_name, "title")
		self.assertEqual(db.get_column_index("tabProbe Doc", "customer", unique=True).Key_name, "customer")
		self.assertIsNone(db.get_column_index("tabProbe Doc", "customer", unique=False))
		self.assertEqual(
			(desc["title"].index, desc["title"].unique, desc["customer"].unique), (1, False, True)
		)
		rows = {r[0]: r for r in db.describe("Probe Doc")}
		self.assertEqual((rows["name"][3], rows["customer"][3], rows["title"][3]), ("PRI", "UNI", "MUL"))
		self.assertGreater(db.get_row_size("Probe Doc"), 1000)

	def test_reserved_field_and_index_names_do_not_poison_the_table(self):
		db = self.make_db()
		names = ("select", "function", "create", "delete", "update", "id", "true", "type", "value", "group")
		specs = [
			col("Check", n, set_index=1) if n not in ("id", "type") else col("Data", n, unique=1)
			for n in names
		]
		self.create(db, "tabDocPerm", specs)
		db.commit()
		desc = {c.name for c in db.get_table_columns_description("tabDocPerm")}
		self.assertTrue(set(names) <= desc)
		for n in names:
			self.assertTrue(db.has_index("tabDocPerm", n), n)
		self.assertTrue(db.get_column_index("tabDocPerm", "select"))
		self.assertTrue(db.get_column_index("tabDocPerm", "id", unique=True))
		# the table keeps working: write, read back by the physical names, alter it
		put(db, "tabDocPerm", "one", **{S.physical("select"): 1, S.physical("function"): 0})
		self.assertEqual(
			db.sql(
				"SELECT name, `select@f` AS `select`, `function@f` AS `function` FROM tabDocPerm /*cols:name,select,function*/"
			),
			(("one", 1, 0),),
		)
		db.rename_column("DocPerm", "select", "chosen")
		db.commit()
		cols = {c.name for c in db.get_table_columns_description("tabDocPerm")}
		self.assertIn("chosen", cols)
		self.assertNotIn("select", cols)
		self.assertTrue(db.has_index("tabDocPerm", "chosen"))
		self.assertFalse(db.has_index("tabDocPerm", "select"))
		put(db, "tabDocPerm", "two", chosen=1)

	# --- constraints ------------------------------------------------------------------------------------------------
	def test_unique_is_collation_insensitive_and_allows_many_nulls(self):
		db = self.make_db()
		self.create(db, "tabU", [col("Link", "customer", unique=1)])
		put(db, "tabU", "one", **shadowed(customer="Résumé"))
		put(db, "tabU", "two")  # NULL customer
		put(db, "tabU", "three")  # a second NULL is fine, as in MariaDB
		with self.assertRaises(E.SurrealDBIntegrityError) as dup:
			put(db, "tabU", "four", **shadowed(customer="RESUME  "))
		self.assertTrue(db.is_unique_key_violation(dup.exception))
		self.assertEqual(
			dup.exception.args, (1062, f"Duplicate entry '{C.ci_key('RESUME')}' for key 'customer'")
		)
		db.rollback()
		# the primary key is collation-insensitive too: same record id
		put(db, "tabU", "Abc")
		with self.assertRaises(E.SurrealDBIntegrityError) as pk:
			put(db, "tabU", "aBC")
		self.assertTrue(db.is_primary_key_violation(pk.exception))
		self.assertEqual(pk.exception.args, (1062, "Duplicate entry 'abc' for key 'PRIMARY'"))

	def test_constraint_errors_map_to_mariadb_errors(self):
		db = self.make_db()
		self.create(
			db,
			"tabC",
			[
				col("Int", "qty", not_nullable=1),
				col("Date", "d"),
				col("Check", "flag"),
				col("Data", "s", length=64),
			],
		)
		cases = [
			({"qty": None}, (1048, "Column 'qty' cannot be null")),
			(
				{"qty": 1, "d": "2024-1-5"},
				(1292, "Incorrect date/datetime value: '2024-1-5' for column 'd' at row 1"),
			),
			({"qty": 1, "flag": 128}, (1264, "Out of range value for column 'flag' at row 1")),
			({"qty": "5"}, (1366, "Incorrect int value: '5' for column 'qty' at row 1")),
			({"qty": 1, **shadowed(s="x" * 65)}, (1406, "Data too long for column 's' at row 1")),
		]
		for i, (fields, expected) in enumerate(cases):
			with self.subTest(fields=fields), self.assertRaises(E.SurrealDBError) as err:
				put(db, "tabC", f"row{i}", **fields)
			self.assertEqual(err.exception.args, expected)
			db.rollback()
		put(db, "tabC", "ok", qty=1)  # defaults fill the rest
		self.assertEqual(db.sql("SELECT flag, d FROM tabC /*cols:flag,d*/"), ((0, None),))

	def test_undefined_table_and_column_errors(self):
		db = self.make_db()
		self.create(db, "tabD", [col("Data", "a")])
		with self.assertRaises(E.SurrealDBProgrammingError) as err:
			db.sql("CREATE type::record('tabD', 'x') SET name = 'x', nofield = 1")
		self.assertTrue(db.is_missing_column(err.exception))  # SCHEMAFULL rejects unknown fields on write
		db.rollback()

	def test_create_after_write_in_one_transaction_is_not_an_implicit_commit(self):
		db = self.make_db()
		self.create(db, "tabT2", [col("Data", "a")])
		db.commit()
		put(db, "tabT2", "one")
		db.sql("UPDATE tabT2 SET a = $a", {"a": "x"})
		put(
			db, "tabT2", "two"
		)  # MariaDB's guard would raise ImplicitCommitError for a `create` after a write
		db.commit()
		self.assertEqual(db.sql("SELECT count() AS c FROM tabT2 GROUP ALL /*cols:c*/"), ((2,),))

	# --- alteration -------------------------------------------------------------------------------------------------
	def test_add_column_backfills_not_null_default(self):
		db = self.make_db()
		self.create(db, "tabA", [col("Data", "a")])
		put(db, "tabA", "old1")
		put(db, "tabA", "old2")
		db.commit()
		for spec in (
			col("Int", "qty", not_nullable=1, default=7),
			col("Data", "code", not_nullable=1, default="x"),
		):
			db.sql_ddl(spec.define_field("tabA"))
			for stmt in spec.define_shadows("tabA"):
				db.sql_ddl(stmt)
			S.backfill_default("tabA", spec, db=db)
		db.commit()
		rows = db.sql("SELECT name, qty, code, `code@ci` AS ci FROM tabA /*cols:name,qty,code,ci*/")
		self.assertEqual(set(rows), {("old1", 7, "x", C.ci_key("x")), ("old2", 7, "x", C.ci_key("x"))})
		db.sql(
			"UPDATE tabA SET a = 'changed'"
		)  # MariaDB allows this; a NOT NULL field without backfill would not
		db.commit()

	def test_add_index_and_unique_through_the_database_api(self):
		db = self.make_db()
		self.create(db, "tabIx", [col("Data", "a"), col("Data", "b"), col("Int", "n")])
		db.commit()
		frappe.flags.in_install = (
			True  # keeps add_index from writing a Property Setter into the site's MariaDB
		)
		try:
			db.add_index("Ix", ["a"])
			db.add_index("Ix", ["a", "n"])
			db.add_unique("Ix", ["b"])
			db.add_unique("Ix", "n")
		finally:
			frappe.flags.in_install = False
		self.assertTrue(db.has_index("tabIx", "a_index"))
		self.assertTrue(db.has_index("tabIx", "a_n_index"))
		self.assertTrue(db.has_index("tabIx", "unique_b"))
		info = db.table_info("tabIx")
		self.assertEqual(info.indexes["a_index"]["fields"], ["a@ci"])
		self.assertEqual(info.indexes["a_n_index"]["fields"], ["a@ci", "n"])
		self.assertEqual(
			info.indexes["unique_b"],
			{"name": "unique_b", "fields": ["b@ci"], "unique": True, "kind": "UNIQUE"},
		)
		self.assertEqual(info.indexes["unique_n"]["fields"], ["n"])
		db.add_index("Ix", ["a"])  # idempotent
		with self.assertRaises(E.SurrealDBProgrammingError):
			db.add_index("Ix", ["missing"])

	def test_rename_column_and_table(self):
		db = self.make_db()
		self.create(db, "tabR Old", [col("Data", "old_name", set_index=1), col("Int", "n")])
		put(db, "tabR Old", "Row One", **shadowed(old_name="Ünï"), n=3)
		db.commit()
		db.rename_column("R Old", "old_name", "new_name")
		db.commit()
		cols = {c.name: c for c in db.get_table_columns_description("tabR Old")}
		self.assertIn("new_name", cols)
		self.assertNotIn("old_name", cols)
		self.assertEqual(
			db.sql("SELECT new_name, `new_name@ci` AS k FROM `tabR Old` /*cols:new_name,k*/"),
			(("Ünï", C.ci_key("Ünï")),),
		)
		self.assertTrue(db.has_index("tabR Old", "new_name"))
		self.assertEqual(db.table_info("tabR Old").indexes["new_name"]["fields"], ["new_name@ci"])

		db.rename_table("R Old", "R New")
		db.commit()
		self.assertIn("tabR New", db.get_tables(cached=False))
		self.assertNotIn("tabR Old", db.get_tables(cached=False))
		self.assertEqual(
			db.sql("SELECT name, new_name, n FROM `tabR New` /*cols:name,new_name,n*/"),
			(("Row One", "Ünï", 3),),
		)
		self.assertTrue(db.has_index("tabR New", "creation"))
		# the row keeps its (collation-derived) id under the new table
		self.assertEqual(
			db.sql("SELECT record::id(id) AS i FROM `tabR New` /*cols:i*/"), ((C.record_id("Row One"),),)
		)

	def test_change_column_type_converts_existing_values(self):
		db = self.make_db()
		self.create(db, "tabM", [col("Data", "v")])
		put(db, "tabM", "a", **shadowed(v="12"))
		put(db, "tabM", "b", **shadowed(v="7.5"))
		put(db, "tabM", "c")
		db.commit()
		db.change_column_type("M", "v", "int(11)", nullable=True)
		db.commit()
		self.assertEqual(
			sorted(db.sql("SELECT name, v FROM tabM /*cols:name,v*/"), key=lambda r: r[0]),
			[("a", 12), ("b", 8), ("c", None)],
		)
		self.assertEqual(db.get_column_type("M", "v"), "int(11)")
		db.sql("UPDATE tabM SET v = 1 WHERE name = 'a'")
		db.commit()

		self.create(db, "tabM2", [col("Data", "v")])
		put(db, "tabM2", "a", **shadowed(v="12"))
		put(db, "tabM2", "b", **shadowed(v="abc"))
		db.commit()
		with self.assertRaises(
			frappe.ValidationError
		):  # values that cannot convert are refused, like MariaDB
			db.change_column_type("M2", "v", "int(11)", nullable=True)
		db.rollback()
		self.assertEqual(db.get_column_type("M2", "v"), "varchar(140)")  # nothing was altered

	# --- system tables and sequences -----------------------------------------------------------------------------
	def test_system_tables(self):
		db = self.make_db()
		db.create_auth_table()
		db.create_global_search_table()
		db.create_user_settings_table()
		db.create_auth_table()  # idempotent
		tables = set(db.get_tables(cached=False))
		self.assertTrue({"__Auth", "__global_search", "__UserSettings"} <= tables)
		self.assertEqual(
			{c.name for c in db.get_table_columns_description("__Auth")},
			{"doctype", "name", "fieldname", "password", "encrypted"},
		)

		def insert(doctype, name, fieldname, password):
			keys = {}
			for column, value in (
				("doctype", doctype),
				("name", name),
				("fieldname", fieldname),
			):  # varchar columns carry shadows
				keys[column], keys[column + "@ci"], keys[column + "@like"] = (
					value,
					C.ci_key(value),
					C.like_shadow(value),
				)
			assignments = ", ".join(f"`{k}` = $v{i}" for i, k in enumerate(keys))
			params = {f"v{i}": v for i, v in enumerate(keys.values())} | {"pw": password}
			db.sql(f"CREATE `__Auth` SET {assignments}, `password` = $pw, `encrypted` = 0", params)

		insert("User", "admin", "password", "x")
		with self.assertRaises(
			E.SurrealDBIntegrityError
		) as dup:  # composite primary key, collation-insensitive
			insert("USER", "ADMIN", "PASSWORD", "y")
		self.assertTrue(db.is_duplicate_entry(dup.exception))

	def test_sequences(self):
		db = self.make_db()
		seq = db.create_sequence("Probe Seq", check_not_exists=True)
		self.assertEqual(seq, "probe_seq_id_seq")
		db.create_sequence("Probe Seq", check_not_exists=True)  # idempotent
		self.assertEqual([db.get_next_sequence_val("Probe Seq") for _ in range(3)], [1, 2, 3])
		db.rollback()  # sequences are not transactional, as in MariaDB: the numbers stay used
		self.assertEqual(db.get_next_sequence_val("Probe Seq"), 4)
		db.set_next_sequence_val("Probe Seq", 100)
		self.assertEqual(db.get_next_sequence_val("Probe Seq"), 100)
		db.set_next_sequence_val("Probe Seq", 200, is_val_used=True)
		self.assertEqual(db.get_next_sequence_val("Probe Seq"), 201)
		with self.assertRaises(E.SurrealDBNotImplementedError):
			db.create_sequence("Other", increment_by=5)

	def test_truncate_and_estimate(self):
		db = self.make_db()
		self.create(db, "tabTr", [col("Data", "a")])
		for i in range(3):
			put(db, "tabTr", f"r{i}")
		db.commit()
		self.assertEqual(db._estimate_count("tabTr"), 3)
		db.truncate("Tr")
		db.commit()
		self.assertEqual(db._estimate_count("tabTr"), 0)
