"""Migration machinery for the text-collation shadows (P1.15 phase 4): the per-table sync hook, the
chunked backfill, the integrity check, the readiness marker and the engine-side enforcement event.

Ordering per column (P0.7): DEFINE the shadow fields WITHOUT the hash ASSERT -> backfill -> verify
(count_invalid_shadows == 0, else the migration fails) -> re-DEFINE `@hash` WITH the ASSERT -> write the
`csv` version marker on the source field's COMMENT -> DEFINE the guarded enforcement event. All steps
complete for a table before the sync moves on (inside model sync, before post-sync patches).

Two entry points drive the sync. `SurrealDBTable.sync()` covers the model-sync path (fresh table
creation and DocType-JSON changes). `sync_all_table_shadows()` is the migrate-time hook (wired at the
top of `Migrate.post_schema_updates`): the model sync hash-skips unchanged DocType files, so a newly
allow-listed column would otherwise reach migrate with NO shadows while queries already mark it
(measured on p20_sdb: the tabComment BUILTIN entry activated nothing and every Comment INSERT failed
with 1054 "Unknown column 'content@ci'").

The backfill selects rows that are *still invalid* (no OFFSET), so it is restartable and idempotent, and
it carries a mandatory no-progress guard: a row that cannot be made valid would otherwise loop forever.
Backfill runs offline (inside `bench migrate`); online backfill is unsupported (see OPEN-ITEMS §C)."""

import json
import re

import frappe
from frappe.database.surrealdb import text_shadows as TS
from frappe.database.surrealdb.errors import SurrealDBProgrammingError
from frappe.database.surrealdb.schema import (
	SHADOW_CI,
	SHADOW_HASH,
	SHADOW_LIKE,
	parse_field_meta,
	physical,
	quote,
	quote_table,
	surql_string,
	table_schema,
)

BATCH = 250

_EVENT_PREFIX = "p115_"
_COMMENT_TOKEN = re.compile(r"COMMENT (?:'[^']*'|\"(?:[^\"\\]|\\.)*\")")


class ShadowBackfillStalled(Exception):
	"""The backfill stopped making progress (a row cannot be made valid)."""


def invalid_predicate(spec) -> str:
	"""WHERE clause matching rows whose stored shadows or @hash are missing. The engine stores real NULL
	for SQL NULL (P0.8-revised): a present-but-NULL source is legal, and then the shadows must ALSO be
	present (as NULL) - a field of type `string | null` that is *absent* (NONE) fails the type check on
	any later UPDATE of the row (measured, probe v3) - so the backfill must materialise NULL shadows.
	For a present source the shadows must be non-null strings and @hash must match."""
	stored = physical(spec.name)
	col = quote(stored)
	keys = {suf: quote(stored + suf) for suf in (SHADOW_CI, SHADOW_LIKE, SHADOW_HASH)}
	missing = (
		f"({keys[SHADOW_CI]} = NONE OR {keys[SHADOW_CI]} = NULL "
		f"OR {keys[SHADOW_LIKE]} = NONE OR {keys[SHADOW_LIKE]} = NULL "
		f"OR {keys[SHADOW_HASH]} = NONE OR {keys[SHADOW_HASH]} = NULL "
		f"OR {keys[SHADOW_HASH]} != crypto::sha256({col}))"
	)
	absent = " OR ".join(f"{keys[suf]} = NONE" for suf in (SHADOW_CI, SHADOW_LIKE, SHADOW_HASH))
	return f"IF ({col} != NONE AND {col} != NULL) THEN {missing} ELSE {absent} END"


def count_invalid_shadows(table: str, spec, db=None) -> int:
	"""Rows of `table` violating the stored-shadow invariant (in-engine; @ci/@like staleness is *not*
	verifiable here - ci_key/like_shadow are Python-only, see health())."""
	db = db or frappe.db
	rows = db.sql(
		f"SELECT count() AS n FROM {quote_table(table)} WHERE {invalid_predicate(spec)} GROUP ALL /*cols:n*/"
	)
	return int(rows[0][0]) if rows and rows[0][0] else 0


def _field_present(info_fields: dict, stored: str, suffix: str) -> bool:
	return (
		f"`{stored}{suffix}`" in info_fields
		or f"{stored}{suffix}`" in info_fields
		or f"{stored}{suffix}" in info_fields
	)


def _update_row(db, table: str, spec, rid: str, value):
	"""Write one row's shadows (module-level so tests can inject failures)."""
	shadows = TS.build_collation_shadows(spec, value)
	sets = ", ".join(
		f"{quote(physical(spec.name) + suf)} = ${'v' + suf[1:]}" for suf in (SHADOW_CI, SHADOW_LIKE, SHADOW_HASH)
	)
	params = {"tb": table, "rid": rid}
	for suf in (SHADOW_CI, SHADOW_LIKE, SHADOW_HASH):
		params["v" + suf[1:]] = shadows[suf[1:]]
	db.sql(f"UPDATE type::record($tb, $rid) SET {sets}", params)


