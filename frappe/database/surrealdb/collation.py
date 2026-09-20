"""Exact emulation of MariaDB's `utf8mb4_unicode_ci` (the collation Frappe creates every table with, ADR 0001).

SurrealDB compares strings by code point, MariaDB by Unicode weights: case-, accent- and PAD-space-insensitive, with
expansions (`ß` = `ss`), ignorable characters, and every supplementary-plane character weighing `FFFD`. The backend
therefore stores *shadow keys* next to string columns and compares those instead:

* `ci_key(s)`      order-preserving key. `ci_key(a) == ci_key(b)` <=> MariaDB `a = b`; comparing keys as strings gives
                   MariaDB's `ORDER BY` order. Serves equality, IN, UNIQUE, range and ORDER BY (`<col>@ci`).
* `like_shadow(s)` character-delimited, untrimmed weights; `like_regex(p)` turns a LIKE pattern into a regex over it.
                   Needed because `ci_key` flattens expansions and trims PAD spaces (`<col>@like`).
* `record_id(n)`   record id of a document `name`: a pure function of the collation key, so collation-equal names map to
                   the same id and `CREATE` of a colliding name fails exactly where MariaDB raises 1062.

The weight table (`data/unicode_ci_weights.json`) was extracted from MariaDB 11.8.5 by
`spike/P0.3-semantics/gen_unicode_ci_weights.py` in the design repository and is *frozen*: `utf8mb4_unicode_ci` is UCA
4.0.0 and never changes, and changing this file requires a data migration. Validated against a live MariaDB in
`frappe/tests/test_surrealdb_collation.py`.
"""

import hashlib
import json
import os
import re
from functools import lru_cache

_TABLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "unicode_ci_weights.json")

SPACE_WEIGHT = "0209"

# MariaDB compares as if every string were followed by an infinite run of spaces. A finite key cannot express that, so
# the key ends with a few pad units: enough for every realistic string (a strict-prefix pair whose extra characters
# are ignorable/control characters after fewer than ORDER_PAD_UNITS spaces). Measured in the collation tests.
ORDER_PAD_UNITS = 4


@lru_cache(maxsize=1)
def _explicit() -> dict[int, str]:
	with open(_TABLE) as f:
		return {int(k): v for k, v in json.load(f)["explicit"].items()}


def _implicit(cp: int) -> str:
	base = 0xFB40 if 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF else 0xFB80
	return "%04X%04X" % (base + (cp >> 15), (cp & 0x7FFF) | 0x8000)


def weight(cp: int) -> str:
	"""Weight units (4 hex digits each) of one code point."""
	if cp > 0xFFFF:
		return "FFFD"
	w = _explicit().get(cp)
	return w if w is not None else _implicit(cp)


def key(s: str) -> str:
	"""Hex weight string of `s` after PAD SPACE trimming.

	PAD SPACE works on *weights*: every trailing unit equal to the space weight is dropped, so trailing U+0020,
	U+00A0 and U+3000 all vanish (measured against MariaDB 11.8.5)."""
	w = "".join(weight(ord(c)) for c in s)
	end = len(w)
	while end >= 4 and w[end - 4 : end] == SPACE_WEIGHT:
		end -= 4
	return w[:end]


def units(s: str) -> list[str]:
	k = key(s)
	return [k[i : i + 4] for i in range(0, len(k), 4)]


def compare_ci(a: str, b: str) -> int:
	"""Exact three-way comparison under MariaDB rules (missing units count as the space weight)."""
	ua, ub = units(a), units(b)
	for i in range(max(len(ua), len(ub))):
		wa = ua[i] if i < len(ua) else SPACE_WEIGHT
		wb = ub[i] if i < len(ub) else SPACE_WEIGHT
		if wa != wb:
			return -1 if wa < wb else 1
	return 0


