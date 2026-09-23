"""Allow-list of text-kind columns that carry MariaDB-collation shadow fields (P1.15).

A text_collation_shadow column remains physically and semantically a Text
column EXCEPT for string-collation operations.

Its @ci and @like fields provide the MariaDB-compatible equivalence relation
used by =, !=, IN, NOT IN, emptiness, LIKE, ORDER BY and GROUP BY. All of these
operations MUST use the same collation representation. Mixing @ci with inline
string::lowercase() on one column is forbidden.

text_collation_shadow MUST NOT imply:
  - varchar length limits           (schema.assertion stays is_varchar-only)
  - varchar DDL type / meta["t"]     (kind is never mutated)
  - index eligibility                (index_field stays is_varchar-only)

Source, @ci, @like and @hash are written together, by build_collation_shadows(),
in one statement. The engine rejects any write where @hash != sha256(source).
If shadow integrity is not established for a column, queries on it RAISE.
They never fall back to the inline lowercase path.

The registry is frozen on the first driver connection: `register` is legal only
before that (apps register at boot, after which the allow-list is closed).
`BUILTIN` carries the framework's own allow-list entries.
"""

import hashlib
from dataclasses import dataclass

from frappe.database.surrealdb import collation

COLLATION_SHADOW_VERSION = 1  # bump when ci_key/like_shadow output changes -> forces re-backfill

SHADOW_HASH = "@hash"


@dataclass(frozen=True)
class TextCollationShadow:
	table: str   # e.g. "tabToDo"
	column: str  # e.g. "description"


BUILTIN: frozenset[TextCollationShadow] = frozenset()

_registered: set[TextCollationShadow] = set()
_frozen: bool = False


def register(table: str, column: str) -> None:
	"""Add (table, column) to the allow-list. Legal only before freeze()."""
	if _frozen:
		raise RuntimeError("text-collation-shadow registry is frozen; register before the driver connects")
	_registered.add(TextCollationShadow(table, column))


def freeze() -> None:
	"""Close the registry. Called when the driver first connects (idempotent)."""
	global _frozen
	_frozen = True


def is_shadowed(table: str, column: str) -> bool:
	"""True when (table, column) is allow-listed. Raises before freeze(): consulting an
	open registry would let DDL/queries run against an allow-list that can still grow."""
	if not _frozen:
		raise RuntimeError("text-collation-shadow registry is not frozen yet; call freeze() before any schema sync or query")
	return TextCollationShadow(table, column) in BUILTIN or TextCollationShadow(table, column) in _registered


def all_shadowed() -> frozenset[TextCollationShadow]:
	if not _frozen:
		raise RuntimeError("text-collation-shadow registry is not frozen yet")
	return frozenset(set(BUILTIN) | _registered)


def _reset_for_tests() -> None:
	"""TEST-ONLY: restore the pristine (unfrozen, unregistered) state. Never call in production."""
	global _registered, _frozen
	_registered = set()
	_frozen = False


# --- shadow primitive -----------------------------------------------------------------------------------------------
def source_hash(value: str) -> str:
	"""SHA-256 hex of the UTF-8 encoding (P0.4: byte-identical to SurrealDB's crypto::sha256)."""
	return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_collation_shadows(spec, value) -> dict[str, object]:
	"""Collation keys for one column value, keyed by shadow suffix ("ci"/"like"/"hash").

	Callers attach their own stored base name (`physical(spec.name) + suffix`), so this module
	stays decoupled from schema.py's identifier rules. `value` must already be the value that
	will be stored (the caller encodes first, then calls this — hash and keys must describe the
	persisted value). A NULL source yields all-NONE shadows (P0.8 convention: the caller writes
	them, which removes the fields). The "hash" entry exists only when spec.has_integrity_hash
	(text shadows; varchar shadows stay hash-free until the §C follow-up).
	"""
	if value is None:
		out: dict[str, object] = {"ci": None, "like": None}
	else:
		out = {"ci": collation.ci_key(value), "like": collation.like_shadow(value)}
	if getattr(spec, "has_integrity_hash", False):
		out["hash"] = None if value is None else source_hash(value)
	return out