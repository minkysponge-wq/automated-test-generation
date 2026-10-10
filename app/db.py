"""
SQLite access layer.

Concurrency model (many users on the LAN at once):
  * WAL journal mode - readers never block the single writer
  * busy_timeout=5000 - a writer waits up to 5 s for the lock instead of failing
  * one short-lived connection per HTTP request (opened lazily, closed on teardown)
  * `tx()` wraps writes in BEGIN IMMEDIATE so read-modify-write sequences
    (number allocation, optimistic-lock checks, audit hash chain) are atomic
"""
import contextlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import config

IST = timezone(timedelta(hours=5, minutes=30))
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def now_ist():
    """ISO timestamp in Indian Standard Time, seconds precision."""
    return datetime.now(IST).isoformat(timespec="seconds")


def connect(path=None):
    """Open a connection with the pragmas every connection needs."""
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # isolation_level=None -> autocommit; transactions are opened explicitly with tx()
    conn = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextlib.contextmanager
def tx(conn):
    """BEGIN IMMEDIATE ... COMMIT (or ROLLBACK on any exception)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(path=None):
    """Create tables/triggers if they do not exist (idempotent)."""
    conn = connect(path)
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        conn.executescript(fh.read())
    conn.close()


# --- Flask integration ------------------------------------------------------
def get_db():
    from flask import g
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    from flask import g
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


# --- small helpers ------------------------------------------------------------
def one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()


def all_(conn, sql, *args):
    return conn.execute(sql, args).fetchall()


def get_settings(conn):
    """DEFAULT_SETTINGS overlaid with whatever the admin saved."""
    out = dict(config.DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value_json FROM settings"):
        out[row["key"]] = json.loads(row["value_json"])
    return out


def save_setting(conn, key, value, user_id):
    conn.execute(
        "INSERT INTO settings(key, value_json, updated_by, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
        "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
        (key, json.dumps(value), user_id, now_ist()),
    )