def backfill_column(table: str, spec, db=None, force: bool = False) -> int:
	"""Bring every row's stored shadows back in line. `force` recomputes every row (version bump); the
	default only touches rows matching the invalid predicate. Returns the number of rows written."""
	db = db or frappe.db
	col = quote(physical(spec.name))
	written = 0
	last_remaining = None
	while True:
		if force:
			rows = db.sql(f"SELECT record::id(id) AS rid, {col} AS v FROM {quote_table(table)} /*cols:rid,v*/")
		else:
			rows = db.sql(
				f"SELECT record::id(id) AS rid, {col} AS v FROM {quote_table(table)} "
				f"WHERE {invalid_predicate(spec)} LIMIT {BATCH} /*cols:rid,v*/"
			)
		if not rows:
			break
		for rid, value in rows:
			_update_row(db, table, spec, rid, value)
			written += 1
		remaining = count_invalid_shadows(table, spec, db)
		if last_remaining is not None and remaining >= last_remaining and remaining > 0:
			sample = db.sql(
				f"SELECT record::id(id) AS rid FROM {quote_table(table)} "
				f"WHERE {invalid_predicate(spec)} LIMIT 3 /*cols:rid*/"
			)
			raise ShadowBackfillStalled(
				f"backfill of {table}.{spec.name} made no progress ({remaining} rows left; "
				f"sample: {[r[0] for r in sample]})"
			)
		last_remaining = remaining
		if force:
			break  # one full pass recomputed everything; the verify below judges the outcome
	return written

def _define_missing(table: str, spec, db, missing: list[str]) -> None:
	for stmt in spec.define_shadows(table, hash_assertion=False):
		if any(quote(physical(spec.name) + suf) in stmt for suf in missing):
			db.sql_ddl(stmt)


def _redefine_with_assert(table: str, spec, db) -> None:
	for stmt in spec.define_shadows(table, overwrite=True):
		if quote(physical(spec.name) + SHADOW_HASH) in stmt:
			db.sql_ddl(stmt)
			return
	raise SurrealDBProgrammingError(0, f"{spec.name} carries no hash statement (contract violation)")


def _write_marker(table: str, spec, db, fields: dict) -> None:
	stored = physical(spec.name)
	definition = fields.get(f"`{stored}`") or fields.get(stored)
	if not definition:
		return
	meta = parse_field_meta(definition) or {}
	if meta.get("csv") == TS.COLLATION_SHADOW_VERSION:
		return
	meta["csv"] = TS.COLLATION_SHADOW_VERSION
	new_def = _COMMENT_TOKEN.sub(
		f"COMMENT {surql_string(json.dumps(meta, separators=(',', ':'), ensure_ascii=False))}",
		definition,
		count=1,
	)
	if " OVERWRITE " not in new_def.split("COMMENT")[0]:
		# the INFO definition is a plain DEFINE FIELD; re-defining an existing field needs OVERWRITE
		new_def = new_def.replace("DEFINE FIELD ", "DEFINE FIELD OVERWRITE ", 1)
	db.sql_ddl(new_def)


def _ensure_event(table: str, spec, db) -> None:
	stored = physical(spec.name)
	hs = quote(stored + SHADOW_HASH)
	name = _event_name(spec)
	db.sql_ddl(f"REMOVE EVENT IF EXISTS {name} ON {quote_table(table)}")
	db.sql_ddl(
		f"DEFINE EVENT {name} ON {quote_table(table)} WHEN $event IN ['CREATE', 'UPDATE'] THEN {{ "
		f"IF $after.{stored} = NONE OR $after.{stored} = NULL {{ "
		f"IF {hs} != NONE AND {hs} != NULL {{ THROW 'stale shadow' }} }} "
		f"ELSE {{ "
		f"IF {hs} = NONE OR {hs} = NULL {{ THROW 'stale shadow' }} "
		f"ELSE {{ IF {hs} != crypto::sha256($after.{stored}) {{ THROW 'stale shadow' }} }} }} }};"
	)


def _event_name(spec) -> str:
	return _EVENT_PREFIX + spec.name


