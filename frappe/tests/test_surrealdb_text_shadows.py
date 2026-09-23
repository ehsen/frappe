"""Unit tests of the text-collation-shadow registry and primitive (no server needed, P1.15)."""

import hashlib
import unittest

import frappe
from frappe.database.surrealdb import collation as C
from frappe.database.surrealdb import schema as S
from frappe.database.surrealdb import text_shadows as TS
from frappe.database.surrealdb.errors import SurrealDBProgrammingError
from frappe.tests import UnitTestCase


class TestSurrealDBTextShadows(UnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if frappe.db.db_type != "mariadb":
			raise unittest.SkipTest("offline registry tests; pinned to the MariaDB site like the other offline suites")

	def setUp(self):
		TS._reset_for_tests()  # test-only hook; never used in production code

	def tearDown(self):
		TS._reset_for_tests()

	def test_register_after_freeze_raises(self):
		TS.freeze()
		with self.assertRaises(RuntimeError):
			TS.register("tabX", "f")

	def test_is_shadowed_before_freeze_raises(self):
		with self.assertRaises(RuntimeError):
			TS.is_shadowed("tabX", "f")
		with self.assertRaises(RuntimeError):
			TS.all_shadowed()

	def test_membership_before_and_after_freeze(self):
		TS.register("tabParityKid", "text_sh")
		TS.freeze()
		self.assertTrue(TS.is_shadowed("tabParityKid", "text_sh"))
		self.assertFalse(TS.is_shadowed("tabParityKid", "text"))
		self.assertFalse(TS.is_shadowed("tabOther", "text_sh"))
		self.assertEqual(TS.all_shadowed(), TS.BUILTIN | frozenset({TS.TextCollationShadow("tabParityKid", "text_sh")}))

	def test_freeze_is_idempotent_and_builtin_needs_no_registration(self):
		TS.freeze()
		TS.freeze()
		self.assertEqual(TS.all_shadowed(), TS.BUILTIN)  # BUILTIN entries are members without registration (the P1.15 allow-list activations)

	def test_columnspec_capability_gates(self):
		with self.assertRaises(SurrealDBProgrammingError):
			S.ColumnSpec("f", "varchar(140)", text_collation_shadow=True)
		with self.assertRaises(SurrealDBProgrammingError):
			S.ColumnSpec("f", "int(11)", text_collation_shadow=True)
		text = S.ColumnSpec("f", "longtext", text_collation_shadow=True)
		self.assertTrue(text.has_collation_shadow)
		self.assertTrue(text.has_integrity_hash)
		self.assertFalse(text.is_length_limited_string)
		self.assertFalse(text.shadow_index_eligible)
		json_spec = S.ColumnSpec("f", "json", text_collation_shadow=True)
		self.assertTrue(json_spec.has_collation_shadow)
		self.assertTrue(json_spec.has_integrity_hash)
		varchar = S.ColumnSpec("f", "varchar(140)")
		self.assertTrue(varchar.has_collation_shadow)
		self.assertTrue(varchar.is_length_limited_string)
		self.assertTrue(varchar.shadow_index_eligible)
		self.assertFalse(varchar.has_integrity_hash)
		plain_text = S.ColumnSpec("f", "text")
		self.assertFalse(plain_text.has_collation_shadow)
		self.assertFalse(plain_text.has_integrity_hash)

	def test_build_collation_shadows(self):
		text = S.ColumnSpec("f", "longtext", text_collation_shadow=True)
		self.assertEqual(TS.build_collation_shadows(text, None), {"ci": None, "like": None, "hash": None})
		empty = TS.build_collation_shadows(text, "")
		self.assertEqual(empty["ci"], C.ci_key(""))
		self.assertEqual(empty["like"], C.like_shadow(""))
		self.assertEqual(empty["hash"], hashlib.sha256(b"").hexdigest())
		strasse = TS.build_collation_shadows(text, "Straße")
		self.assertEqual(strasse["ci"], C.ci_key("Straße"))
		self.assertEqual(strasse["like"], C.like_shadow("Straße"))
		self.assertEqual(strasse["hash"], hashlib.sha256("Straße".encode("utf-8")).hexdigest())
		varchar = S.ColumnSpec("f", "varchar(140)")
		v = TS.build_collation_shadows(varchar, "x")
		self.assertNotIn("hash", v)
		self.assertEqual(v["ci"], C.ci_key("x"))
		self.assertEqual(v["like"], C.like_shadow("x"))

	def test_create_statements_shadow_ddl(self):
		marked = S.ColumnSpec("text_sh", "longtext")
		marked.text_collation_shadow = True
		stmts = S.create_statements("tabKid", [marked])
		text = "\n".join(stmts)
		self.assertIn("DEFINE FIELD `text_sh@ci` ON `tabKid`", text)
		self.assertIn("DEFINE FIELD `text_sh@like` ON `tabKid`", text)
		self.assertIn("DEFINE FIELD `text_sh@hash` ON `tabKid`", text)
		self.assertIn(
			"ASSERT IF $value = NONE OR $value = NULL THEN true ELSE $value = crypto::sha256($this.text_sh) END",
			text,
		)
		self.assertNotIn("DEFINE INDEX `text_sh`", text)  # never index-eligible
		source_stmt = next(s for s in stmts if "DEFINE FIELD `text_sh` ON" in s)
		self.assertNotIn("ASSERT", source_stmt)  # no varchar length cap on a text column
		self.assertNotIn("`text_sh@ci`", source_stmt)
		unmarked = "\n".join(S.create_statements("tabKid", [S.ColumnSpec("text", "longtext")]))
		self.assertNotIn("`text@ci`", unmarked)
		self.assertNotIn("`text@hash`", unmarked)
		# the sync hook defines the fields first and re-DEFINES them with the ASSERT after verification
		no_assert = marked.define_shadows("tabKid", hash_assertion=False)
		self.assertEqual(len(no_assert), 3)
		self.assertNotIn("ASSERT", "\n".join(no_assert))
		overwrite = marked.define_shadows("tabKid", overwrite=True)
		self.assertTrue(all("DEFINE FIELD OVERWRITE" in s for s in overwrite))