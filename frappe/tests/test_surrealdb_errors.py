import builtins

from frappe.database.surrealdb import errors as E
from frappe.database.surrealdb.database import SurrealDBExceptionUtil as U
from frappe.tests import UnitTestCase

# Messages copied verbatim from spike/P1.3-driver/error_kinds.out.jsonl (SurrealDB 3.2.4).
MEASURED = [
	(
		"Database index `uq_name` already contains 'a', with record `t:a`",
		E.SurrealDBIntegrityError,
		E.ER_DUP_ENTRY,
		"Duplicate entry 'a' for key 'uq_name'",
	),
	(
		"Database record `t:a` already exists",
		E.SurrealDBIntegrityError,
		E.ER_DUP_ENTRY,
		"Duplicate entry 'a' for key 'PRIMARY'",
	),
	(
		"Database record `tabToDo:⟨Abc def⟩` already exists",
		E.SurrealDBIntegrityError,
		E.ER_DUP_ENTRY,
		"Duplicate entry 'Abc def' for key 'PRIMARY'",
	),
	(
		"Found 'toolongvalue' for field `name`, with record `t:c`, but field must conform to: string::len($value) <= 5",
		E.SurrealDBDataError,
		E.ER_DATA_TOO_LONG,
		"Data too long for column 'name' at row 1",
	),
	(
		"Found '' for field `req`, with record `t:d`, but field must conform to: $value != ''",
		E.SurrealDBDataError,
		E.ER_CHECK_CONSTRAINT,
		"CONSTRAINT `req` failed: $value != ''",
	),
	(
		"Couldn't coerce value for field `n` of `t:e`: Expected `int` but found `'notint'`",
		E.SurrealDBDataError,
		E.ER_TRUNCATED_WRONG_VALUE,
		"Incorrect int value: 'notint' for column 'n' at row 1",
	),
	(
		"Found field 'undefined_field', but no such field exists for table 't'",
		E.SurrealDBProgrammingError,
		E.ER_BAD_FIELD,
		"Unknown column 'undefined_field' in 'field list'",
	),
	(
		"The table 'no_such_table' does not exist",
		E.SurrealDBProgrammingError,
		E.ER_NO_SUCH_TABLE,
		"Table 'no_such_table' doesn't exist",
	),
	(
		"The table 't' already exists",
		E.SurrealDBProgrammingError,
		E.ER_TABLE_EXISTS,
		"Table 't' already exists",
	),
	(
		"The field 'n' already exists",
		E.SurrealDBProgrammingError,
		E.ER_DUP_FIELDNAME,
		"Duplicate column name 'n'",
	),
	(
		"The index 'uq_name' already exists",
		E.SurrealDBProgrammingError,
		E.ER_DUP_KEYNAME,
		"Duplicate key name 'uq_name'",
	),
	(
		"The index 'no_idx' does not exist",
		E.SurrealDBProgrammingError,
		E.ER_CANT_DROP_FIELD_OR_KEY,
		"Can't DROP 'no_idx'; check that column/key exists",
	),
	(
		"The query was not executed because it exceeded the timeout: 100ms",
		E.SurrealDBTimeoutError,
		E.ER_STATEMENT_TIMEOUT,
		"Query execution was interrupted (max_statement_time exceeded)",
	),
	(
		"There was a problem with the key-value store: Transaction conflict: Resource busy. This transaction can be retried",
		E.SurrealDBTransactionConflict,
		E.ER_DEADLOCK,
		"Deadlock found when trying to get lock; try restarting transaction",
	),
	(
		"IAM error: Not enough permissions to perform this action",
		E.SurrealDBAuthError,
		E.ER_ACCESS_DENIED,
		"Access denied",
	),
	(
		"Database record `tabParityZz:`pz-1`` already exists",
		E.SurrealDBIntegrityError,
		E.ER_DUP_ENTRY,
		"Duplicate entry 'pz-1' for key 'PRIMARY'",
	),
	(
		"Couldn't coerce value for field `qty` of `tabParityZz:`qq-1``: Expected `int` but found `NULL`",
		E.SurrealDBIntegrityError,
		E.ER_BAD_NULL_ERROR,
		"Column 'qty' cannot be null",
	),
	(
		"Found 'xxx' for field `title`, with record `tabParityZz:`qq-2``, but field must conform to: $value = NULL OR (string::len($value) <= 140)",
		E.SurrealDBDataError,
		E.ER_DATA_TOO_LONG,
		"Data too long for column 'title' at row 1",
	),
	(
		"Database index `title` already contains 'k', with record `tabParityZz:`pz-3``",
		E.SurrealDBIntegrityError,
		E.ER_DUP_ENTRY,
		"Duplicate entry 'k' for key 'title'",
	),
]


