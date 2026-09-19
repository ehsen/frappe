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
| P1.6 query translator: single-table SELECT (exact NULL and collation semantics), aggregates, GROUP BY, INSERT/UPDATE/DELETE | implemented and compared with MariaDB on identical data (167 predicates, orderings, writes, 41 aggregate cases); joins, functions and subqueries fail closed and are next |
| ORM layer, locks and savepoints, raw-SQL rewrites, site install | not started |

A site cannot be installed on SurrealDB yet (the framework bootstrap is still missing). Everything not implemented **fails closed**: it raises
`SurrealDBNotImplementedError` naming the work item that owns it, and never falls through into MariaDB's or
Postgres's code path.

## How the code is organised

All SurrealDB code is isolated so that merging upstream stays cheap:

```
frappe/database/surrealdb/      the backend: database.py, connection.py (driver boundary), errors.py, setup_db.py (provisioning),
                                schema.py (fieldtypes -> DDL, introspection), collation.py (utf8mb4_unicode_ci shadow keys),
                                values.py (value encoders), translator.py (PyPika -> SurrealQL), data/ (frozen weight table)
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