def sync_column_shadows(table: str, spec, db=None) -> None:
	"""The six-step migration for one shadowed text column (module docstring). No-op when the fields
	exist, the stored version matches and the invariant holds."""
	db = db or frappe.db
	stored = physical(spec.name)
	fields = db._info(f"INFO FOR TABLE {quote_table(table)}").get("fields") or {}
	missing = [suf for suf in (SHADOW_CI, SHADOW_LIKE, SHADOW_HASH) if not _field_present(fields, stored, suf)]
	meta = parse_field_meta(fields.get(f"`{stored}`") or fields.get(stored) or "") or {}
	if not missing and meta.get("csv") == TS.COLLATION_SHADOW_VERSION:
		remaining = count_invalid_shadows(table, spec, db)
		if remaining:
			raise ShadowBackfillStalled(
				f"{table}.{spec.name}: {remaining} rows violate the shadow invariant outside migrate "
				"(run frappe.database.surrealdb.shadow_migration.health for details)"
			)
		_ensure_event(table, spec, db)
		return
	_define_missing(table, spec, db, missing)
	if meta.get("csv") is not None and meta.get("csv") != TS.COLLATION_SHADOW_VERSION:
		backfill_column(table, spec, db, force=True)  # version bump: recompute every row
	else:
		backfill_column(table, spec, db)
	remaining = count_invalid_shadows(table, spec, db)
	if remaining:
		raise ShadowBackfillStalled(f"{table}.{spec.name}: {remaining} rows still invalid after backfill")
	_redefine_with_assert(table, spec, db)
	_write_marker(table, spec, db, fields)
	_ensure_event(table, spec, db)


def sync_table_shadows(table: str, specs: list, db=None) -> None:
	"""Sync hook entry point: migrate every allow-listed text column of `table`."""
	db = db or frappe.db
	for spec in specs:
		if getattr(spec, "has_integrity_hash", False):
			sync_column_shadows(table, spec, db)


def sync_all_table_shadows(db=None) -> list[str]:
	"""Migrate-time, registry-driven sync of EVERY allow-listed column, independent of DocType-JSON
	changes (the model sync hash-skips unchanged files - see the module docstring). Wired at the top
	of `Migrate.post_schema_updates`, before sync_jobs. Tables absent on the site are skipped, so a
	fresh test site or a partial install never blocks; returns the synced "table: columns" entries."""
	db = db or frappe.db
	by_table: dict[str, list[str]] = {}
	for shadow in sorted(TS.all_shadowed(), key=lambda s: (s.table, s.column)):
		by_table.setdefault(shadow.table, []).append(shadow.column)
	synced = []
	for table in sorted(by_table):
		try:
			schema = table_schema(table, db=db)
		except SurrealDBProgrammingError:
			continue  # table not present on this site
		specs = [schema.column(column) for column in by_table[table]]
		specs = [spec for spec in specs if spec is not None and spec.has_integrity_hash]
		if specs:
			sync_table_shadows(table, specs, db)
			synced.append(f"{table}: {', '.join(spec.name for spec in specs)}")
	return synced


def _python_side_mismatches(table: str, spec, db) -> int:
	"""Rows whose @ci/@like differ from the Python-computed keys of the stored value (invisible to the
	in-engine predicate). Keyset-paged over record ids."""
	from frappe.database.surrealdb import collation as C

	stored = physical(spec.name)
	mismatch = 0
	last = None
	while True:
		sql = (
			f"SELECT record::id(id) AS rid, {quote(stored)} AS v, "
			f"{quote(stored + SHADOW_CI)} AS ci, {quote(stored + SHADOW_LIKE)} AS lk "
			f"FROM {quote_table(table)}"
			+ (" WHERE id > $last" if last else "")
			+ " ORDER BY id LIMIT 500 /*cols:rid,v,ci,lk*/"
		)
		rows = db.sql(page := sql, {"last": last} if last else {})
		if not rows:
			break
		for rid, value, ci, lk in rows:
			if value is None:  # SQL NULL source: the shadows are present, as NULL
				if ci is not None or lk is not None:
					mismatch += 1
			elif ci != C.ci_key(value) or lk != C.like_shadow(value):
				mismatch += 1
		last = rows[-1][0]
	return mismatch


def health() -> dict:
	"""`bench execute frappe.database.surrealdb.shadow_migration.health` — per allow-listed column:
	the in-engine invalid-row count and the Python-side @ci/@like mismatch count. Ops runbook: anything
	non-zero means raw writes bypassed the driver; re-run `bench migrate` to repair (offline)."""
	out = {}
	db = frappe.db
	for shadow in sorted(TS.all_shadowed(), key=lambda s: (s.table, s.column)):
		key = f"{shadow.table}.{shadow.column}"
		try:
			schema = table_schema(shadow.table, db=db)
		except SurrealDBProgrammingError:
			out[key] = {"status": "table missing on this site"}
			continue
		spec = schema.column(shadow.column)
		if spec is None or not spec.has_integrity_hash:
			out[key] = {"status": "not a shadowed column on this site"}
			continue
		out[key] = {
			"invalid_engine": count_invalid_shadows(shadow.table, spec, db),
			"mismatch_python": _python_side_mismatches(shadow.table, spec, db),
		}
	return out