class SDKError(Exception):
	"""Stand-in for surrealdb.errors.* (they expose `kind`/`code`/`details`)."""

	def __init__(self, message, details=None):
		super().__init__(message)
		self.details = details


class NotAllowedError(SDKError):
	pass


class ConnectionClosedError(Exception):
	pass


class TestSurrealDBErrorClassification(UnitTestCase):
	def test_measured_statement_messages(self):
		for message, cls, code, mariadb_text in MEASURED:
			with self.subTest(message=message[:60]):
				err = E.classify_statement_error(message)
				self.assertIsInstance(err, cls)
				self.assertEqual(err.args, (code, mariadb_text))
				self.assertEqual(err.raw, message)

	def test_unrecognised_message_is_internal_and_matches_no_predicate(self):
		err = E.classify_statement_error("something SurrealDB 9 might say")
		self.assertIsInstance(err, E.SurrealDBInternalError)
		self.assertEqual(err.code, 0)
		for name in dir(U):
			if name.startswith(("is_", "cant_")):
				self.assertFalse(getattr(U, name)(err), name)

	def test_predicates(self):
		dup = E.classify_statement_error(MEASURED[0][0])
		pk = E.classify_statement_error(MEASURED[1][0])
		self.assertTrue(U.is_duplicate_entry(dup) and U.is_unique_key_violation(dup))
		self.assertFalse(U.is_primary_key_violation(dup))
		self.assertTrue(U.is_duplicate_entry(pk) and U.is_primary_key_violation(pk))
		self.assertFalse(U.is_unique_key_violation(pk))
		self.assertTrue(U.is_table_missing(E.classify_statement_error(MEASURED[7][0])))
		self.assertTrue(U.is_missing_column(E.classify_statement_error(MEASURED[6][0])))
		self.assertTrue(U.is_data_too_long(E.classify_statement_error(MEASURED[3][0])))
		self.assertTrue(U.is_statement_timeout(E.classify_statement_error(MEASURED[12][0])))
		self.assertTrue(U.is_deadlocked(E.classify_statement_error(MEASURED[13][0])))
		self.assertTrue(U.is_access_denied(E.classify_statement_error(MEASURED[14][0])))
		self.assertTrue(U.cant_drop_field_or_key(E.classify_statement_error(MEASURED[11][0])))
		self.assertTrue(U.is_duplicate_fieldname(E.classify_statement_error(MEASURED[9][0])))

	def test_rpc_errors(self):
		parse = E.classify_rpc_error(
			{"code": -32000, "kind": "Validation", "message": "Parse error: Unexpected token"}
		)
		self.assertTrue(U.is_syntax_error(parse))
		conflict = E.classify_rpc_error(
			{"code": -32009, "kind": "Query", "message": "x", "details": {"kind": "TransactionConflict"}}
		)
		self.assertIsInstance(conflict, E.SurrealDBTransactionConflict)
		auth = E.classify_rpc_error({"code": -32002, "kind": "NotAllowed", "message": "There was a problem"})
		self.assertTrue(U.is_access_denied(auth))

	def test_sdk_and_transport_exceptions(self):
		self.assertIsInstance(
			E.classify_exception(SDKError("boom", {"kind": "TransactionConflict"})),
			E.SurrealDBTransactionConflict,
		)
		self.assertIsInstance(
			E.classify_exception(NotAllowedError("There was a problem with authentication")),
			E.SurrealDBAuthError,
		)
		for exc in (
			ConnectionClosedError("sent 1000"),
			builtins.ConnectionRefusedError(111, "refused"),
			TimeoutError(),
		):
			err = E.classify_exception(exc)
			self.assertIsInstance(err, E.SurrealDBConnectionError)
			self.assertTrue(U.is_interface_error(err))
		already = E.SurrealDBProgrammingError(1064, "x")
		self.assertIs(E.classify_exception(already), already)

	def test_hierarchy_and_args_shape(self):
		for cls in (
			E.SurrealDBProgrammingError,
			E.SurrealDBIntegrityError,
			E.SurrealDBDataError,
			E.SurrealDBOperationalError,
			E.SurrealDBInternalError,
		):
			self.assertTrue(issubclass(cls, E.SurrealDBError))
		for cls in (E.SurrealDBConnectionError, E.SurrealDBAuthError, E.SurrealDBTimeoutError):
			self.assertTrue(issubclass(cls, E.SurrealDBOperationalError))
		self.assertTrue(issubclass(E.SurrealDBTransactionConflict, E.SurrealDBOperationalError))
		self.assertTrue(issubclass(E.SurrealDBTransactionTainted, E.SurrealDBOperationalError))
