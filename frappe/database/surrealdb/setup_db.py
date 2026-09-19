from frappe.database.surrealdb.errors import unsupported


def setup_database(force, verbose=None):
	unsupported("provisioning a namespace/database/user for a site", "P1.2")


def bootstrap_database(verbose=None, source_sql=None):
	unsupported("bootstrapping the framework schema", "P1.11")


def drop_user_and_database(db_name, db_user):
	unsupported("dropping a site's namespace/database/user", "P1.2")


def get_root_connection():
	unsupported("the root connection", "P1.2")
