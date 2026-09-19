from frappe.database.schema import DBTable
from frappe.database.surrealdb.errors import unsupported


class SurrealDBTable(DBTable):
	def create(self):
		unsupported("creating a table for a DocType", "P1.4")

	def alter(self):
		unsupported("altering a table for a DocType", "P1.4")
