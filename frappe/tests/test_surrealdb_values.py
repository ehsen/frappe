import datetime as dt
import decimal

from frappe.database.surrealdb import values as V
from frappe.tests import UnitTestCase


class TestSurrealDBValues(UnitTestCase):
	def test_date(self):
		self.assertEqual(V.to_date("2024-1-5"), "2024-01-05")
		self.assertEqual(V.to_date(dt.date(2024, 2, 29)), "2024-02-29")
		self.assertEqual(V.to_date(dt.datetime(2024, 2, 29, 13, 5)), "2024-02-29")
		self.assertEqual(V.to_date("2024-02-29 10:00:00"), "2024-02-29")
		self.assertEqual(V.to_date("0000-00-00"), "0000-00-00")  # MariaDB accepts the zero date
		self.assertIsNone(V.to_date(None))
		for bad in ("2023-02-29", "2024-13-01", "20240101", "abc", ""):
			with self.assertRaises(V.ValueError_, msg=bad):
				V.to_date(bad)

	def test_datetime(self):
		self.assertEqual(V.to_datetime("2024-01-05 3:04"), "2024-01-05 03:04:00.000000")
		self.assertEqual(V.to_datetime("2024-01-05T03:04:05.1"), "2024-01-05 03:04:05.100000")
		self.assertEqual(V.to_datetime(dt.datetime(2024, 1, 5, 3, 4, 5, 6)), "2024-01-05 03:04:05.000006")
		self.assertEqual(V.to_datetime(dt.date(2024, 1, 5)), "2024-01-05 00:00:00.000000")
		# MariaDB truncates (does not round) fractional seconds beyond 6 digits
		self.assertEqual(V.to_datetime("2024-01-05 03:04:05.1234569"), "2024-01-05 03:04:05.123456")
		with self.assertRaises(V.ValueError_):
			V.to_datetime(dt.datetime(2024, 1, 5, tzinfo=dt.UTC))
		with self.assertRaises(V.ValueError_):
			V.to_datetime("2024-01-05 25:00:00")

	def test_datetime_text_sorts_like_datetimes(self):
		values = [
			dt.datetime(2024, 1, 5, 0, 0, 0, 1),
			dt.datetime(2024, 1, 5),
			dt.datetime(2023, 12, 31, 23, 59, 59, 999999),
		]
		self.assertEqual(sorted(V.to_datetime(v) for v in values), [V.to_datetime(v) for v in sorted(values)])

	def test_time(self):
		self.assertEqual(V.to_time_us("01:02:03.5"), 3723500000)
		self.assertEqual(V.to_time_us("-838:59:59"), -(838 * 3600 + 59 * 60 + 59) * 1_000_000)
		self.assertEqual(V.to_time_us(dt.time(1, 2, 3, 4)), 3723000004)
		self.assertEqual(V.to_time_us(dt.timedelta(hours=2, microseconds=5)), 7200000005)
		self.assertEqual(V.from_time_us(7200000005), dt.timedelta(hours=2, microseconds=5))
		for bad in ("839:00:00", "1:61:00", "abc"):
			with self.assertRaises(V.ValueError_, msg=bad):
				V.to_time_us(bad)

	def test_decimal(self):
		self.assertEqual(V.to_decimal("1.0000000005"), decimal.Decimal("1.000000001"))  # half up
		self.assertEqual(V.to_decimal(-2.5, 10, 0), decimal.Decimal(-3))
		self.assertEqual(V.to_decimal(1), decimal.Decimal("1.000000000"))
		self.assertEqual(V.to_decimal("999999999999.999999999"), decimal.Decimal("999999999999.999999999"))
		for bad in ("1e12", "1000000000000", "abc", "nan"):
			with self.assertRaises(V.ValueError_, msg=bad):
				V.to_decimal(bad)

	def test_int(self):
		self.assertEqual(V.to_int("5"), 5)
		self.assertEqual(V.to_int(2.5), 3)  # MariaDB rounds half away from zero
		self.assertEqual(V.to_int(-2.5), -3)
		self.assertEqual(V.to_int(True), 1)
		self.assertEqual(V.to_int(127, "tinyint"), 127)
		for value, kind in ((128, "tinyint"), (2**31, "int"), (2**63, "bigint"), ("x", "int")):
			with self.assertRaises(V.ValueError_, msg=(value, kind)):
				V.to_int(value, kind)

	def test_str(self):
		self.assertEqual(V.to_str(5), "5")
		self.assertEqual(V.to_str(b"abc"), "abc")
		self.assertIsNone(V.to_str(None))
