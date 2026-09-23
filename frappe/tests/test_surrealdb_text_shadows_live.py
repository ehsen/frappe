"""P1.15 against a real SurrealDB: shadow DDL, storage invariants, the engine-enforced @hash bypass
rejection, and rename/drop of shadowed columns (needs a server; skipped otherwise)."""

import unittest

from pypika.queries import Table

from frappe.database.surrealdb import collation as C
from frappe.database.surrealdb import errors as E
from frappe.database.surrealdb import schema as S
from frappe.database.surrealdb import text_shadows as TS
from frappe.database.surrealdb.translator import render
from frappe.query_builder.surrealdb_builder import SurrealDB
from frappe.tests import UnitTestCase
from frappe.tests.surrealdb_live import LIVE, SKIP_REASON, LiveSurrealDB

TABLE = "tabParityKid"
TABLE2 = "tabParityKid2"  # the destructive cases (drop / rename) run here
T, T2 = Table(TABLE), Table(TABLE2)

if LIVE:
	# The harness allow-lists the kid's text columns before the driver's first connection (freeze()).
	# `tabParityKid.text` stays unregistered as the unshadowed regression guard.
	TS.register(TABLE, "text_sh")
	TS.register(TABLE2, "text_sh")


def kid_specs():
	# `name` is added by create_statements itself (name_spec); specs here are the table's own columns.
	text_sh = S.ColumnSpec("text_sh", "longtext")
	text_sh.text_collation_shadow = True
	return [
		S.ColumnSpec("title", "varchar(140)"),
		text_sh,
		S.ColumnSpec("text", "longtext"),  # unshadowed guard column
	]


def run(db, query):
	"""Render a qb query against `db`'s own schema (the driver's default loader reads frappe.db, which is
	MariaDB on the reference site) and execute it — the parity suites' convention."""
	sql, params = render(query, None, lambda name: S.table_schema(name, db=db))
	return db.sql(sql, params.values)


def kid_insert(db, table, name, text_sh=None, title="t"):
	t = Table(table)
	run(db, SurrealDB.into(t).columns("name", "title", "text_sh").insert((name, title, text_sh)))


def kid_shadow_row(db, table, name):
	# NOTE: table-qualified projections of `@`-fields read back as NULL (SurrealDB quirk, measured) —
	# the projections must be un-prefixed.
	rows = db.sql(
		f"SELECT text_sh AS s, `text_sh@ci` AS ci, `text_sh@like` AS lk, `text_sh@hash` AS h "
		f"FROM `{table}` WHERE name = $n /*cols:s,ci,lk,h*/",
		{"n": name},
	)
	return rows[0] if rows else (None, None, None, None)


def invalid_count(db, table=TABLE):
	"""Rows whose shadows or hash are missing/stale (the sync hook's predicate; the engine stores real
	NULL for SQL NULL, P0.8-revised: both NONE and NULL are 'absent'). The in-engine check can verify
	@hash (crypto::sha256) but not @ci/@like (ci_key/like_shadow are Python-only) — staleness of those
	is the health command's (Python-side) job."""
	pred = (
		"IF text_sh = NONE OR text_sh = NULL THEN "
		"((`text_sh@ci` != NONE AND `text_sh@ci` != NULL) "
		"OR (`text_sh@like` != NONE AND `text_sh@like` != NULL) "
		"OR (`text_sh@hash` != NONE AND `text_sh@hash` != NULL)) "
		"ELSE "
		"(`text_sh@ci` = NONE OR `text_sh@ci` = NULL "
		"OR `text_sh@like` = NONE OR `text_sh@like` = NULL "
		"OR `text_sh@hash` = NONE OR `text_sh@hash` = NULL "
		"OR `text_sh@hash` != crypto::sha256(text_sh)) END"
	)
	rows = db.sql(f"SELECT count() AS n FROM `{table}` WHERE {pred} GROUP ALL /*cols:n*/")
	return rows[0][0] if rows and rows[0][0] else 0


