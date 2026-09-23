# SurrealDB backend for Frappe v16 (experimental fork)

## What this fork is

This is a fork of [frappe/frappe](https://github.com/frappe/frappe) (v16) that adds **SurrealDB** as a fourth
`db_type` next to `mariadb`, `postgres` and `sqlite`.

**Goal:** run Frappe, and later ERPNext v16, on SurrealDB while behaving *exactly* like they do on MariaDB 11.8 today.
MariaDB is the only reference engine. "Done" means the Frappe and ERPNext test suites on SurrealDB give the same
results as the recorded MariaDB baseline, not "roughly works".

**Why SurrealDB?** It is a multi-model engine (documents, relations and graph in one store). The longer-term interest is
using its graph capabilities on top of a fully relational ERPNext (for example following a Sales Order to its invoices
and payments). That only makes sense once the relational behaviour is provably identical, so the relational parity work
comes first and the graph work is explicitly out of scope until then.

**This is not upstream Frappe and it is not production software.** Do not report problems in this fork to the
upstream project.

## Where it stands

| Area | State |
|---|---|
| Phase 0 feasibility (record ids, value semantics, transactions, concurrency, performance) | done; conditional GO |
| P1.1 scaffolding and explicit `db_type` dispatch | implemented; the full Frappe suite was compared with the MariaDB baseline and no regression is attributable to the change |
| P1.2 site provisioning (namespace, database, scoped user; create/drop) | implemented and tested against a live server, including `bench new-site` / `drop-site` up to bootstrap |
| P1.3 driver boundary (connection, cursor, error mapping, transactions, parameters) | implemented and tested (fake SDK and live server) |
| P1.4 schema: fieldtype mapping, DDL, introspection, collation shadow keys, sequences | implemented; all 271 table-backed Frappe DocTypes apply to SurrealDB and match MariaDB's columns and indexes with no unexplained difference |
| P1.6 query translator: SELECT/INSERT/UPDATE/DELETE with exact NULL and collation semantics, aggregates, GROUP BY (incl. non-strict), expressions and functions, INNER/LEFT joins, uncorrelated sub-queries, upserts | implemented and compared with MariaDB on identical data (20 live parity tests, 24 golden tests); long-text comparisons, correlated sub-queries, right/full joins and unmapped functions fail closed |
| P1.15 allow-listed text-column collation shadows (`tabToDo.description`, `tabComment.content`) | implemented; =, !=, IN/NOT IN, emptiness, LIKE, ORDER BY, GROUP BY on those columns match MariaDB `utf8mb4_unicode_ci` exactly — stored `@ci`/`@like` keys + engine-enforced `@hash`, migrate-time sync/backfill; extend via `text_shadows.BUILTIN` + `bench migrate`; unlisted text columns keep the pre-P1.15 behavior byte-identical |
| ORM layer, locks and savepoints, raw-SQL rewrites, site install | not started |

A site cannot be installed on SurrealDB yet (the framework bootstrap is still missing). Everything not implemented **fails closed**: it raises
`SurrealDBNotImplementedError` naming the work item that owns it, and never falls through into MariaDB's or
Postgres's code path.

## How the code is organised

All SurrealDB code is isolated so that merging upstream stays cheap:

```
frappe/database/surrealdb/      the backend: database.py, connection.py (driver boundary), errors.py, setup_db.py (provisioning),
                                schema.py (fieldtypes -> DDL, introspection), collation.py (utf8mb4_unicode_ci shadow keys),
                                text_shadows.py (text-collation shadow registry + shadow primitive), shadow_migration.py
                                (migrate-time sync/backfill/integrity/readiness), values.py (value encoders),
                                translator.py (PyPika -> SurrealQL), data/ (frozen weight table)
frappe/query_builder/surrealdb_builder.py   query builder facade over the translator
frappe/tests/test_surrealdb_*.py            tests (dispatch, error classification, driver, live server)
```

Outside those paths the fork differs from upstream only by small, additive dispatch edits. Each one is a
`surrealdb` branch placed *before* the existing ones, so MariaDB, Postgres and SQLite control flow is unchanged.

Branches: `version-16` mirrors upstream, `surreal/v16` is the integration branch, and work happens on
`surreal/<chunk>-<slug>` branches.

## Key design decisions

* **Parity, never approximation.** Unsupported constructs raise; a result that differs from MariaDB is a bug.
* **Collation.** MariaDB's `utf8mb4_unicode_ci` (case, accent and PAD-space insensitive) is reproduced with an exact
  weight-key emulation (`data/unicode_ci_weights.json`, extracted once from MariaDB 11.8.5 and frozen: the collation is UCA 4.0.0 and
  never changes), because record ids, uniqueness, comparisons and ordering depend on it.
* **Concurrency.** SurrealDB detects write conflicts at commit (optimistic) instead of blocking like MariaDB. The
  plan is unit-of-work retry plus application-level locks, mapped onto Frappe's existing `QueryDeadlockError`.
* **Transactions.** WebSocket only, one interactive transaction per Frappe transaction. A statement that fails inside a
  transaction leaves its write behind in SurrealDB 3.2.4, so the driver refuses to commit such a transaction.
* **Values are always bound as parameters**, never interpolated. Date/time values are encoded by the (future) query
  translator, which knows the column type.

## Text collation shadows (P1.15)

MariaDB string collation cannot be computed inside SurrealQL (the exact `utf8mb4_unicode_ci` key exists only
in Python), so **allow-listed text-kind columns store their collation keys at write time** and queries read
them — exactly like varchar columns. Allow-listed today (option B, mechanism (a)):
`tabToDo.description`, `tabComment.content`. Unlisted text columns keep the pre-P1.15 behavior byte-identical
(inline case-fold for =/IN/emptiness; LIKE/ORDER BY/GROUP BY fail closed).

Invariant (verbatim from the P1.15 plan):

```
A text_collation_shadow column remains physically and semantically a Text
column EXCEPT for string-collation operations.

Its @ci and @like fields provide the MariaDB-compatible equivalence relation
used by =, !=, IN, NOT IN, emptiness, LIKE, ORDER BY and GROUP BY. All of these
operations MUST use the same collation representation. Mixing @ci with inline
string::lowercase() on one column is forbidden.

text_collation_shadow MUST NOT imply: varchar length limits (schema.assertion
stays is_varchar-only), varchar DDL type / meta["t"] (kind is never mutated),
index eligibility (index_field stays is_varchar-only).

Source, @ci, @like and @hash are written together, by build_collation_shadows(),
in one statement. The engine rejects any write where @hash != sha256(source).
If shadow integrity is not established for a column, queries on it RAISE.
They never fall back to the inline lowercase path.
```

* **Registry** (`text_shadows.py`): `BUILTIN` + `register()`, legal only before `freeze()` (frozen at
  driver connect, before any schema sync or query). `ColumnSpec.text_collation_shadow` is set from the
  registry; collation questions go through `has_collation_shadow`, while length caps, the DDL type and
  index eligibility stay varchar-only.
* **Write path**: every write calls `build_collation_shadows(spec, value)` after value coercion and stores
  the source plus `@ci` (collation key), `@like` (LIKE shadow) and `@hash` (sha-256 hex; NONE for NULL) in
  one statement. NULL sources follow the existing varchar shadow convention (absent fields).
* **Engine-enforced `@hash`**: the field is defined with
  `ASSERT $value = crypto::sha256($this.<col>)` and a guarded `DEFINE EVENT` rejects bypass `UPDATE`s of
  the source, `CREATE` without `@hash`, and `@hash` removal — a raw write that changes the source without
  its shadows is **rejected by the engine at write time**. Raw SurrealQL writes to a shadowed column are
  therefore not supported; use the ORM/qb. (A deliberate forgery that sets the source *and* a matching
  `@hash` together still passes the engine — it requires intent; ops policy, not the driver, covers it.
  `shadow_migration.health()` / `count_invalid_shadows` detect stale shadows Python-side and
  `bench migrate` repairs them offline.)
* **Migration** (`shadow_migration.py`): `bench migrate` runs `sync_all_table_shadows()` — per column:
  define the shadow fields (without the ASSERT), backfill invalid rows in restartable batches (with a
  mandatory no-progress guard), verify `count_invalid_shadows == 0`, re-define `@hash` with the ASSERT,
  then write the version marker (`meta["csv"]`). Tables absent on a site are skipped silently. Online
  (live-traffic) backfill is unsupported.
* **Readiness**: a shadowed column is queryable only when synced + backfilled + version-current
  (`shadow_ready`); otherwise every string-collation operation on it raises
  `unsupported: <col> collation shadows not ready (run bench migrate)` — never an inline fallback.
* **Extending the allow-list**: add one `TextCollationShadow(table, column)` to `BUILTIN` and run
  `bench migrate`.

## Trying it

Requirements: SurrealDB 3.2.4 (RocksDB) and the Python SDK (`pip install "frappe[surrealdb]"`, currently
`surrealdb~=2.0.0`). The SDK is imported lazily, so MariaDB sites never need it.

Site configuration keys: `db_type: "surrealdb"`, `db_host`, `db_port` (default 8000), `db_namespace` (default
`frappe`), plus the usual `db_name`, `db_user`, `db_password`. `root_login` / `root_password` are used only while
provisioning or dropping a site.

```bash
# unit tests (no server needed)
bench --site <site> run-tests --module frappe.tests.test_surrealdb_dispatch
bench --site <site> run-tests --module frappe.tests.test_surrealdb_errors
bench --site <site> run-tests --module frappe.tests.test_surrealdb_driver
# live tests: set SURREAL_ENDPOINT, SURREAL_USER, SURREAL_PASS for a scratch SurrealDB server
bench --site <site> run-tests --module frappe.tests.test_surrealdb_live
```

## Working rules

* No test is edited to make it pass; the only allowed edit is the mechanical conversion of raw MariaDB SQL embedded in
  a test.
* No credentials, client data or site configuration in any commit.
* Design notes, measurements and the plan live in a separate private repository, not here.

Licensed under the same terms as Frappe (MIT).
