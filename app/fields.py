"""
Field registry, test catalogue and stations.

The registry lives in the `field_registry` table and is seeded from
mappings/fields.yaml (+ mappings/tests.yaml) the first time the database is
created. Admins edit it in the UI; changes are applied only after HOD
approval (approval type `registry_change`), which bumps `settings.registry_rev`.

Every consumer (web forms, Excel templates, importer, AI schema, rules,
report auto-mapper) reads it through this module. Results are cached and
re-read when the revision number changes.
"""
import json
import re
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import yaml

import config

_NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
TESTS_YAML = config.FIELDS_YAML.replace("fields.yaml", "tests.yaml")

_lock = threading.Lock()
_cache = {"data": None, "rev": None, "checked": 0.0, "db": None}


# =============================================================================
# seeding
# =============================================================================
def _yaml(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def registry_row(f):
    """YAML/API field dict -> field_registry column values."""
    extra = {k: f[k] for k in ("option_labels",) if k in f}
    return (f["key"], f["label"], f.get("unit") or "", f.get("dtype", "number"), f.get("dp"), f["source"],
            f.get("test_code"), f.get("station"), 1 if f.get("required") else 0, f.get("formula"), f.get("stage"),
            f.get("ui"), json.dumps(f["options"]) if f.get("options") else None, f.get("section"), f.get("sheet"),
            f.get("row"), f.get("col"), f.get("report_label"), 1 if f.get("multi") else 0, f.get("order", 0),
            1 if f.get("active", True) else 0, json.dumps(extra) if extra else None)


REGISTRY_COLS = ("key,label,unit,dtype,dp,source,test_code,station,required,formula,stage,ui,options_json,section,"
                 "sheet,row_label,col_label,report_label,multi,display_order,active,extra_json")


def seed(conn, force=False):
    """Load fields.yaml / tests.yaml into the database if the tables are empty (or force=True)."""
    have = conn.execute("SELECT COUNT(*) FROM field_registry").fetchone()[0]
    if have and not force:
        return False
    data = _yaml(config.FIELDS_YAML)
    tests = _yaml(TESTS_YAML)
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        if force:
            conn.execute("DELETE FROM field_registry")
            conn.execute("DELETE FROM test_catalogue")
            conn.execute("DELETE FROM stations")
        q = ",".join("?" * len(REGISTRY_COLS.split(",")))
        conn.executemany(f"INSERT INTO field_registry({REGISTRY_COLS}) VALUES ({q})",
                         [registry_row(f) for f in data["fields"]])
        conn.executemany("INSERT INTO test_catalogue(code,name,grp,clause,standard,station,excel_template,report_section,"
                         "display_order) VALUES (?,?,?,?,?,?,?,?,?)",
                         [(t["code"], t["name"], t["group"], t.get("clause"), t.get("standard"), t.get("station"),
                           t.get("excel_template"), t.get("report_section"), t.get("order", 0)) for t in tests["tests"]])
        conn.executemany("INSERT INTO stations(code,title,logsheet,file) VALUES (?,?,?,?)",
                         [(s["code"], s["title"], s.get("logsheet"), s.get("file")) for s in tests["stations"]])
        conn.execute("INSERT INTO settings(key, value_json, updated_at) VALUES ('registry_rev', '1', datetime('now')) "
                     "ON CONFLICT(key) DO UPDATE SET value_json = CAST(CAST(value_json AS INTEGER) + 1 AS TEXT)")
        if own_tx:
            conn.execute("COMMIT")
    except BaseException:
        if own_tx:
            conn.execute("ROLLBACK")
        raise
    invalidate()
    return True


def apply_changes(conn, changes):
    """
    Apply HOD-approved registry edits (caller holds the transaction).
    changes: [{"op": "upsert", "field": {...}} | {"op": "deactivate", "key": "..."}]
    """
    q = ",".join("?" * len(REGISTRY_COLS.split(",")))
    n = 0
    for ch in changes:
        if ch.get("op") == "deactivate":
            n += conn.execute("UPDATE field_registry SET active=0 WHERE key=?", (ch["key"],)).rowcount
        else:
            f = dict(ch["field"])
            old = conn.execute("SELECT display_order FROM field_registry WHERE key=?", (f["key"],)).fetchone()
            f.setdefault("order", old["display_order"] if old else
                         conn.execute("SELECT COALESCE(MAX(display_order),0)+1 FROM field_registry").fetchone()[0])
            conn.execute(f"INSERT OR REPLACE INTO field_registry({REGISTRY_COLS}) VALUES ({q})", registry_row(f))
            n += 1
    conn.execute("UPDATE settings SET value_json = CAST(CAST(value_json AS INTEGER) + 1 AS TEXT) WHERE key='registry_rev'")
    invalidate()
    return n


def field_as_change(f):
    """Registry dict -> the shape stored in a registry_change approval payload."""
    keys = ("key", "label", "unit", "dtype", "dp", "source", "test_code", "station", "required", "formula", "stage", "ui",
            "options", "section", "sheet", "row", "col", "report_label", "multi", "order")
    return {k: f.get(k) for k in keys}


# =============================================================================
# loading (cached)
# =============================================================================
def invalidate():
    with _lock:
        _cache["data"] = None
        _cache["rev"] = None


def _row_to_field(r):
    f = {
        "key": r["key"], "label": r["label"], "unit": r["unit"] or "", "dtype": r["dtype"], "dp": r["dp"],
        "source": r["source"], "test_code": r["test_code"], "station": r["station"], "required": bool(r["required"]),
        "formula": r["formula"], "stage": r["stage"] or "pre", "ui": r["ui"],
        "options": json.loads(r["options_json"]) if r["options_json"] else None, "section": r["section"],
        "sheet": r["sheet"], "row": r["row_label"], "col": r["col_label"], "report_label": r["report_label"],
        "multi": bool(r["multi"]), "order": r["display_order"], "active": bool(r["active"]),
    }
    f.update(json.loads(r["extra_json"]) if r["extra_json"] else {})
    # v1 aliases still used by the Excel layer
    f["type"] = f["dtype"]
    f["test"] = f["test_code"]
    return f


def _read(conn):
    fields = [_row_to_field(r) for r in conn.execute("SELECT * FROM field_registry WHERE active=1 ORDER BY display_order, key")]
    tests = {}
    for r in conn.execute("SELECT * FROM test_catalogue WHERE active=1 ORDER BY display_order"):
        tests[r["code"]] = {"code": r["code"], "name": r["name"], "label": r["name"], "group": r["grp"],
                            "clause": r["clause"], "standard": r["standard"], "station": r["station"],
                            "excel_template": r["excel_template"], "report_section": r["report_section"],
                            "order": r["display_order"]}
    stations = {r["code"]: {"title": r["title"], "logsheet": r["logsheet"], "file": r["file"]}
                for r in conn.execute("SELECT * FROM stations ORDER BY rowid")}
    data = {"fields": fields, "tests": tests, "stations": stations}
    data["by_key"] = {f["key"]: f for f in fields}
    data["by_station"] = {}
    for f in fields:
        if f["source"] == "reading":
            data["by_station"].setdefault(f["station"], []).append(f)
    return data


def load():
    """Registry snapshot; re-read from the DB when settings.registry_rev changes (checked at most every 2 s)."""
    from . import db as dbm
    now = time.time()
    with _lock:
        data, checked = _cache["data"], _cache["checked"]
        if data is not None and now - checked < 2 and _cache["db"] == config.DB_PATH:
            return data
    conn = dbm.connect()
    try:
        dbm.init_db_conn(conn)
        seed(conn)
        rev = conn.execute("SELECT value_json FROM settings WHERE key='registry_rev'").fetchone()
        rev = rev[0] if rev else "0"
        with _lock:
            if _cache["data"] is not None and _cache["rev"] == rev and _cache["db"] == config.DB_PATH:
                _cache["checked"] = now
                return _cache["data"]
        data = _read(conn)
    finally:
        conn.close()
    with _lock:
        _cache.update(data=data, rev=rev, checked=now, db=config.DB_PATH)
    return data


def stations():
    return load()["stations"]


def tests():
    return load()["tests"]


def all_fields():
    return load()["fields"]


def field(key):
    return load()["by_key"].get(key)


def station_fields(station):
    return load()["by_station"].get(station, [])


def request_fields():
    """Request form + proforma fields in display order (drives the web form and the AI schema)."""
    return [f for f in load()["fields"] if f["source"] in ("request", "proforma")]


def computed_fields():
    return [f for f in load()["fields"] if f["source"] == "computed"]


def stations_for_tests(test_codes):
    """Ordered list of logsheet stations needed for the requested tests."""
    t = tests()
    wanted = {t[c]["station"] for c in test_codes if c in t}
    return [s for s in stations() if s in wanted]


def is_required(f, test_codes):
    return bool(f.get("required")) and f.get("test_code") in test_codes


def defined_name(key):
    """Excel defined name for a key: dots are not allowed -> underscores."""
    return key.replace(".", "_")


# =============================================================================
# number helpers
# =============================================================================
def to_number(value):
    """Parse a cell value as a number; accepts '1,234.5', '4.19 GΩ', etc. Returns Decimal or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    s = str(value).strip().replace(",", "")
    m = _NUM_RE.match(s)
    if not m:
        return None
    try:
        return Decimal(m.group(0))
    except InvalidOperation:
        return None


def fmt(value, dp):
    """Format a value with `dp` decimals (round half up, like a lab sheet)."""
    if value is None or value == "":
        return ""
    if dp is None:
        return str(value)
    d = to_number(value)
    if d is None:
        return str(value)
    q = Decimal(1).scaleb(-int(dp)) if int(dp) > 0 else Decimal(1)
    return str(d.quantize(q, rounding=ROUND_HALF_UP))
