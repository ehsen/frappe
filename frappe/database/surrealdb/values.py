"""Value encoders: Frappe/MariaDB value -> SurrealDB storage value, and back (ADR 0001 point 8, P0.3 findings).

The driver refuses date/time values (it cannot know the column type), so the query translator calls these encoders
with the column's storage kind *before* binding:

  Date      'YYYY-MM-DD' text                          (SurrealDB has no DATE type)
  Datetime  'YYYY-MM-DD HH:MM:SS.ffffff' text          (native indexed `datetime` returns wrong rows in 3.2.4: P0.3)
  Time      signed integer microseconds                (MariaDB TIME range +-838:59:59.999999)
  Decimal   `decimal`, half-up to the column scale     (MariaDB rounds on insert and rejects overflow)
  Int       `int` with the column's range              (MariaDB strict mode rejects out-of-range values)

MariaDB behaviours pinned by measurement: it accepts `0000-00-00`, truncates (does not round) fractional seconds beyond
6 digits, accepts 1-digit month/day parts, and rejects invalid calendar dates.
"""

import datetime as dt
import decimal
import re

DATE_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")
DT_RE = re.compile(
	r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2})(?:\.(\d{1,9}))?)?)?\s*$"
)
TIME_RE = re.compile(r"^\s*(-)?(\d{1,3}):(\d{1,2})(?::(\d{1,2})(?:\.(\d{1,9}))?)?\s*$")
MAX_TIME_US = (838 * 3600 + 59 * 60 + 59) * 1_000_000 + 999_999

INT_RANGES = {
	"tinyint": (-128, 127),
	"smallint": (-32768, 32767),
	"int": (-(2**31), 2**31 - 1),
	"bigint": (-(2**63), 2**63 - 1),
}


class ValueError_(ValueError):
	"""Raised for a value MariaDB would reject in strict mode (Frappe surfaces these as data errors)."""


def _valid_date(y, m, d):
	if (y, m, d) == (0, 0, 0):
		return True  # accepted unless NO_ZERO_DATE is set, and it is not in MariaDB 11.8's default sql_mode
	try:
		dt.date(y, m, d)
		return True
	except ValueError:
		return False


def to_date(v):
	if v is None:
		return None
	if isinstance(v, dt.datetime):
		return v.date().isoformat()
	if isinstance(v, dt.date):
		return v.isoformat()
	m = DATE_RE.match(str(v)) or DT_RE.match(str(v))
	if not m:
		raise ValueError_(f"Incorrect date value: {v!r}")
	y, mo, d = (int(x) for x in m.groups()[:3])
	if not _valid_date(y, mo, d):
		raise ValueError_(f"Incorrect date value: {v!r}")
	return f"{y:04d}-{mo:02d}-{d:02d}"


def to_datetime(v):
	if v is None:
		return None
	if isinstance(v, dt.datetime):
		if v.tzinfo:
			raise ValueError_("timezone-aware datetime: convert to the site's timezone first")
		return v.strftime("%Y-%m-%d %H:%M:%S.%f")
	if isinstance(v, dt.date):
		return f"{v.isoformat()} 00:00:00.000000"
	m = DT_RE.match(str(v))
	if not m:
		raise ValueError_(f"Incorrect datetime value: {v!r}")
	y, mo, d, h, mi, s, frac = m.groups()
	y, mo, d = int(y), int(mo), int(d)
	h, mi, s = int(h or 0), int(mi or 0), int(s or 0)
	if not _valid_date(y, mo, d) or h > 23 or mi > 59 or s > 59:
		raise ValueError_(f"Incorrect datetime value: {v!r}")
	return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}.{(frac or '').ljust(9, '0')[:6]}"


def to_time_us(v):
	if v is None:
		return None
	if isinstance(v, dt.timedelta):
		us = (v.days * 86400 + v.seconds) * 1_000_000 + v.microseconds
	elif isinstance(v, dt.time):
		us = ((v.hour * 60 + v.minute) * 60 + v.second) * 1_000_000 + v.microsecond
	else:
		m = TIME_RE.match(str(v))
		if not m:
			raise ValueError_(f"Incorrect time value: {v!r}")
		neg, h, mi, s, frac = m.groups()
		if int(mi) > 59 or int(s or 0) > 59:
			raise ValueError_(f"Incorrect time value: {v!r}")
		us = ((int(h) * 60 + int(mi)) * 60 + int(s or 0)) * 1_000_000 + int((frac or "").ljust(6, "0")[:6])
		us = -us if neg else us
	if abs(us) > MAX_TIME_US:
		raise ValueError_(f"Time out of range: {v!r}")
	return us


def from_time_us(us):
	return None if us is None else dt.timedelta(microseconds=us)


def to_decimal(v, precision: int = 21, scale: int = 9):
	if v is None:
		return None
	try:
		d = v if isinstance(v, decimal.Decimal) else decimal.Decimal(str(v).strip())
	except decimal.InvalidOperation as e:
		raise ValueError_(f"Incorrect decimal value: {v!r}") from e
	if not d.is_finite():
		raise ValueError_(f"Incorrect decimal value: {v!r}")
	q = d.quantize(decimal.Decimal(1).scaleb(-scale), rounding=decimal.ROUND_HALF_UP)
	if abs(q) >= decimal.Decimal(10) ** (precision - scale):
		raise ValueError_(f"Out of range value for decimal({precision},{scale}): {v!r}")
	return q


def to_int(v, kind: str = "int"):
	if v is None:
		return None
	if isinstance(v, bool):
		v = int(v)
	elif isinstance(v, int):
		pass
	else:
		try:
			d = decimal.Decimal(str(v).strip())
		except decimal.InvalidOperation as e:
			raise ValueError_(f"Incorrect integer value: {v!r}") from e
		if not d.is_finite():
			raise ValueError_(f"Incorrect integer value: {v!r}")
		v = int(
			d.quantize(decimal.Decimal(1), rounding=decimal.ROUND_HALF_UP)
		)  # MariaDB rounds half away from zero
	lo, hi = INT_RANGES[kind]
	if not lo <= v <= hi:
		raise ValueError_(f"Out of range value for {kind}: {v!r}")
	return v


def to_str(v):
	if v is None:
		return None
	if isinstance(v, bytes):
		return v.decode("utf-8")
	return v if isinstance(v, str) else str(v)