@lru_cache(maxsize=1)
def _rank() -> dict[int, int]:
	"""Dense, order-preserving map weight unit -> character (Latin/ASCII units land on 2-byte UTF-8 characters)."""
	found = {0x0209}
	for cp in range(0x10000):
		if 0xD800 <= cp <= 0xDFFF:
			continue
		w = weight(cp)
		found.update(int(w[i : i + 4], 16) for i in range(0, len(w), 4))
	ranks = {u: 0x80 + r for r, u in enumerate(sorted(found))}
	assert max(ranks.values()) < 0xD800
	return ranks


def ci_key(s: str) -> str:
	"""Shadow key stored in `<col>@ci` (see module docstring)."""
	rank = _rank()
	k = key(s)
	body = "".join(chr(rank[int(k[i : i + 4], 16)]) for i in range(0, len(k), 4))
	return body + chr(rank[0x0209]) * ORDER_PAD_UNITS


# --- record ids (ADR 0001, scheme C) ------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _unit_to_ascii() -> dict[str, str]:
	table: dict[str, str] = {}
	for c in range(0x20, 0x7F):
		ch = chr(c)
		if (
			ch == "~" or ch.isupper()
		):  # '~' reserved for the hashed namespace; upper case shares lower case's weight
			continue
		table.setdefault(weight(c), ch)
	return table


def readable_rep(name: str) -> str | None:
	table = _unit_to_ascii()
	out = []
	for u in units(name):
		ch = table.get(u)
		if ch is None:
			return None
		out.append(ch)
	return "".join(out) or None


def record_id(name: str) -> str:
	"""Readable canonical form of the collation key when every unit is decodable, else `~` + blake2b-128 of the key."""
	return readable_rep(name) or "~" + hashlib.blake2b(key(name).encode(), digest_size=16).hexdigest()


# --- LIKE ---------------------------------------------------------------------------------------------------------
def like_shadow(value: str) -> str:
	"""Character-delimited weights: every character keeps its boundary and trailing spaces stay significant."""
	return "".join(weight(ord(c)) + ";" for c in value)


def like_regex(pattern: str, escape: str = "\\") -> str:
	"""Regex over `like_shadow` values equivalent to `col LIKE pattern` (`%`, `_`, `escape`)."""
	parts = ["^"]
	i = 0
	while i < len(pattern):
		c = pattern[i]
		if c == escape and i + 1 < len(pattern):
			i += 1
			parts.append(re.escape(weight(ord(pattern[i])) + ";"))
		elif c == "%":
			parts.append("(?:[0-9A-F]*;)*")
		elif c == "_":
			parts.append("[0-9A-F]*;")
		else:
			parts.append(re.escape(weight(ord(c)) + ";"))
		i += 1
	parts.append("$")
	return "".join(parts)


@lru_cache(maxsize=1)
def empty_pattern() -> str:
	"""Regex (for `string::matches`) that matches exactly the strings MariaDB compares equal to `''`.

	Long-text columns have no collation shadow, but the one comparison Frappe makes on them all the time - `col = ''` /
	`col <> ''` ("is not set" / "is set") - needs none: a string equals `''` under PAD SPACE when every character weighs
	nothing (ignorable: control characters, zero-width characters) or only the space weight (U+0020, U+00A0, U+2000..)."""
	cps = sorted(
		cp
		for cp, w in _explicit().items()
		if not w or all(w[i : i + 4] == SPACE_WEIGHT for i in range(0, len(w), 4))
	)
	ranges: list[list[int]] = []
	for cp in cps:
		if ranges and cp == ranges[-1][1] + 1:
			ranges[-1][1] = cp
		else:
			ranges.append([cp, cp])
	body = "".join(f"\\x{{{a:X}}}" if a == b else f"\\x{{{a:X}}}-\\x{{{b:X}}}" for a, b in ranges)
	return f"^[{body}]*$"


def equals_empty(value: str) -> bool:
	return ci_key(value) == ci_key("")
