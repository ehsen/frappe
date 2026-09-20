# ruff: noqa: RUF001
"""The shadow keys must agree with MariaDB's `utf8mb4_unicode_ci`. Runs against the site's MariaDB (no SurrealDB needed)."""

import random
import re
import unittest

import frappe
from frappe.database.surrealdb import collation as C
from frappe.tests import UnitTestCase

# exotic pool: case, accents, expansions, ignorable/control characters, spaces, CJK, emoji, compatibility forms
POOL = (
	list("abcxyzABCXYZ019 -_./@:\x01\t")
	+ list("éÉèêëáàäãåçñöøüÿšžłđÆæŒœßÞþðĲĳǅǆ")
	+ list("日本語한글ก")
	+ ["́", "​", "­", " ", "　", "😀", "😁", "𝒜", "\U00020000", "ǈ", "Ⅷ", "ﬁ", "㎏", "①", "ａ", "Ａ"]
)

EDGE_PAIRS = [
	("straße", "STRASSE"),
	("résumé", "RESUME"),
	("Apple", "apple"),
	("a", "a  "),
	("a", "a "),
	("a", "a\x01"),
	("a", "a \x01"),
	("a", "a  \x01"),
	("", "\t"),
	("", " "),
	("x​y", "xy"),
	("😀", "😁"),
	("a" * 1000, "a" * 1000 + "\x01"),
	("ab", "a b"),
	("a b", "a  b"),
	("ǅ", "dž"),
	("ﬁne", "fine"),
	("Ⅷ", "viii"),
]


def maria(query: str, rows: list[tuple]) -> dict[int, tuple]:
	"""Run `query` (a template with one `SELECT %s n, ...` per row) on MariaDB; returns n -> result columns."""
	out = {}
	for start in range(0, len(rows), 400):
		part = rows[start : start + 400]
		template = " UNION ALL ".join([query] * len(part))
		args = [v for i, row in enumerate(part, start) for v in (i, *row)]
		for r in frappe.db.sql(f"SELECT * FROM ({template}) t", args):
			out[r[0]] = r[1:]
	return out


def random_strings(rng: random.Random, n: int) -> list[str]:
	return ["".join(rng.choice(POOL) for _ in range(rng.randint(0, 8))) for _ in range(n)]


class TestSurrealDBCollation(UnitTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if frappe.db.db_type != "mariadb":
			raise unittest.SkipTest("compares against MariaDB")

	def test_equality_and_ordering_agree_with_mariadb(self):
		rng = random.Random(20260920)
		strings = random_strings(rng, 1500)
		pairs = [(rng.choice(strings), rng.choice(strings)) for _ in range(2500)]
		pairs += [(s, s.swapcase()) for s in rng.sample(strings, 800)]
		pairs += [(s, s + " ") for s in rng.sample(strings, 500)]
		pairs += EDGE_PAIRS
		expected = maria(
			"SELECT %s n, (%s COLLATE utf8mb4_unicode_ci = %s COLLATE utf8mb4_unicode_ci) eq, "
			"(%s COLLATE utf8mb4_unicode_ci < %s COLLATE utf8mb4_unicode_ci) lt",
			[(a, b, a, b) for a, b in pairs],
		)
		bad = []
		for i, (a, b) in enumerate(pairs):
			eq, lt = expected[i]
			ka, kb = C.ci_key(a), C.ci_key(b)
			if (ka == kb) != bool(eq) or (ka < kb) != bool(lt):
				bad.append((a, b, "maria", eq, lt, "key", ka == kb, ka < kb))
		self.assertEqual(bad[:5], [], f"{len(bad)} of {len(pairs)} pairs disagree")

	def test_exact_comparator_agrees_with_mariadb(self):
		"""`compare_ci` (infinite space padding) is the specification the padded key approximates."""
		rng = random.Random(7)
		strings = random_strings(rng, 600)
		pairs = [(rng.choice(strings), rng.choice(strings)) for _ in range(1200)] + EDGE_PAIRS
		expected = maria(
			"SELECT %s n, (%s COLLATE utf8mb4_unicode_ci < %s COLLATE utf8mb4_unicode_ci) lt, "
			"(%s COLLATE utf8mb4_unicode_ci = %s COLLATE utf8mb4_unicode_ci) eq",
			[(a, b, a, b) for a, b in pairs],
		)
		for i, (a, b) in enumerate(pairs):
			lt, eq = expected[i]
			got = C.compare_ci(a, b)
			self.assertEqual((got < 0, got == 0), (bool(lt), bool(eq)), (a, b))

	def test_like_shadow_agrees_with_mariadb(self):
		rng = random.Random(11)
		values = [
			*random_strings(rng, 250),
			"",
			"50% off",
			"a_b",
			"a\\b",
			"Apple pie",
			"straße",
			"日本 ",
			"a  ",
		]
		wild = ["%", "_"]
		patterns = [
			"".join(rng.choice(POOL + wild + wild) for _ in range(rng.randint(0, 6))) for _ in range(250)
		]
		patterns += [
			"app%",
			"%PLE",
			"%pp%",
			"a_c",
			"50\\%%",
			"a\\_b",
			"%é%",
			"strass_",
			"%ß",
			"x_y",
			"%\\\\%",
			"a",
		]
		cases = [(rng.choice(values), rng.choice(patterns)) for _ in range(2500)]
		cases += [(v, p) for v in values[-8:] for p in patterns[-12:]]
		expected = maria(
			"SELECT %s n, (%s COLLATE utf8mb4_unicode_ci LIKE %s COLLATE utf8mb4_unicode_ci) hit",
			[(v, p) for v, p in cases],
		)
		bad = [
			(v, p)
			for i, (v, p) in enumerate(cases)
			if bool(re.search(C.like_regex(p), C.like_shadow(v))) != bool(expected[i][0])
		]
		self.assertEqual(bad[:5], [], f"{len(bad)} of {len(cases)} LIKE cases disagree")

	def test_record_ids(self):
		equal = [
			("Administrator", "ADMINISTRATOR  "),
			("résumé", "RESUME"),
			("straße", "strasse"),
			("😀", "😁"),
		]
		for a, b in equal:
			self.assertEqual(C.record_id(a), C.record_id(b), (a, b))
		self.assertNotEqual(C.record_id("abc"), C.record_id("abd"))
		self.assertEqual(C.record_id("Administrator@Example.com"), "administrator@example.com")
		self.assertEqual(C.record_id("SAL-ORD-2024-00001"), "sal-ord-2024-00001")
		hashed = C.record_id("日本語")
		self.assertTrue(hashed.startswith("~") and len(hashed) == 33)
		self.assertEqual(C.record_id("Tab\tName"), C.record_id("tab\tname"))
		# the two id namespaces cannot collide: readable ids never start with '~'
		self.assertFalse(C.record_id("~abc").startswith("abc"))

	def test_pad_units_are_part_of_every_key(self):
		pad = C.ci_key("")
		self.assertEqual(len(pad), C.ORDER_PAD_UNITS)
		self.assertEqual(C.ci_key("a  "), C.ci_key("a"))
