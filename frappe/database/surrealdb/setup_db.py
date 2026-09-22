"""Site provisioning for SurrealDB (chunk P1.2).

Mapping of MariaDB concepts: the site's `db_name` is a SurrealDB *database* inside the bench's *namespace*
(`db_namespace`, default `frappe`); `db_user`/`db_password` become a database-level user with the OWNER role, which
can define tables but cannot touch other databases or server-level objects (measured, spike/P1.3-driver). The root
credentials come from the `surrealdb_root_login`/`surrealdb_root_password` conf keys and are used only here and never
stored in a site; SurrealDB is a separate server, so the MariaDB-style `root_login`/`root_password` (and the
`--db-root-username`/`--db-root-password` bench flags, which carry MariaDB values) are only a fallback when those keys
are absent — measured 2026-09-21: `root` + the MariaDB root password is rejected by the SurrealDB server, which is how
the full-suite run's temp sites ended up with a database but no `DEFINE USER` and poisoned their shard with 1045s.

Identifiers cannot be bound in SurrealQL, so they are validated against a strict pattern before being embedded.
The password cannot be bound in `DEFINE USER` either (measured), so it is hashed server-side with a *bound*
`crypto::argon2::generate($pw)` and only the validated hash literal is embedded (`PASSHASH`).
"""

import json
import re
import sys

import frappe
from frappe.database.surrealdb.connection import ConnectionParams, SurrealConnection
from frappe.database.surrealdb.errors import SurrealDBProgrammingError, unsupported

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_ARGON2_HASH = re.compile(r"^\$argon2[a-z0-9]*\$[A-Za-z0-9$=,+/.\-]+$")


def _identifier(value: str, what: str) -> str:
	if not isinstance(value, str) or not _IDENTIFIER.match(value):
		raise SurrealDBProgrammingError(
			0, f"Invalid {what} {value!r}: only letters, digits and underscore (max 64) are allowed."
		)
	return value


def _namespace() -> str:
	from frappe.database.surrealdb.database import get_namespace

	return _identifier(get_namespace(), "namespace")


def get_root_connection() -> SurrealConnection:
	"""Root-authenticated admin connection (not bound to a site database, no implicit transaction)."""
	if not frappe.local.flags.root_connection:
		from getpass import getpass

		from frappe.database.surrealdb.database import get_url

		if not frappe.flags.root_login:
			frappe.flags.root_login = (
				frappe.conf.get("surrealdb_root_login")
				or frappe.conf.get("root_login")
				or (sys.__stdin__.isatty() and input("Enter SurrealDB root user [root]: "))
				or "root"
			)

		if not frappe.flags.root_password:
			frappe.flags.root_password = (
				frappe.conf.get("surrealdb_root_password")
				or frappe.conf.get("root_password")
				or getpass("SurrealDB root password: ")
			)

		# SurrealDB is a separate server: its root credentials are the `surrealdb_root_login`/`surrealdb_root_password`
		# conf keys, and they must override the CLI-provided `--db-root-username`/`--db-root-password` flags — bench
		# scripts and the test harness pass MariaDB-style root credentials there, which the SurrealDB server rejects.
		if frappe.conf.get("surrealdb_root_login"):
			frappe.flags.root_login = frappe.conf["surrealdb_root_login"]
		if frappe.conf.get("surrealdb_root_password"):
			frappe.flags.root_password = frappe.conf["surrealdb_root_password"]

		frappe.local.flags.root_connection = SurrealConnection(
			ConnectionParams(
				url=get_url(frappe.conf.db_host, frappe.conf.db_port),
				namespace=_namespace(),
				database=None,
				username=frappe.flags.root_login,
				password=frappe.flags.root_password,
				level="root",
				implicit_transaction=False,
			)
		).open()

	return frappe.local.flags.root_connection


def _database_exists(root: SurrealConnection, db_name: str) -> bool:
	(info,) = root.execute("INFO FOR NS")
	return db_name in (info.get("databases") or {})


def _hash_password(root: SurrealConnection, password: str) -> str:
	(password_hash,) = root.execute("RETURN crypto::argon2::generate($pw)", {"pw": password})
	if not isinstance(password_hash, str) or not _ARGON2_HASH.match(password_hash):
		raise SurrealDBProgrammingError(0, "SurrealDB returned an unexpected password hash format.")
	return password_hash


def _in_database(root: SurrealConnection, db_name: str):
	"""Point the admin session at `db_name` (needed for database-level statements)."""
	root.select_db(db_name)


