from pypika import Query
from pypika.queries import QueryBuilder
from pypika.terms import Field, ValueWrapper
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
		self._duplicate_updates = []

	def __copy__(self):
		clone = super().__copy__()
		clone._ignore = self._ignore
		clone._duplicate_updates = list(self._duplicate_updates)
		return clone

	@builder
	def ignore(self):
		"""`INSERT IGNORE`: rows whose record id already exists are skipped, like MariaDB's."""
		self._ignore = True

	@builder
	def for_update(self, nowait: bool = False, skip_locked: bool = False, of=()):
		"""PyPika's MySQL dialect signature (frappe's fork of pypika), because `frappe.database.query`
		calls `for_update(skip_locked=..., nowait=...)` with these keyword arguments. The base builder
		only has the no-argument variant. The translator reads the stored flags (`P1.8`): the flags land
		in the same attributes the fork's MySQL builder uses, so `_for_update_mode` is engine-agnostic."""
		self._for_update = True
		self._for_update_skip_locked = skip_locked
		self._for_update_nowait = nowait
		self._for_update_of = set(of)

	@builder
	def on_duplicate_key_update(self, field, value):
		"""`INSERT .. ON DUPLICATE KEY UPDATE field = value` (same call as PyPika's MySQL builder; `Values(field)` allowed)."""
		self._duplicate_updates.append(
			(field if isinstance(field, Field) else Field(field), ValueWrapper(value))
		)

	def get_sql(self, *args, **kwargs):
		# lazy: importing frappe.database at module level is circular while frappe/query_builder initialises
		from frappe.database.surrealdb.translator import render

		text, params = render(self, kwargs.get("param_wrapper"))
		self.surreal_params = (
			params.values
		)  # only filled when no NamedParameterWrapper was supplied (tests, debugging)
		return text


class SurrealDB(Base, Query):
	Field = Field  # frappe.qb.Field(...) is used on the dialect class (cache_manager, commands/site.py)

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
