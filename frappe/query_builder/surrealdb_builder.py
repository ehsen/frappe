from pypika import Query
from pypika.queries import QueryBuilder
from pypika.utils import builder

from frappe.query_builder.builder import Base
from frappe.query_builder.terms import ParameterizedValueWrapper


class SurrealDBQueryBuilder(QueryBuilder):
	"""Facade over PyPika query objects; renders SurrealQL from the typed query tree (ADR 0003).

	`get_sql` is what Frappe's `prepare_query` calls (with a `NamedParameterWrapper` that collects the bound values). Anything the
	renderer does not support raises `SurrealDBNotImplementedError`: PyPika's generic SQL is not SurrealQL, so silently emitting
	it would be wrong (see `frappe/database/surrealdb/translator.py`)."""

	def __init__(self, **kwargs):
		super().__init__(dialect=None, **kwargs)
		self._ignore = False

	def __copy__(self):
		clone = super().__copy__()
		clone._ignore = self._ignore
		return clone

	@builder
	def ignore(self):
		"""`INSERT IGNORE`: rows whose record id already exists are skipped, like MariaDB's."""
		self._ignore = True

	def get_sql(self, *args, **kwargs):
		# lazy: importing frappe.database at module level is circular while frappe/query_builder initialises
		from frappe.database.surrealdb.translator import render

		text, params = render(self, kwargs.get("param_wrapper"))
		self.surreal_params = (
			params.values
		)  # only filled when no NamedParameterWrapper was supplied (tests, debugging)
		return text


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
