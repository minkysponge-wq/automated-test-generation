"""
Loads mappings/fields.yaml - the single source of truth for every reading.
"""
import functools
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import yaml

import config

_NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


@functools.lru_cache(maxsize=1)
def load():
    with open(config.FIELDS_YAML, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    fields = data["fields"]
    data["by_key"] = {f["key"]: f for f in fields}
    data["by_station"] = {}
    for f in fields:
        data["by_station"].setdefault(f["station"], []).append(f)
    return data


def stations():
    return load()["stations"]


def tests():
    return load()["tests"]


def field(key):
    return load()["by_key"].get(key)


def station_fields(station):
    return load()["by_station"].get(station, [])


def stations_for_tests(test_codes):
    """Ordered list of logsheet stations needed for the requested tests."""
    t = tests()
    wanted = {t[c]["station"] for c in test_codes if c in t}
    return [s for s in stations() if s in wanted]


def is_required(f, test_codes):
    return bool(f.get("required")) and f.get("test") in test_codes


def defined_name(key):
    """Excel defined name for a key: dots are not allowed -> underscores."""
    return key.replace(".", "_")


def to_number(value):
    """Parse a cell value as a number; accepts '1,234.5', '4.19 GΩ', etc. Returns Decimal or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    s = str(value).strip().replace(",", "")
    # take the leading number and ignore a trailing unit ("4.19 GΩ", "0.51%")
    m = _NUM_RE.match(s)
    if not m:
        return None
    try:
        return Decimal(m.group(0))
    except InvalidOperation:
        return None


def fmt(value, dp):
    """Format a stored value with `dp` decimals (round half up, like a lab sheet)."""
    if value is None or value == "":
        return ""
    if dp is None:
        return str(value)
    d = to_number(value)
    if d is None:
        return str(value)
    q = Decimal(1).scaleb(-int(dp)) if int(dp) > 0 else Decimal(1)
    return str(d.quantize(q, rounding=ROUND_HALF_UP))
