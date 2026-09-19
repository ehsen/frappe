"""Site provisioning for SurrealDB (chunk P1.2).

Mapping of MariaDB concepts: the site's `db_name` is a SurrealDB *database* inside the bench's *namespace*
(`db_namespace`, default `frappe`); `db_user`/`db_password` become a database-level user with the OWNER role, which
can define tables but cannot touch other databases or server-level objects (measured, spike/P1.3-driver). The root
credentials (`root_login`/`root_password`, like MariaDB's) are used only here and never stored in a site.

Identifiers cannot be bound in SurrealQL, so they are validated against a strict pattern before being embedded.
The password cannot be bound in `DEFINE USER` either (measured), so it is hashed server-side with a *bound*
`crypto::argon2::generate($pw)` and only the validated hash literal is embedded (`PASSHASH`).
"""

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


def bootstrap_database(verbose=None, source_sql=None):
	unsupported("bootstrapping the framework schema", "P1.11")