@unittest.skipUnless(LIVE, SKIP_REASON)
class TestSurrealDBTextShadowLive(LiveSurrealDB, UnitTestCase):
	def setUp(self):
		db_name, db_user, password = self.new_site()
		self.provision(db_name, db_user, password)
		self.addCleanup(self._drop, db_name, db_user, password)
		self.db = self.connect(db_name, db_user, password)  # freezes the registry
		self.addCleanup(self.db.close)
		for table in (TABLE, TABLE2):
			for stmt in S.create_statements(table, kid_specs()):
				self.db.sql_ddl(stmt)
		S.clear_schema_cache()

	def _drop(self, db_name, db_user, password):
		from frappe.database.surrealdb import setup_db

		with self._site_conf(db_name, db_user, password):
			setup_db.drop_user_and_database(db_name, db_user)

	def test_registry_marking(self):
		self.assertTrue(TS.is_shadowed(TABLE, "text_sh"))
		self.assertFalse(TS.is_shadowed(TABLE, "text"))
		schema = S.table_schema(TABLE, db=self.db)
		self.assertTrue(schema.column("text_sh").has_collation_shadow)
		self.assertTrue(schema.column("text_sh").has_integrity_hash)
		self.assertFalse(schema.column("text").has_collation_shadow)
		self.assertTrue(schema.column("title").has_collation_shadow)  # varchar, as always
		self.assertFalse(schema.column("title").has_integrity_hash)

	def test_info_shows_shadows_with_assert(self):
		fields = (self.db._info(f"INFO FOR TABLE `{TABLE}`").get("fields") or {})
		self.assertIn("`text_sh@ci`", fields)
		self.assertIn("`text_sh@like`", fields)
		self.assertIn("`text_sh@hash`", fields)
		self.assertIn(
			"ASSERT (IF $value = NONE OR $value = NULL THEN true ELSE $value = crypto::sha256($this.text_sh) END)",
			fields["`text_sh@hash`"],
		)
		self.assertNotIn("`text@ci`", fields)  # the unshadowed guard column stays plain
		self.assertNotIn("`text@hash`", fields)
		for definition in (self.db._info(f"INFO FOR TABLE `{TABLE}`").get("indexes") or {}).values():
			self.assertNotIn("text_sh@ci", definition)  # never index-eligible

	def test_insert_moves_all_four_together(self):
		kid_insert(self.db, TABLE, "K1", text_sh="Äpple")
		s, ci, lk, h = kid_shadow_row(self.db, TABLE, "K1")
		self.assertEqual(s, "Äpple")
		self.assertEqual(ci, C.ci_key("Äpple"))
		self.assertEqual(lk, C.like_shadow("Äpple"))
		self.assertEqual(h, TS.source_hash("Äpple"))
		self.assertEqual(invalid_count(self.db), 0)

	def test_update_bound_value(self):
		kid_insert(self.db, TABLE, "K2", text_sh="alt")
		run(self.db, SurrealDB.update(T).set(T.text_sh, "ß-ss").where(T.name == "K2"))
		_, ci, lk, h = kid_shadow_row(self.db, TABLE, "K2")
		self.assertEqual(ci, C.ci_key("ß-ss"))
		self.assertEqual(lk, C.like_shadow("ß-ss"))
		self.assertEqual(h, TS.source_hash("ß-ss"))
		self.assertEqual(invalid_count(self.db), 0)

	def test_update_expression_fails_closed(self):
		kid_insert(self.db, TABLE, "K3", text_sh="self")
		# a varchar source has no witness -> fail closed (the shadowed-to-shadowed copy becomes
		# reachable with the _family flip in Phase 5; pinned there)
		with self.assertRaises(E.SurrealDBNotImplementedError):
			run(self.db, SurrealDB.update(T).set(T.text_sh, T.title).where(T.name == "K3"))
		self.assertEqual(invalid_count(self.db), 0)

	def test_set_null_then_empty(self):
		kid_insert(self.db, TABLE, "K4", text_sh="gone")
		run(self.db, SurrealDB.update(T).set(T.text_sh, None).where(T.name == "K4"))
		self.assertEqual(kid_shadow_row(self.db, TABLE, "K4"), (None, None, None, None))
		self.assertEqual(invalid_count(self.db), 0)
		run(self.db, SurrealDB.update(T).set(T.text_sh, "").where(T.name == "K4"))
		_, ci, lk, h = kid_shadow_row(self.db, TABLE, "K4")
		self.assertEqual((ci, lk, h), (C.ci_key(""), C.like_shadow(""), TS.source_hash("")))
		self.assertEqual(invalid_count(self.db), 0)

	def test_unrelated_update_passes_on_valid_row(self):
		kid_insert(self.db, TABLE, "K5", text_sh="keep")
		run(self.db, SurrealDB.update(T).set(T.title, "other").where(T.name == "K5"))
		self.assertEqual(invalid_count(self.db), 0)

	def test_bypass_write_rejected_and_not_persisted(self):
		kid_insert(self.db, TABLE, "K6", text_sh="before")
		with self.assertRaises(E.SurrealDBError):
			self.db.sql(f"UPDATE `{TABLE}` SET text_sh = 'after' WHERE name = 'K6'")
		s, _, _, _ = kid_shadow_row(self.db, TABLE, "K6")
		self.assertEqual(s, "before")
		self.assertEqual(invalid_count(self.db), 0)

	def test_forgery_accepted_by_engine_stale_ci_detected_python_side(self):
		kid_insert(self.db, TABLE, "K7", text_sh="orig")
		# documented limitation: a write that sets source AND a correct hash together is accepted
		# (it requires intent). The in-engine integrity count is blind to @ci staleness (ci_key is
		# Python-only); the Phase 4 health command verifies @ci/@like Python-side.
		self.db.sql(
			f"UPDATE `{TABLE}` SET text_sh = 'forged', `text_sh@hash` = $h WHERE name = 'K7'",
			{"h": TS.source_hash("forged")},
		)
		s, ci, _, _ = kid_shadow_row(self.db, TABLE, "K7")
		self.assertEqual(s, "forged")
		self.assertNotEqual(ci, C.ci_key("forged"))  # the stale witness, visible Python-side
		self.assertEqual(invalid_count(self.db), 0)  # and invisible to the in-engine predicate
		run(self.db, SurrealDB.update(T).set(T.text_sh, "restored").where(T.name == "K7"))
		_, ci, _, _ = kid_shadow_row(self.db, TABLE, "K7")
		self.assertEqual(ci, C.ci_key("restored"))

	def test_rename_moves_shadows_assert_and_values(self):
		kid_insert(self.db, TABLE2, "K8", text_sh="move me")
		self.db.rename_column("ParityKid2", "text_sh", "text_sh2")
		S.clear_schema_cache()
		fields = (self.db._info(f"INFO FOR TABLE `{TABLE2}`").get("fields") or {})
		self.assertIn("`text_sh2@hash`", fields)
		self.assertIn(
			"ASSERT (IF $value = NONE OR $value = NULL THEN true ELSE $value = crypto::sha256($this.text_sh2) END)",
			fields["`text_sh2@hash`"],
		)
		self.assertNotIn("`text_sh@hash`", fields)
		rows = self.db.sql(
			f"SELECT `text_sh2@ci` AS ci, `text_sh2@hash` AS h FROM `{TABLE2}` WHERE name = $n /*cols:ci,h*/",
			{"n": "K8"},
		)
		self.assertEqual(rows[0][0], C.ci_key("move me"))
		self.assertEqual(rows[0][1], TS.source_hash("move me"))

	def test_drop_removes_source_shadows_and_witness(self):
		kid_insert(self.db, TABLE2, "K9", text_sh="drop me")
		self.db.drop_columns("ParityKid2", ["text_sh"])
		fields = (self.db._info(f"INFO FOR TABLE `{TABLE2}`").get("fields") or {})
		self.assertNotIn("`text_sh`", fields)
		self.assertNotIn("`text_sh@ci`", fields)
		self.assertNotIn("`text_sh@hash`", fields)