def setup_database(force, verbose=None):
	frappe.local.session = frappe._dict({"user": "Administrator"})

	db_user = _identifier(frappe.conf.db_user, "database user")
	db_name = _identifier(frappe.local.conf.db_name, "database name")
	root = get_root_connection()

	root.execute(f"DEFINE NAMESPACE IF NOT EXISTS {_namespace()}")
	exists = _database_exists(root, db_name)
	if exists and not force:
		print(f"Database {db_name} already exists, please drop it manually or pass `--force`.")
		sys.exit(1)

	if exists:
		_drop_database(root, db_name, db_user)

	root.execute(f"DEFINE DATABASE {db_name}")
	if verbose:
		print(f"Created database {db_name}")

	password_hash = _hash_password(root, frappe.conf.db_password)
	_in_database(root, db_name)
	root.execute(f"DEFINE USER OVERWRITE {db_user} ON DATABASE PASSHASH '{password_hash}' ROLES OWNER")
	if verbose:
		print(f"Created or updated user {db_user} with access to database {db_name}")

	# close root connection
	root.close()
	frappe.local.flags.root_connection = None


def _drop_database(root: SurrealConnection, db_name: str, db_user: str):
	_in_database(root, db_name)
	root.execute(f"REMOVE USER IF EXISTS {db_user} ON DATABASE")
	root.execute(f"REMOVE DATABASE IF EXISTS {db_name}")


def drop_user_and_database(db_name, db_user):
	_identifier(db_name, "database name")
	_identifier(db_user, "database user")
	root = get_root_connection()
	_drop_database(root, db_name, db_user)
	root.close()
	frappe.local.flags.root_connection = None


# MariaDB creates these in `framework_mariadb.sql`. They are the DocType tables that the `install_app`
# sync writes into *before* it can create any table itself: `updatedb` reads the `tabDocType` row of the
# doctype it syncs, and the sync imports each doctype's definition as document rows into exactly these
# tables (the `DocType` json's fields/permissions/actions/links land in the four child tables). Plus
# `DefaultValue`: a real DocType in this fork whose rows (`add_default`) are written during install by
# document inserts, so its table must pre-exist as well; its meta is built from the app's JSON.
_FRAMEWORK_DOCTYPES = ("DocType", "DocField", "DocPerm", "DocType Action", "DocType Link", "DefaultValue")


def _meta_from_file(doctype: str):
	"""DocType meta before the doctype row exists, built straight from the app's JSON.

	A full `Meta` cannot be constructed here: `BaseDocument` validates child-document columns
	against the database (`get_valid_columns` does a raw `get_table_columns` read for every
	doctype-for-doctype — exactly the tables this function is about to create). The table builder
	(`DBTable`/`SurrealDBTable`) only consumes the field list plus a few scalar attributes, so a
	small stand-in provides exactly that shape, mirroring `Meta.get_fieldnames_with_value`.
	"""
	import os

	from frappe.model.meta import NO_VALUE_FIELDS

	class _FileMeta(frappe._dict):
		def get(self, key, default=None):
			return dict.get(self, key, default)

		def get_fieldnames_with_value(self, with_field_meta=False, with_virtual_fields=False):
			def is_value_field(df):
				return df.get("fieldtype") not in NO_VALUE_FIELDS and (
					with_virtual_fields or not df.get("is_virtual")
				)

			if with_field_meta:
				return [df for df in self.fields if is_value_field(df)]
			return [df["fieldname"] for df in self.fields if is_value_field(df)]

	fname = frappe.scrub(doctype)
	path = os.path.join(frappe.get_app_path("frappe"), "core", "doctype", fname, fname + ".json")
	with open(path) as f:
		return _FileMeta(json.load(f))


def bootstrap_database(verbose=None, source_sql=None):
	"""Framework schema bootstrap (chunk P1.11). MariaDB's reference is `framework_mariadb.sql` +
	`create_auth_table`/`create_global_search_table`/`create_user_settings_table` in `install_db`.

	Called by `install_db` after `setup_database`, on the site connection (the database and the
	database-level user already exist). Creates every table that the first DocType sync or the install
	itself writes to:
	* the sync's own tables (see `_FRAMEWORK_DOCTYPES`) from the app's JSON metas, so `install_app`
	  finds them ready and later `updatedb` calls reconcile them (no-op when identical),
	* the meta-less system tables `tabSingles`, `tabSeries` and `tabSessions` (MariaDB: framework SQL;
	  `__Auth`, `__global_search`, `__UserSettings` come from `install_db` right after this).
	"""
	import sys

	if source_sql is not None:
		unsupported("restoring a MariaDB-style dump (source_sql) into SurrealDB", "P1.11")

	frappe.connect()

	from frappe.database.surrealdb.schema import SurrealDBTable
	from frappe.utils import get_table_name

	for doctype in _FRAMEWORK_DOCTYPES:
		if get_table_name(doctype) not in frappe.db.get_tables(cached=False):
			SurrealDBTable(doctype, _meta_from_file(doctype)).create()
			if verbose:
				print(f"Created table {get_table_name(doctype)}")

	frappe.db.create_singles_table()
	frappe.db.create_series_table()
	frappe.db.create_sessions_table()
	if verbose:
		print("Created the framework system tables")

	frappe.db.commit()

	if "tabDefaultValue" not in frappe.db.get_tables(cached=False):
		from click import secho

		secho(
			"Table 'tabDefaultValue' missing after the framework bootstrap. "
			"Do go through the above output to check the exact error from SurrealDB",
			fg="red",
		)
		sys.exit(1)