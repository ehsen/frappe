from pypika import Query
from pypika.queries import QueryBuilder

from frappe.query_builder.builder import Base
from frappe.query_builder.terms import ParameterizedValueWrapper


class SurrealDBQueryBuilder(QueryBuilder):
	"""Facade over PyPika query objects; renders SurrealQL from the typed query tree (ADR 0003).

	Until P1.6 lands there is no renderer, and rendering must fail closed: PyPika's generic SQL is not
	SurrealQL, so silently emitting it would be wrong.
	"""

	def __init__(self, **kwargs):
		super().__init__(dialect=None, **kwargs)

	def get_sql(self, *args, **kwargs):
		# lazy: importing frappe.database at module level is circular while frappe/query_builder initialises
		from frappe.database.surrealdb.errors import unsupported

		unsupported("SurrealQL rendering of query objects", "P1.6")


class SurrealDB(Base, Query):
	_BuilderClasss = SurrealDBQueryBuilder

	@classmethod
	def _builder(cls, **kwargs) -> SurrealDBQueryBuilder:
		# PyPika's Query._builder would build a plain QueryBuilder (generic SQL), so construct ours explicitly
		return cls._BuilderClasss(wrapper_cls=ParameterizedValueWrapper, **kwargs)

	@classmethod
	def from_(cls, table, *args, **kwargs):
		if isinstance(table, str):
			table = cls.DocType(table)
		return super().from_(table, *args, **kwargs)
