"""P1.15 against a real SurrealDB: shadow DDL, storage invariants, the engine-enforced @hash bypass
rejection, and rename/drop of shadowed columns (needs a server; skipped otherwise)."""

import json
import unittest
from unittest import mock

from pypika.queries import Table

import frappe
from frappe.database.surrealdb import collation as C
from frappe.database.surrealdb import errors as E
from frappe.database.surrealdb import schema as S
from frappe.database.surrealdb import shadow_migration as MIG
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
	NULL for SQL NULL, P0.8-revised: a NULL source carries its shadows as NULL, and an ABSENT shadow
	fails every later UPDATE of the row, so absence - NONE - is what counts as invalid)."""
	pred = (
		"IF (text_sh != NONE AND text_sh != NULL) THEN "
		"(`text_sh@ci` = NONE OR `text_sh@ci` = NULL "
		"OR `text_sh@like` = NONE OR `text_sh@like` = NULL "
		"OR `text_sh@hash` = NONE OR `text_sh@hash` = NULL "
		"OR `text_sh@hash` != crypto::sha256(text_sh)) "
		"ELSE (`text_sh@ci` = NONE OR `text_sh@like` = NONE OR `text_sh@hash` = NONE) END"
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


class _FileMeta(frappe._dict):
	"""The stand-in meta SurrealDBTable needs (same shape as setup_db._meta_from_file's)."""

	def get(self, key, default=None):
		return dict.get(self, key, default)

	def get_fieldnames_with_value(self, with_field_meta=False, with_virtual_fields=False):
		from frappe.model.meta import NO_VALUE_FIELDS

		def is_value_field(df):
			return df.get("fieldtype") not in NO_VALUE_FIELDS and (with_field_meta or not df.get("is_virtual"))

		if with_field_meta:
			return [df for df in self.fields if is_value_field(df)]
		return [df["fieldname"] for df in self.fields if is_value_field(df)]


def kid_meta():
	return _FileMeta({
		"name": "ParityKid",
		"fields": [
			{"fieldname": "title", "fieldtype": "Data"},
			{"fieldname": "text_sh", "fieldtype": "Text"},
			{"fieldname": "text", "fieldtype": "Long Text"},
		],
		"istable": 0,
		"issingle": 0,
		"autoname": None,
		"sort_field": "creation",
	})


def kid_spec(db):
	return S.table_schema(TABLE, db=db).column("text_sh")


def _sync():
	"""SurrealDBTable.sync() the way updatedb runs it: validate() populates current_columns first."""
	table = S.SurrealDBTable("ParityKid", kid_meta())
	table.validate()
	table.sync()
	return table

@unittest.skipUnless(LIVE, SKIP_REASON)
class TestSurrealDBTextShadowMigrationLive(LiveSurrealDB, UnitTestCase):
	"""The pre-migration state is built raw (column without shadows, rows without keys), like a table
	that predates the allow-list entry; every test then runs the real model-sync hook."""

	def setUp(self):
		db_name, db_user, password = self.new_site()
		self.provision(db_name, db_user, password)
		self.addCleanup(self._drop, db_name, db_user, password)
		self.db = self.connect(db_name, db_user, password)
		self.addCleanup(self.db.close)
		# SurrealDBTable.sync() routes every DDL/backfill statement through frappe.db — on a SurrealDB
		# site that IS the SurrealDB connection; the reference site is MariaDB, so bind ours for the test.
		self._orig_db = frappe.local.db
		frappe.local.db = self.db
		self.addCleanup(setattr, frappe.local, "db", self._orig_db)
		unmarked = [
			S.ColumnSpec("title", "varchar(140)"),
			S.ColumnSpec("text_sh", "text"),  # same logical type as the meta's Text (a longtext here
			# would make alter() convert_column on every sync, pre-defining the shadows+ASSERT)
			S.ColumnSpec("text", "longtext"),
			# the optional columns model sync adds to non-table doctypes: they must pre-exist here,
			# because a `string | null` field defined after rows exist leaves those rows holding the
			# field as NONE, which fails every later UPDATE of the row (SurrealDB 3.2.4, measured)
			S.ColumnSpec("_user_tags", "text"),
			S.ColumnSpec("_comments", "text"),
			S.ColumnSpec("_assign", "text"),
			S.ColumnSpec("_liked_by", "text"),
		]
		for stmt in S.create_statements(TABLE, unmarked):
			self.db.sql_ddl(stmt)
		# the raw CREATEs don't invalidate the per-site "db_tables" client cache (shared across the
		# throwaway sites in this process); drop it so is_new() sees this site's fresh table list
		frappe.client_cache.delete_value("db_tables")
		for name, value in (("M1", "Äpple"), ("M2", "straße"), ("M3", None)):
			sets = {"name": name, "name@ci": C.ci_key(name), "name@like": C.like_shadow(name), "text_sh": value}
			assignments = ", ".join(f"`{k}` = ${'p%d' % i}" for i, k in enumerate(sets))
			params = {f"p{i}": v for i, v in enumerate(sets.values())}
			params["tb"], params["rid"] = TABLE, C.record_id(name)
			self.db.sql(f"CREATE type::record($tb, $rid) SET {assignments}", params)
		S.clear_schema_cache()

	def _drop(self, db_name, db_user, password):
		from frappe.database.surrealdb import setup_db

		with self._site_conf(db_name, db_user, password):
			setup_db.drop_user_and_database(db_name, db_user)

	def test_sync_backfills_verifies_marks_and_enforces(self):
		_sync()
		fields = (self.db._info(f"INFO FOR TABLE `{TABLE}`").get("fields") or {})
		for key in ("`text_sh@ci`", "`text_sh@like`", "`text_sh@hash`"):
			self.assertIn(key, fields)
		self.assertIn(
			"ASSERT (IF $value = NONE OR $value = NULL THEN true ELSE $value = crypto::sha256($this.text_sh) END)",
			fields["`text_sh@hash`"],
		)
		self.assertEqual(S.parse_field_meta(fields["text_sh"]).get("csv"), TS.COLLATION_SHADOW_VERSION)
		self.assertIn("p115_text_sh", (self.db._info(f"INFO FOR TABLE `{TABLE}`").get("events") or {}))
		self.assertEqual(MIG.count_invalid_shadows(TABLE, kid_spec(self.db), self.db), 0)
		s, ci, lk, h = kid_shadow_row(self.db, TABLE, "M1")
		self.assertEqual((ci, lk, h), (C.ci_key("Äpple"), C.like_shadow("Äpple"), TS.source_hash("Äpple")))
		self.assertIsNone(kid_shadow_row(self.db, TABLE, "M3")[3])  # the NULL row: shadows stay absent
		kid_insert(self.db, TABLE, "M4", text_sh="neu")
		self.assertEqual(MIG.count_invalid_shadows(TABLE, kid_spec(self.db), self.db), 0)
		with self.assertRaises(E.SurrealDBError):
			self.db.sql(f"UPDATE `{TABLE}` SET text_sh = 'bypass' WHERE name = 'M4'")

	def test_sync_is_idempotent(self):
		_sync()
		with mock.patch.object(MIG, "backfill_column", wraps=MIG.backfill_column) as spy:
			_sync()
			spy.assert_not_called()
		self.assertEqual(MIG.count_invalid_shadows(TABLE, kid_spec(self.db), self.db), 0)

	def test_backfill_is_restartable(self):
		real = MIG._update_row
		calls = {"n": 0}

		def flaky(db, table, spec, rid, value):
			calls["n"] += 1
			if calls["n"] > 1:
				raise RuntimeError("injected crash mid-backfill")
			return real(db, table, spec, rid, value)

		with mock.patch.object(MIG, "_update_row", flaky):
			with self.assertRaises(RuntimeError):
				_sync()
		_sync()  # re-run to completion
		self.assertEqual(MIG.count_invalid_shadows(TABLE, kid_spec(self.db), self.db), 0)

	def test_stall_guard_raises_with_sample_ids(self):
		real_hash = TS.source_hash

		def poisoned(value):
			h = real_hash(value)
			return h[:-1] + ("0" if h[-1] != "0" else "1")

		with mock.patch.object(TS, "source_hash", poisoned):
			with self.assertRaises(MIG.ShadowBackfillStalled) as ctx:
				_sync()
			self.assertIn("made no progress", str(ctx.exception))

	def test_version_bump_forces_rebackfill(self):
		_sync()
		with mock.patch.object(TS, "COLLATION_SHADOW_VERSION", 2), \
			 mock.patch.object(MIG, "backfill_column", wraps=MIG.backfill_column) as spy:
			_sync()
			spy.assert_called_once()
			self.assertTrue(spy.call_args.kwargs.get("force"))
		fields = (self.db._info(f"INFO FOR TABLE `{TABLE}`").get("fields") or {})
		self.assertEqual(S.parse_field_meta(fields["text_sh"]).get("csv"), 2)
		self.assertEqual(MIG.count_invalid_shadows(TABLE, kid_spec(self.db), self.db), 0)

	def test_pending_column_raises_not_ready(self):
		_sync()
		fields = (self.db._info(f"INFO FOR TABLE `{TABLE}`").get("fields") or {})
		meta = S.parse_field_meta(fields["text_sh"])
		meta.pop("csv", None)
		strip = MIG._COMMENT_TOKEN.sub(
			f"COMMENT {S.surql_string(json.dumps(meta, separators=(',', ':'), ensure_ascii=False))}",
			fields["text_sh"], count=1,
		)
		if " OVERWRITE " not in strip.split("COMMENT")[0]:
			strip = strip.replace("DEFINE FIELD ", "DEFINE FIELD OVERWRITE ", 1)
		self.db.sql_ddl(strip)
		S.clear_schema_cache()
		self.assertFalse(S.table_schema(TABLE, db=self.db).column("text_sh").shadow_ready)
		with self.assertRaises(E.SurrealDBNotImplementedError):
			run(self.db, SurrealDB.from_(T).select(T.name).where(T.text_sh == "x"))
		_sync()  # migrate re-establishes readiness
		S.clear_schema_cache()
		self.assertTrue(S.table_schema(TABLE, db=self.db).column("text_sh").shadow_ready)