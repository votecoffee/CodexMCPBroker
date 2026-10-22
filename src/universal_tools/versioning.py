"""Fixed release timestamps and conservative numeric package-version comparison."""

import re
from datetime import datetime


def timestamp_version(instant):
    """Format an aware release instant in its explicitly selected local time zone."""
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise ValueError("release time must include an explicit time-zone offset")
    if not 2000 <= instant.year < 2100:
        raise ValueError("two-digit release years require a date from 2000 through 2099")
    return instant.strftime("%y.%m%d.%H%M%S")


def validate_timestamp_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{2}\.[0-9]{4}\.[0-9]{6}", value):
        raise ValueError("release version must use YY.MMDD.HHmmss")
    try:
        datetime.strptime("20" + value, "%Y.%m%d.%H%M%S")
    except ValueError as error:
        raise ValueError("release version contains an invalid date or time") from error
    return value


def package_version(value):
    """Numeric canonical identity used by PEP 440 and strict SemVer packages."""
    validate_timestamp_version(value)
    return ".".join(str(int(part)) for part in value.split("."))


def file_version(value):
    """Map a display timestamp to four bounded Windows/.NET version components."""
    validate_timestamp_version(value)
    year, date, clock = value.split(".")
    return ".".join(str(int(part)) for part in (year, date, clock[:4], clock[4:]))


def numeric_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", value):
        return None
    parts = tuple(map(int, value.split(".")))
    return parts + (0,) * (4 - len(parts))


def versions_equal(left, right):
    """Padding aliases are equal; unknown/custom versions retain exact comparison."""
    first, second = numeric_version(left), numeric_version(right)
    return first == second if first is not None and second is not None else left == right
