class SurrealDBNotImplementedError(NotImplementedError):
	"""A SurrealDB code path that exists as a dispatch point but is not implemented yet.

	Every `db_type` branch that Frappe reaches for SurrealDB must either do the right thing or raise
	this. It must never fall through into another engine's behaviour (MariaDB/Postgres/SQLite).
	`chunk` names the plan chunk in the docs repo (`docs/DEVELOPMENT_PLAN.md`) that owns the work.
	"""

	def __init__(self, what: str, chunk: str):
		self.what = what
		self.chunk = chunk
		super().__init__(
			f"SurrealDB backend: {what} is not implemented yet (plan chunk {chunk}). "
			"It deliberately does not fall back to another database engine."
		)


def unsupported(what: str, chunk: str):
	raise SurrealDBNotImplementedError(what, chunk)
